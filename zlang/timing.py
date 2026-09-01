"""M30 timing-aware value relations, kept separate from value equality."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from hashlib import sha256
from typing import Iterable

from zlang.ir import expressions as expr
from zlang.ir.traversal import expression_children as typed_expression_children


@dataclass(frozen=True)
class TimingInfo:
    latency: int = 0
    ii: int = 1
    clock_domain: str | None = None
    reset_domain: str | None = None

    def __post_init__(self) -> None:
        if self.latency < 0 or self.ii < 1:
            raise ValueError("latency must be non-negative and II must be positive")


class TimingRelationKind(str, Enum):
    SAME_CYCLE = "same_cycle"
    TIMED_EQUIVALENT = "timed_equivalent"
    NOT_EQUIVALENT = "not_equivalent"
    INCOMPATIBLE_II = "incompatible_ii"
    INCOMPATIBLE_CLOCK = "incompatible_clock"
    INCOMPATIBLE_RESET = "incompatible_reset"
    INCOMPATIBLE_VALUE = "incompatible_value"


@dataclass(frozen=True)
class TimingRelation:
    kind: TimingRelationKind
    earlier: object | None
    later: object | None
    delta: int | None
    proof: str

    @property
    def equivalent(self) -> bool:
        return self.kind in {TimingRelationKind.SAME_CYCLE, TimingRelationKind.TIMED_EQUIVALENT}


@dataclass(frozen=True)
class AlignmentPlan:
    target_latency: int
    adjustments: tuple[int, ...]
    reason: str


def timing_info(
    value: expr.Expression,
    *,
    module=None,
    clock_domain=None,
    reset_domain=None,
    ii=1,
) -> TimingInfo:
    """Compute existing ZLang latency without changing compilation semantics."""
    return TimingInfo(_latency(value, module=module), ii, clock_domain, reset_domain)


def relate_timing(lhs, rhs, lhs_timing: TimingInfo | None = None,
                  rhs_timing: TimingInfo | None = None) -> TimingRelation:
    left = lhs_timing or timing_info(lhs)
    right = rhs_timing or timing_info(rhs)
    if lhs.type != rhs.type:
        return TimingRelation(TimingRelationKind.INCOMPATIBLE_VALUE, None, None, None, "canonical types differ")
    if left.ii != right.ii:
        return TimingRelation(TimingRelationKind.INCOMPATIBLE_II, None, None, None, "initiation intervals differ")
    if left.clock_domain != right.clock_domain:
        return TimingRelation(TimingRelationKind.INCOMPATIBLE_CLOCK, None, None, None, "clock domains differ")
    if left.reset_domain != right.reset_domain:
        return TimingRelation(TimingRelationKind.INCOMPATIBLE_RESET, None, None, None, "reset domains differ")
    if _value_identity(lhs) != _value_identity(rhs):
        return TimingRelation(TimingRelationKind.INCOMPATIBLE_VALUE, None, None, None, "value semantics are not proven equivalent")
    if left.latency == right.latency:
        return TimingRelation(TimingRelationKind.SAME_CYCLE, lhs, rhs, 0, "same-cycle value equivalence")
    if left.latency < right.latency:
        return TimingRelation(TimingRelationKind.TIMED_EQUIVALENT, lhs, rhs, right.latency - left.latency, _proof(lhs, rhs))
    return TimingRelation(TimingRelationKind.TIMED_EQUIVALENT, rhs, lhs, left.latency - right.latency, _proof(rhs, lhs))


def validate_timed_candidate(original, candidate, *, original_timing=None,
                             candidate_timing=None, value_equivalent=False) -> TimingRelation:
    """Validate a generated candidate without merging it into value equality."""
    left = original_timing or timing_info(original)
    right = candidate_timing or timing_info(candidate)
    if original.type != candidate.type:
        return TimingRelation(TimingRelationKind.INCOMPATIBLE_VALUE, None, None, None, "candidate type differs")
    if left.ii != right.ii:
        return TimingRelation(TimingRelationKind.INCOMPATIBLE_II, None, None, None, "candidate II differs")
    if left.clock_domain != right.clock_domain:
        return TimingRelation(TimingRelationKind.INCOMPATIBLE_CLOCK, None, None, None, "candidate clock domain differs")
    if left.reset_domain != right.reset_domain:
        return TimingRelation(TimingRelationKind.INCOMPATIBLE_RESET, None, None, None, "candidate reset domain differs")
    if not value_equivalent and _value_identity(original) != _value_identity(candidate):
        return TimingRelation(TimingRelationKind.INCOMPATIBLE_VALUE, None, None, None, "candidate value semantics are unproven")
    delta = right.latency - left.latency
    if delta == 0:
        return TimingRelation(TimingRelationKind.SAME_CYCLE, original, candidate, 0, "validated candidate")
    if delta > 0:
        return TimingRelation(TimingRelationKind.TIMED_EQUIVALENT, original, candidate, delta, "generated_pipeline_candidate")
    return TimingRelation(TimingRelationKind.TIMED_EQUIVALENT, candidate, original, -delta, "generated_pipeline_candidate")


def compare_timing(lhs: TimingInfo, rhs: TimingInfo) -> TimingRelation:
    lhs = _as_timing(lhs)
    rhs = _as_timing(rhs)
    if lhs.ii != rhs.ii:
        return TimingRelation(TimingRelationKind.INCOMPATIBLE_II, None, None, None, "initiation intervals differ")
    if lhs.clock_domain != rhs.clock_domain:
        return TimingRelation(TimingRelationKind.INCOMPATIBLE_CLOCK, None, None, None, "clock domains differ")
    if lhs.reset_domain != rhs.reset_domain:
        return TimingRelation(TimingRelationKind.INCOMPATIBLE_RESET, None, None, None, "reset domains differ")
    delta = abs(lhs.latency - rhs.latency)
    return TimingRelation(TimingRelationKind.SAME_CYCLE if delta == 0 else TimingRelationKind.TIMED_EQUIVALENT,
                          None, None, delta, "timing contract comparison")


def _as_timing(value) -> TimingInfo:
    if isinstance(value, TimingInfo):
        return value
    return TimingInfo(value.latency, value.initiation_interval,
                      getattr(value, "clock_domain", None),
                      getattr(value, "reset_domain", None))


def align_operands(operands: Iterable[expr.Expression], *, target_latency: int | None = None,
                   clock_domain=None, reset_domain=None, ii=1) -> AlignmentPlan:
    values = tuple(operands)
    infos = tuple(timing_info(item, clock_domain=clock_domain, reset_domain=reset_domain, ii=ii) for item in values)
    if any(item.ii != infos[0].ii for item in infos) or any(item.clock_domain != infos[0].clock_domain for item in infos):
        raise ValueError("operands have incompatible timing domains or initiation intervals")
    target = max(item.latency for item in infos) if target_latency is None else target_latency
    if target < max(item.latency for item in infos):
        raise ValueError("alignment target is earlier than the latest operand")
    return AlignmentPlan(target, tuple(target - item.latency for item in infos), "minimum-latency alignment")


class TimedEquivalenceGraph:
    """Separate timing graph; it never unions ordinary value e-classes."""

    def __init__(self) -> None:
        self._edges: dict[tuple[str, str], int] = {}

    def add(self, relation: TimingRelation) -> None:
        if not relation.equivalent or relation.earlier is None or relation.later is None:
            raise ValueError(f"cannot add non-equivalence relation: {relation.proof}")
        delta = relation.delta or 0
        key = (_timed_key(relation.earlier), _timed_key(relation.later))
        previous = self._edges.get(key)
        if previous is not None and previous != delta:
            raise ValueError("contradictory timed-equivalence relation")
        self._edges[key] = delta

    def compose(self, first: TimingRelation, second: TimingRelation) -> TimingRelation:
        if first.later is None or second.earlier is None or _timed_key(first.later) != _timed_key(second.earlier):
            raise ValueError("timed relations do not compose")
        return TimingRelation(TimingRelationKind.TIMED_EQUIVALENT, first.earlier, second.later,
                              (first.delta or 0) + (second.delta or 0), "composition")


def _latency(value: expr.Expression, *, module=None) -> int:
    """Return the exact structural latency embedded in a typed value graph.

    M30 originally only inspected a timing node at the expression root.  That
    made a conversion or arithmetic node around ``delay``/``pipeline`` erase
    the latency, and it also meant target graphs silently reported zero.  Walk
    the already-typed expression graph instead.  Alignment legality remains a
    semantic-analysis concern; taking the maximum here merely reports the
    latency of a graph that has already passed that validation.

    Instance outputs use the module-published timing record when a module is
    available.  The optional context keeps the historical public API usable by
    the M30 value-equivalence helpers.
    """
    if isinstance(value, expr.Delay):
        return _latency(value.expression, module=module) + value.cycles
    if isinstance(value, expr.Pipeline):
        return _latency(value.expression, module=module) + value.stages
    if isinstance(value, expr.InstanceOutputRef) and module is not None:
        matches = tuple(
            item.timing
            for item in getattr(module, "instance_output_timings", ())
            if item.instance == value.instance and item.port == value.port
        )
        if len(matches) == 1:
            timing = matches[0]
            if getattr(timing.knowledge, "value", timing.knowledge) == "known":
                assert timing.latency is not None
                return timing.latency
            # Timeless values impose no cycle of their own.  Unknown values
            # cannot be represented by the legacy integer-only TimingInfo;
            # semantic module timing records retain that distinction.
            return 0

    children = tuple(_expression_children(value))
    return max((_latency(child, module=module) for child in children), default=0)


def _expression_children(value: expr.Expression):
    """Yield direct typed-expression children without depending on AST shape."""
    yield from typed_expression_children(value)


def _strip_timing(value):
    while isinstance(value, (expr.Delay, expr.Pipeline)):
        value = value.expression
    return value


def _value_identity(value) -> str:
    def normalize(item):
        item = _strip_timing(item)
        if isinstance(item, expr.Binary) and item.operator in {
            expr.BinaryOperator.BIT_OR, expr.BinaryOperator.BIT_XOR,
            expr.BinaryOperator.SHIFT_LEFT, expr.BinaryOperator.SHIFT_RIGHT,
        } and isinstance(item.right, expr.Constant) and item.right.value == 0:
            return normalize(item.left)
        if is_dataclass(item):
            return (type(item).__name__, tuple(
                (field.name, normalize(getattr(item, field.name)))
                for field in fields(item)
                if field.name not in {"origin", "source_origin"}
            ))
        if isinstance(item, tuple):
            return tuple(normalize(child) for child in item)
        if isinstance(item, (str, int, float, type(None), Enum)):
            return item.value if isinstance(item, Enum) else item
        return repr(item)
    return sha256(repr(normalize(value)).encode()).hexdigest()


def _timed_key(value) -> str:
    return f"{_value_identity(value)}@{_latency(value)}"


def _proof(lhs, rhs) -> str:
    if isinstance(rhs, expr.Delay) or isinstance(rhs, expr.Pipeline):
        return "explicit_delay" if isinstance(rhs, expr.Delay) else "fixed_pipeline"
    return "value_equivalence_lift"
