# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned scalar implementation intent analysis.

This module owns source-policy validation, candidate-site construction, and
explicit implementation-choice typing.  It does not select target resources
or depend on backend representations.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import TYPE_CHECKING

from zlang.ast import nodes as ast
from zlang import exploration as exploration
from zlang.implementation_request import MAXIMIZABLE_METRICS, MINIMIZABLE_METRICS
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir import pipelines as ir_pipelines
from zlang.ir.traversal import walk_expression
from zlang.ir.types import HardwareType, SIntType, UIntType
from zlang.pipelines import pipeline_constraints_from_unified
from zlang.source import SourceOrigin

from . import callables, expression_domains, expression_origins, expression_timing
from .errors import SemanticError
from .expression_support import _inline_semantic_locals

if TYPE_CHECKING:
    from .context import ExpressionContext


_DEFAULT_TRANSFORMS = (
    exploration.TransformFamily.MULTIPLIER,
    exploration.TransformFamily.DSP,
    exploration.TransformFamily.REDUCTION,
)


@dataclass(frozen=True)
class ImplementationAnalysisProduct:
    """One selected typed expression and optional retained pipeline site."""

    expression: ir_expr.Expression
    pipeline_exploration: ir_pipelines.PipelineExploration | None


class ImplementationIntentAnalyzer:
    """Own canonical ``implement`` and explicit ``choice`` semantics."""

    def analyze_site(
        self,
        syntax: ast.ImplementExpr,
        *,
        target: ir_module.Port,
        target_type: HardwareType,
        inputs: Mapping[str, object],
        locals_: tuple[ir_module.LocalValue, ...],
        functions: tuple[ir_module.Function, ...],
        symbols: Mapping[str, object],
        resource_symbols: Mapping[str, object],
        register_symbols: Mapping[str, ir_module.Register],
        context: ExpressionContext,
        equivalences: tuple[ir_module.EquivalenceRule, ...],
        formal_config: object | None,
        formal_verifier: object | None,
        candidate_site_owner: str,
        exploration_results: list[object] | None,
    ) -> ImplementationAnalysisProduct:
        if self._contains_nested_site(syntax.expression):
            raise SemanticError("nested implementation selection is not supported")

        operand = context.expressions.check(
            syntax.expression,
            inputs,
            target_type,
            context,
        )
        # Immutable locals are aliases, not candidate boundaries.  Preserve
        # their source origins while presenting one closed typed expression to
        # compiler-owned candidate discovery.
        operand = _inline_semantic_locals(operand, locals_)
        operand = callables._expand_analysis_calls(
            operand,
            context,
            purpose="exploration operand",
            functions=functions,
        )

        implementation_domains = {
            item
            for item in expression_domains.expression_domains(
                operand,
                {**symbols, **resource_symbols},
                register_symbols,
            )
            if item is not None
        }
        implementation_domain = target.domain
        if implementation_domain is None and len(implementation_domains) == 1:
            implementation_domain = next(iter(implementation_domains))
        if implementation_domain is None:
            implementation_domain = context.environment.default_clock_domain
        if implementation_domains - {implementation_domain}:
            foreign = sorted(implementation_domains - {implementation_domain})[0]
            raise SemanticError(
                f"implementation region in '{implementation_domain}' reads "
                f"dynamic value from '{foreign}'; implementation planning "
                "cannot cross clock domains",
                code="ZL-DOMAIN-CROSSING",
                primary=expression_origins.semantic_origin(syntax, context),
                fixes=("insert an explicit supported clock-domain crossing first",),
            )

        self._validate_objective(syntax.objective)
        objective_metric = self._objective_metric(syntax.objective)
        allowed = self._default_transforms(syntax, context)
        constraints = exploration.constraints_from_syntax(syntax.constraints)
        try:
            result = exploration.explore(
                exploration.ExplorationRequest(
                    operand,
                    allowed,
                    (),
                    constraints,
                    objective_metric,
                    source_origin=syntax.origin,
                    equivalences=equivalences,
                    formal_config=formal_config,
                    formal_verifier=formal_verifier,
                ),
                exploration.ExplorationContext(
                    target.name,
                    target_type,
                    context.allocate_delay,
                    candidate_site_owner,
                    "implement",
                    implementation_domain,
                ),
            )
        except exploration.ExplorationSelectionError as error:
            raise SemanticError(
                str(error),
                code="ZL-IMPLEMENT-CONSTRAINTS",
                primary=expression_origins.semantic_origin(syntax, context),
                notes=(
                    "candidate summaries are structural and omit typed "
                    "expression trees",
                    "hard implementation constraints were not relaxed",
                ),
            ) from error
        except ValueError as error:
            raise SemanticError(str(error)) from error

        if exploration_results is not None:
            exploration_results.append(result)
        pipeline = self._pipeline_product(
            syntax,
            target,
            target_type,
            operand,
            result,
            context,
        )
        expression = result.selected.expression
        origin = expression_origins.semantic_origin(syntax, context)
        if origin is not None:
            expression = replace(expression, origin=origin)
        return ImplementationAnalysisProduct(expression, pipeline)

    def analyze_choice(
        self,
        syntax: ast.ImplementationChoiceExpr,
        inputs: Mapping[str, object],
        expected: HardwareType | None,
        context: ExpressionContext,
    ) -> ir_expr.ImplementationChoice:
        if len(syntax.alternatives) < 2:
            raise SemanticError(
                "implementation choice requires at least two alternatives"
            )
        kinds = [alternative.kind for alternative in syntax.alternatives]
        if len(kinds) != len(set(kinds)):
            duplicate = next(kind for kind in kinds if kinds.count(kind) > 1)
            raise SemanticError(
                f"implementation choice repeats '{duplicate.value}' alternative"
            )
        if syntax.selected is not None and syntax.selected not in kinds:
            raise SemanticError(
                f"selected implementation '{syntax.selected.value}' has no alternative"
            )
        if syntax.cost_policy is not None:
            metrics = [
                constraint.metric for constraint in syntax.cost_policy.constraints
            ]
            if len(metrics) != len(set(metrics)):
                duplicate = next(
                    metric for metric in metrics if metrics.count(metric) > 1
                )
                raise SemanticError(
                    f"cost policy repeats '{duplicate.value}' constraint"
                )

        alternatives: list[ir_expr.ImplementationAlternative] = []
        computations: list[ir_expr.Expression] = []
        result_type = expected
        for alternative_syntax in syntax.alternatives:
            expression = context.expressions.check(
                alternative_syntax.expression,
                inputs,
                result_type,
                context,
            )
            if result_type is None:
                result_type = expression.type
            if expression.type != result_type:
                raise SemanticError(
                    f"implementation '{alternative_syntax.kind.value}' has type "
                    f"{expression.type}, expected {result_type}"
                )
            computation, multiply, addend = self._mac_shape(
                expression,
                alternative_syntax.kind.value,
            )
            self._require_pure_value(computation, alternative_syntax.kind.value)
            latency = expression_timing.expression_latency(expression) or 0
            kind = ir_expr.ImplementationKind(alternative_syntax.kind.value)
            resource = (
                ir_expr.ImplementationResource.DSP
                if kind is ir_expr.ImplementationKind.DSP_MAC
                else ir_expr.ImplementationResource.LOGIC
            )
            applicability = ir_expr.ImplementationApplicability(
                operation="multiply_add",
                conditions=(
                    "integer_scalar_operands",
                    "full_precision_multiply",
                    "single_addend",
                    "initiation_interval_1",
                ),
                multiplier_left_type=multiply.left.type,
                multiplier_right_type=multiply.right.type,
                addend_type=addend.type,
                result_type=expression.type,
                resource_hint=resource,
            )
            semantics = ir_expr.ImplementationSemantics(
                result_type=expression.type,
                latency=latency,
                initiation_interval=1,
                protocol_events=(),
            )
            alternatives.append(
                ir_expr.ImplementationAlternative(
                    kind,
                    expression,
                    applicability,
                    semantics,
                )
            )
            computations.append(computation)

        assert result_type is not None
        if any(computation != computations[0] for computation in computations[1:]):
            raise SemanticError(
                "implementation alternatives are not mathematically equivalent; "
                "multiply-add architectures require identical typed computations"
            )
        latencies = {
            alternative.kind.value: alternative.semantics.latency
            for alternative in alternatives
        }
        if len(set(latencies.values())) != 1:
            rendered = ", ".join(
                f"{kind}={latency}" for kind, latency in sorted(latencies.items())
            )
            raise SemanticError(
                "implementation choice latency mismatch: "
                f"{rendered}; alternatives are mathematical but not cycle-accurate "
                "equivalents",
                code="ZL-TIMING-MISMATCH",
            )
        return ir_expr.ImplementationChoice(
            (
                ir_expr.ImplementationKind(syntax.selected.value)
                if syntax.selected is not None
                else None
            ),
            tuple(alternatives),
            (
                ir_expr.ImplementationEquivalence.MATHEMATICAL,
                ir_expr.ImplementationEquivalence.CYCLE_ACCURATE,
            ),
            result_type,
            (
                ir_expr.CostPolicy(
                    ir_expr.CostMetric(syntax.cost_policy.goal.value),
                    tuple(
                        ir_expr.CostConstraint(
                            ir_expr.CostMetric(constraint.metric.value),
                            constraint.maximum,
                        )
                        for constraint in syntax.cost_policy.constraints
                    ),
                    (
                        ir_expr.SynthesisFeedback(
                            syntax.cost_policy.feedback.value
                        )
                        if syntax.cost_policy.feedback is not None
                        else None
                    ),
                )
                if syntax.cost_policy is not None
                else None
            ),
        )

    @staticmethod
    def _contains_nested_site(value: object) -> bool:
        if isinstance(value, ast.ImplementExpr):
            return True
        if is_dataclass(value):
            return any(
                ImplementationIntentAnalyzer._contains_nested_site(
                    getattr(value, item.name)
                )
                for item in fields(value)
                if item.name != "origin"
            )
        if isinstance(value, tuple):
            return any(
                ImplementationIntentAnalyzer._contains_nested_site(item)
                for item in value
            )
        return False

    @staticmethod
    def _objective_metric(
        objective: ast.ExplorationObjective | None,
    ) -> ir_expr.CostMetric:
        if objective is None:
            return ir_expr.CostMetric.LUT
        return (
            ir_expr.CostMetric.FMAX_EST
            if objective.metric.value == "fmax_est"
            else ir_expr.CostMetric(objective.metric.value)
        )

    @classmethod
    def _validate_objective(
        cls,
        objective: ast.ExplorationObjective | None,
    ) -> None:
        metric = cls._objective_metric(objective)
        if objective is None:
            return
        if objective.direction == "maximize":
            if metric not in MAXIMIZABLE_METRICS:
                raise SemanticError("maximize currently supports only fmax (fmax_est)")
            return
        if objective.direction != "minimize":
            raise SemanticError(
                "implementation objective direction must be 'minimize' or 'maximize'"
            )
        if metric not in MINIMIZABLE_METRICS:
            raise SemanticError(
                "minimize currently supports only lut, ff, dsp, bram, or latency"
            )

    @staticmethod
    def _permits_nonzero_latency(
        constraints: Iterable[ast.ExplorationConstraint],
    ) -> bool:
        return any(
            constraint.metric is ast.CostMetric.LATENCY
            and constraint.relation
            in {
                ast.ExplorationRelation.MAXIMUM,
                ast.ExplorationRelation.EXACT,
                ast.ExplorationRelation.MINIMUM,
            }
            and constraint.value > 0
            for constraint in constraints
        )

    @classmethod
    def _default_transforms(
        cls,
        expression: ast.ImplementExpr,
        context: ExpressionContext,
    ) -> tuple[exploration.TransformFamily, ...]:
        transforms = list(_DEFAULT_TRANSFORMS)
        permits_latency = cls._permits_nonzero_latency(expression.constraints)
        if permits_latency and context.scope.allow_delay:
            transforms.append(exploration.TransformFamily.PIPELINE)
        elif permits_latency:
            raise SemanticError(
                "implement requires a module clock and reset before a "
                "positive-latency implementation can be selected"
            )
        return tuple(transforms)

    @staticmethod
    def _pipeline_product(
        syntax: ast.ImplementExpr,
        target: ir_module.Port,
        target_type: HardwareType,
        operand: ir_expr.Expression,
        result: object,
        context: ExpressionContext,
    ) -> ir_pipelines.PipelineExploration | None:
        candidates = result.candidates
        selected_pipeline = next(
            (
                candidate.architecture
                for candidate in candidates
                if candidate.implementation_identity
                == result.selected.implementation_identity
                and isinstance(candidate.architecture, ir_pipelines.PipelineCandidate)
            ),
            None,
        )
        pipeline_by_name: dict[str, ir_pipelines.PipelineCandidate] = {}
        for candidate in candidates:
            if isinstance(candidate.architecture, ir_pipelines.PipelineCandidate):
                pipeline_by_name.setdefault(
                    candidate.architecture.name,
                    candidate.architecture,
                )
        if isinstance(selected_pipeline, ir_pipelines.PipelineCandidate):
            pipeline_by_name[selected_pipeline.name] = selected_pipeline
        pipeline_candidates = tuple(
            pipeline_by_name[name] for name in sorted(pipeline_by_name)
        )
        if not pipeline_candidates:
            return None
        selected_name = (
            selected_pipeline.name
            if isinstance(selected_pipeline, ir_pipelines.PipelineCandidate)
            else pipeline_candidates[0].name
        )
        pipeline_operand = operand
        if syntax.origin is not None:
            pipeline_operand = replace(
                pipeline_operand,
                origin=SourceOrigin(
                    syntax.origin,
                    "implement",
                    context.scope.source_unit,
                    context.scope.source_digest,
                ),
            )
        return ir_pipelines.PipelineExploration(
            target.name,
            target_type,
            pipeline_operand,
            pipeline_constraints_from_unified(
                exploration.constraints_from_syntax(syntax.constraints)
            ),
            pipeline_candidates,
            selected_name,
            len(pipeline_candidates),
        )

    @staticmethod
    def _mac_shape(
        expression: ir_expr.Expression,
        kind: str,
    ) -> tuple[ir_expr.Expression, ir_expr.Binary, ir_expr.Expression]:
        computation = (
            expression.expression
            if isinstance(expression, ir_expr.Pipeline)
            else expression
        )
        if not isinstance(computation, ir_expr.Add):
            raise SemanticError(
                f"implementation '{kind}' requires one multiply-add expression"
            )
        products = tuple(
            (operand, other)
            for operand, other in (
                (computation.left, computation.right),
                (computation.right, computation.left),
            )
            if isinstance(operand, ir_expr.Binary)
            and operand.operator is ir_expr.BinaryOperator.MULTIPLY
        )
        if len(products) != 1:
            raise SemanticError(
                f"implementation '{kind}' requires exactly one full-precision "
                "multiply and one addend"
            )
        multiply, addend = products[0]
        if not all(
            isinstance(type_, (UIntType, SIntType))
            for type_ in (
                multiply.left.type,
                multiply.right.type,
                addend.type,
                computation.type,
            )
        ):
            raise SemanticError(
                f"implementation '{kind}' requires scalar integer multiply-add types"
            )
        return computation, multiply, addend

    @classmethod
    def _require_pure_value(
        cls,
        expression: ir_expr.Expression,
        kind: str,
    ) -> None:
        allowed = (
            ir_expr.InputRef,
            ir_expr.ParameterRef,
            ir_expr.Constant,
            ir_expr.Add,
            ir_expr.Binary,
            ir_expr.Extend,
            ir_expr.Truncate,
            ir_expr.FixedConvert,
            ir_expr.FieldAccess,
            ir_expr.TupleProject,
            ir_expr.VectorIndex,
            ir_expr.Slice,
            ir_expr.Bitcast,
            ir_expr.Reshape,
            ir_expr.Pack,
            ir_expr.Unpack,
            ir_expr.Concat,
            ir_expr.VectorConcat,
            ir_expr.RuntimeIndex,
            ir_expr.VectorUpdate,
            ir_expr.Mux,
            ir_expr.Switch,
            ir_expr.Call,
        )
        protocol_state = (
            ir_expr.ReadyValidRef,
            ir_expr.CreditRef,
            ir_expr.PacketRef,
            ir_expr.VirtualChannelCreditRef,
            ir_expr.RequestResponseRef,
        )
        for value in walk_expression(expression, deduplicate=False):
            if isinstance(value, allowed):
                continue
            if isinstance(value, protocol_state):
                raise SemanticError(
                    f"implementation '{kind}' depends on protocol state; protocol/"
                    "observational alternatives are not supported"
                )
            if isinstance(value, ir_expr.RegisterRef):
                raise SemanticError(
                    f"implementation '{kind}' depends on sequential state; only an "
                    "explicit outer pipeline is supported"
                )
            if isinstance(value, (ir_expr.FifoRef, ir_expr.MemoryRef, ir_expr.RomRef)):
                raise SemanticError(
                    f"implementation '{kind}' depends on an architectural storage value"
                )
            raise SemanticError(
                f"implementation '{kind}' contains unsupported nested timing or "
                f"architecture node {type(value).__name__}"
            )
