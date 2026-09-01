"""Backend-independent architectural alternatives for proven value expressions.

Architecture candidates are intentionally outside the M26/M27 value e-graph.
They reference one exact semantic expression and preserve its timing contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from typing import Any

from zlang.ir import expressions as expr
from zlang.ir.types import BitsType, SIntType, UIntType
from zlang.timing import timing_info


class ArchitectureImplementation(str, Enum):
    GENERIC = "generic"
    GENERIC_MUL_ADD = "generic"
    MAC = "mac"
    DSP_MAC = "dsp_mac"


class ResourceIntent(str, Enum):
    GENERIC_LOGIC = "generic_logic"
    DEDICATED_DSP = "dedicated_dsp"


@dataclass(frozen=True)
class TimingContract:
    latency: int
    initiation_interval: int
    clock_domain: str | None = None
    reset_domain: str | None = None


@dataclass(frozen=True)
class ArchitectureLegality:
    legal: bool
    reason: str


@dataclass(frozen=True)
class ArchitectureCandidate:
    value_root: expr.Expression
    implementation: ArchitectureImplementation
    parameters: tuple[tuple[str, Any], ...]
    timing_contract: TimingContract
    resource_intent: ResourceIntent
    source_origin: object | None = None
    legality: ArchitectureLegality = ArchitectureLegality(True, "exact value/type/timing match")

    @property
    def legal(self) -> bool:
        return self.legality.legal

    @property
    def identity(self) -> str:
        payload = repr((self.value_root, self.implementation.value,
                        self.parameters, self.timing_contract))
        return sha256(payload.encode()).hexdigest()

    @property
    def name(self) -> str:
        return self.implementation.value


def architecture_candidate_hash(candidate: ArchitectureCandidate) -> str:
    """Stable measurement identity including architecture intent."""
    return candidate.identity


def expand_architectures(value_root: expr.Expression, context: object | None = None) -> tuple[ArchitectureCandidate, ...]:
    """Expand one normalized semantic value into deterministic candidates.

    M29 recognizes only the local scalar ``Add(Mul(a,b), c)`` shape. Unsupported
    values receive the original generic candidate and no invented architecture.
    """
    timing = TimingContract(_expression_latency(value_root), 1)
    shape = _mul_add_shape(value_root)
    if shape is None:
        return (_candidate(value_root, ArchitectureImplementation.GENERIC,
                           timing, ResourceIntent.GENERIC_LOGIC, context),)
    multiply, addend = shape
    if not _supported_scalar(multiply.left.type, multiply.right.type, addend.type):
        return (_candidate(value_root, ArchitectureImplementation.GENERIC,
                           timing, ResourceIntent.GENERIC_LOGIC, context),)
    params = (("lhs_type", multiply.left.type), ("rhs_type", multiply.right.type),
              ("addend_type", addend.type), ("result_type", value_root.type))
    return tuple(
        _candidate(value_root, kind, timing, intent, context, params)
        for kind, intent in (
            (ArchitectureImplementation.GENERIC, ResourceIntent.GENERIC_LOGIC),
            (ArchitectureImplementation.MAC, ResourceIntent.GENERIC_LOGIC),
            (ArchitectureImplementation.DSP_MAC, ResourceIntent.DEDICATED_DSP),
        )
    )


def _candidate(root, implementation, timing, intent, context, parameters=()):
    return ArchitectureCandidate(root, implementation, parameters, timing, intent,
                                 getattr(root, "origin", None) if context is None else context)


def _mul_add_shape(value: expr.Expression):
    if not isinstance(value, expr.Add):
        return None
    if isinstance(value.left, expr.Binary) and value.left.operator is expr.BinaryOperator.MULTIPLY:
        return value.left, value.right
    if isinstance(value.right, expr.Binary) and value.right.operator is expr.BinaryOperator.MULTIPLY:
        return value.right, value.left
    return None


def _supported_scalar(left, right, addend) -> bool:
    scalar = (UIntType, SIntType)
    return isinstance(left, scalar) and type(left) is type(right) is type(addend)


def _expression_latency(value: expr.Expression) -> int:
    # Pipeline uses the canonical ``stages`` field.  Delegate to the shared
    # recursive M30 traversal so conversions/arithmetic wrapped around a fixed
    # pipeline retain their exact structural latency as well.
    return timing_info(value).latency


def architecture_cost(candidate: ArchitectureCandidate):
    """Return a M28 CandidateCost using only structural estimates."""
    from zlang.costs import CandidateCost
    width = candidate.value_root.type.width
    multiply = _mul_add_shape(candidate.value_root)
    if multiply is None:
        lut = width
    else:
        product = multiply[0]
        lut = product.left.type.width * product.right.type.width + width
    dsp = 1 if candidate.implementation is ArchitectureImplementation.DSP_MAC else 0
    if candidate.implementation is ArchitectureImplementation.DSP_MAC:
        lut = width
    return CandidateCost.estimate(lut=lut, dsp=dsp,
                                  latency=candidate.timing_contract.latency,
                                  ii=candidate.timing_contract.initiation_interval,
                                  structural_cost=1 + (0 if candidate.implementation is ArchitectureImplementation.GENERIC_MUL_ADD else 1))


def extract_best_architecture(candidates, objective="lut", constraints=(), source_policy="estimate_only"):
    from zlang.costs import extract_best
    return extract_best(candidates, objective, constraints, source_policy,
                        cost_fn=architecture_cost)


def architecture_candidates_for_choice(choice: expr.ImplementationChoice) -> tuple[ArchitectureCandidate, ...]:
    """Adapt legacy ``choice`` alternatives to the M29 candidate model."""
    candidates: list[ArchitectureCandidate] = []
    for alternative in choice.alternatives:
        implementation = {
            expr.ImplementationKind.MUL_ADD: ArchitectureImplementation.GENERIC,
            expr.ImplementationKind.DSP_MAC: ArchitectureImplementation.DSP_MAC,
        }.get(alternative.kind)
        if implementation is None:
            continue
        timing = TimingContract(alternative.semantics.latency,
                                alternative.semantics.initiation_interval)
        candidates.append(ArchitectureCandidate(
            alternative.expression, implementation, (), timing,
            ResourceIntent.DEDICATED_DSP if implementation is ArchitectureImplementation.DSP_MAC else ResourceIntent.GENERIC_LOGIC,
            alternative.expression.origin,
        ))
    return tuple(candidates)


def expand_reduction_architectures(value_root, search=None):
    """M32 generic reduction entry point kept beside M29 architecture APIs."""
    from zlang.reductions import ReductionSearch, expand_reduction
    return expand_reduction(value_root, search or ReductionSearch())
