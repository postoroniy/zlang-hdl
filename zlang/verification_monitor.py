"""Engine-neutral sampling of compiler-owned verification overlays."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from zlang.ir import expressions as expr
from zlang.ir.interfaces import ReadyValidSignal
from zlang.ir.module import Function
from zlang.ir.runtime_values import RuntimeValueError, normalize_scalar
from zlang.ir.verification import (
    VerificationGoalKind,
    VerificationScope,
    validate_verification_overlay,
)
from zlang.simulation_errors import (
    SimulationError,
    VerificationAssertionError,
    VerificationCoverWitness,
    VerificationRequirementViolation,
    verification_origin_text,
)
from zlang.source import SourceOrigin


@dataclass(frozen=True)
class VerificationSampleResult:
    """Events produced by one pre-edge verification sample."""

    cycle: int
    reset_suppressed: bool
    active_scope_ids: tuple[str, ...] = ()
    requirement_violations: tuple[VerificationRequirementViolation, ...] = ()
    cover_witnesses: tuple[VerificationCoverWitness, ...] = ()


def _evaluate(
    expression: expr.Expression,
    values: Mapping[str, object],
    functions: Mapping[str, Function],
) -> object:
    if isinstance(expression, (expr.InputRef, expr.ParameterRef, expr.RegisterRef)):
        return values[expression.name]
    if isinstance(expression, expr.InstanceOutputRef):
        return values[f"{expression.instance}.{expression.port}"]
    if isinstance(expression, expr.ReadyValidRef):
        if expression.signal is ReadyValidSignal.TRANSFER:
            return int(
                bool(values[f"{expression.interface}.valid"])
                and bool(values[f"{expression.interface}.ready"])
            )
        return values[f"{expression.interface}.{expression.signal.value}"]
    if isinstance(expression, expr.Constant):
        return expression.value
    if isinstance(expression, expr.Add):
        value = _evaluate(expression.left, values, functions) + _evaluate(
            expression.right, values, functions
        )
        try:
            return normalize_scalar(value, expression.type)
        except RuntimeValueError as error:
            raise SimulationError(str(error)) from error
    if isinstance(expression, expr.Binary):
        left = _evaluate(expression.left, values, functions)
        right = _evaluate(expression.right, values, functions)
        if expression.operator is expr.BinaryOperator.EQUAL:
            return int(left == right)
        if expression.operator is expr.BinaryOperator.NOT_EQUAL:
            return int(left != right)
        if expression.operator is expr.BinaryOperator.LESS:
            return int(left < right)
        if expression.operator is expr.BinaryOperator.LESS_EQUAL:
            return int(left <= right)
        if expression.operator is expr.BinaryOperator.GREATER:
            return int(left > right)
        if expression.operator is expr.BinaryOperator.GREATER_EQUAL:
            return int(left >= right)
        if expression.operator is expr.BinaryOperator.SUBTRACT:
            value = left - right
        elif expression.operator is expr.BinaryOperator.MULTIPLY:
            value = left * right
        elif expression.operator is expr.BinaryOperator.BIT_AND:
            value = left & right
        elif expression.operator is expr.BinaryOperator.BIT_OR:
            value = left | right
        elif expression.operator is expr.BinaryOperator.BIT_XOR:
            value = left ^ right
        elif expression.operator is expr.BinaryOperator.SHIFT_LEFT:
            value = left << right
        elif expression.operator is expr.BinaryOperator.SHIFT_RIGHT:
            value = left >> right
        else:  # pragma: no cover - closed enum
            raise SimulationError(
                f"unsupported verification operator {expression.operator.value}"
            )
        try:
            return normalize_scalar(value, expression.type)
        except RuntimeValueError as error:
            raise SimulationError(str(error)) from error
    if isinstance(expression, expr.Extend):
        return _evaluate(expression.expression, values, functions)
    if isinstance(expression, expr.Truncate):
        value = _evaluate(expression.expression, values, functions)
        try:
            return normalize_scalar(value, expression.type)
        except RuntimeValueError as error:
            raise SimulationError(str(error)) from error
    if isinstance(expression, expr.Mux):
        branch = (
            expression.when_true
            if _evaluate(expression.condition, values, functions)
            else expression.when_false
        )
        return _evaluate(branch, values, functions)
    if isinstance(expression, expr.Switch):
        selector = _evaluate(expression.selector, values, functions)
        for case in expression.cases:
            if case.key == selector:
                return _evaluate(case.expression, values, functions)
        return _evaluate(expression.default, values, functions)
    if isinstance(expression, expr.Call):
        function = functions[expression.function]
        arguments = tuple(
            _evaluate(argument, values, functions)
            for argument in expression.arguments
        )
        bindings = {
            parameter.name: argument
            for parameter, argument in zip(
                function.parameters, arguments, strict=True
            )
        }
        return _evaluate(function.body, bindings, functions)
    raise SimulationError(
        f"verification monitor does not support {type(expression).__name__}"
    )


class VerificationMonitor:
    """Sample typed verification scopes without owning simulation state."""

    def __init__(
        self,
        scopes: Iterable[VerificationScope],
        functions: Iterable[Function] = (),
    ) -> None:
        self.scopes = tuple(scopes)
        validate_verification_overlay(self.scopes)
        self.functions: dict[str, Function] = {}
        for function in functions:
            previous = self.functions.get(function.name)
            if previous is not None and previous != function:
                raise ValueError(
                    f"typed function name '{function.name}' has conflicting definitions"
                )
            self.functions[function.name] = function
        self._requirement_violations: list[VerificationRequirementViolation] = []
        self._cover_witnesses: dict[str, VerificationCoverWitness] = {}

    @property
    def requirement_violations(self) -> tuple[VerificationRequirementViolation, ...]:
        return tuple(self._requirement_violations)

    @property
    def cover_witnesses(self) -> tuple[VerificationCoverWitness, ...]:
        return tuple(self._cover_witnesses.values())

    def sample(
        self,
        values: Mapping[str, object],
        cycle: int,
        reset_active: bool = False,
        *,
        clock: str | None = None,
    ) -> VerificationSampleResult:
        if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 0:
            raise ValueError(
                "verification sample cycle must be a non-negative integer"
            )
        clocks = tuple(dict.fromkeys(scope.clock for scope in self.scopes))
        if clock is None:
            if len(clocks) > 1:
                raise ValueError(
                    "multi-domain verification sampling requires an explicit clock"
                )
            selected_scopes = self.scopes
        else:
            if clock not in clocks:
                raise ValueError(f"verification monitor has no clock '{clock}'")
            selected_scopes = tuple(
                scope for scope in self.scopes if scope.clock == clock
            )
        if reset_active:
            return VerificationSampleResult(cycle=cycle, reset_suppressed=True)

        active: list[str] = []
        violations: list[VerificationRequirementViolation] = []
        witnesses: list[VerificationCoverWitness] = []
        for scope in selected_scopes:
            requirements_hold = True
            for requirement in scope.requirements:
                if self._evaluate_bit(
                    requirement.expression,
                    values,
                    f"requirement '{scope.name}.{requirement.name}'",
                    requirement.source_origin,
                ):
                    continue
                requirements_hold = False
                violation = VerificationRequirementViolation(
                    scope.semantic_id,
                    scope.name,
                    requirement.semantic_id,
                    requirement.name,
                    cycle,
                    requirement.source_origin,
                )
                self._requirement_violations.append(violation)
                violations.append(violation)
            if not requirements_hold:
                continue
            active.append(scope.semantic_id)
            for goal in scope.goals:
                holds = self._evaluate_bit(
                    goal.expression,
                    values,
                    f"goal '{scope.name}.{goal.name}'",
                    goal.source_origin,
                )
                if goal.kind is VerificationGoalKind.COVER:
                    if holds and goal.semantic_id not in self._cover_witnesses:
                        witness = VerificationCoverWitness(
                            scope.semantic_id,
                            scope.name,
                            goal.semantic_id,
                            goal.name,
                            cycle,
                            goal.source_origin,
                        )
                        self._cover_witnesses[goal.semantic_id] = witness
                        witnesses.append(witness)
                elif not holds:
                    raise VerificationAssertionError(scope, goal, cycle)
        return VerificationSampleResult(
            cycle,
            False,
            tuple(active),
            tuple(violations),
            tuple(witnesses),
        )

    def _evaluate_bit(
        self,
        expression: expr.Expression,
        values: Mapping[str, object],
        label: str,
        origin: SourceOrigin | None,
    ) -> bool:
        try:
            value = _evaluate(expression, values, self.functions)
        except KeyError as error:
            raise SimulationError(
                f"verification {label} cannot be sampled: missing value "
                f"'{error.args[0]}'{verification_origin_text(origin)}"
            ) from error
        except SimulationError as error:
            raise SimulationError(
                f"verification {label} cannot be sampled: {error}"
                f"{verification_origin_text(origin)}"
            ) from error
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise SimulationError(
            f"verification {label} produced non-bit value {value!r}"
            f"{verification_origin_text(origin)}"
        )


__all__ = ["VerificationMonitor", "VerificationSampleResult"]
