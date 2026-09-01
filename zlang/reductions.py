"""Generic, bounded reduction architecture expansion for M32."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256

from zlang.ir import expressions as expr
from zlang.ir.functional import collection_elements
from zlang.ir.types import HardwareType, SIntType, StructType, UIntType
from zlang.timing import TimingInfo


class ReductionTopology(str, Enum):
    ORIGINAL = "original"
    LINEAR = "linear"
    BALANCED = "balanced"
    LANE_GROUPED = "lane_grouped"


@dataclass(frozen=True)
class ReductionSemantics:
    operator: str
    count: int
    element_type: HardwareType
    accumulator_type: HardwareType
    result_type: HardwareType
    ordering: str = "source_order"


@dataclass(frozen=True)
class ReductionSearch:
    max_candidates: int = 8
    max_topologies: int = 8
    max_lane_counts: int = 4
    parallelism: int | None = None
    depth: int | None = None


@dataclass(frozen=True)
class ReductionLegality:
    legal: bool
    reason: str


@dataclass(frozen=True)
class ReductionArchitectureCandidate:
    semantics: ReductionSemantics
    topology: ReductionTopology
    parameters: tuple[tuple[str, int], ...]
    expression: expr.Expression
    reduction_depth: int
    parallelism: int
    timing: TimingInfo
    implementation_policy: str = "generic"
    legality: ReductionLegality = ReductionLegality(True, "exact canonical reduction semantics")

    @property
    def legal(self) -> bool:
        return self.legality.legal

    @property
    def identity(self) -> str:
        payload = repr((self.semantics, self.topology.value, self.parameters,
                        self.implementation_policy, self.timing))
        return sha256(payload.encode()).hexdigest()

    @property
    def semantic_identity(self) -> str:
        return sha256(repr((self.semantics, self.semantics.operator,
                            self.semantics.count, self.semantics.element_type,
                            self.semantics.accumulator_type,
                            self.semantics.result_type)).encode()).hexdigest()


class ReductionExplorationError(ValueError):
    pass


def recognize_reduction(value: expr.Expression) -> tuple[ReductionSemantics, tuple[expr.Expression, ...]] | None:
    """Recognize high-level Reduce/Dot and their scalar sum fallback."""
    if isinstance(value, expr.Reduce) and value.operator is expr.ReductionOperator.ADD:
        # Exact-overload nominal reductions retain a frozen balanced expansion.
        # M32 must not reassociate or reinterpret that operator tree.
        if value.expanded is not None or isinstance(value.type, StructType):
            return None
        elements = collection_elements(value.collection)
        if not elements:
            raise ReductionExplorationError("empty reductions are not legal")
        return ReductionSemantics("add", len(elements), elements[0].type,
                                  value.type, value.type), elements
    if isinstance(value, expr.Dot):
        products = tuple(value.products)
        if not products:
            raise ReductionExplorationError("empty dot products are not legal")
        return ReductionSemantics("add", len(products), products[0].type,
                                  value.type, value.type), products
    terms = _flatten_add(value)
    if len(terms) >= 2 and all(hasattr(item, "type") for item in terms):
        return ReductionSemantics("add", len(terms), terms[0].type,
                                  value.type, value.type), terms
    return None


def expand_reduction(value: expr.Expression, search: ReductionSearch = ReductionSearch()) -> tuple[ReductionArchitectureCandidate, ...]:
    recognized = recognize_reduction(value)
    if recognized is None:
        return ()
    semantics, elements = recognized
    if not isinstance(semantics.result_type, (UIntType, SIntType)):
        raise ReductionExplorationError("arithmetic reductions require an integer result")
    if any(type(item.type) is not type(semantics.result_type) for item in elements):
        raise ReductionExplorationError("mixed signedness reduction is not supported")
    candidates: list[ReductionArchitectureCandidate] = []
    specs: list[tuple[ReductionTopology, tuple[tuple[str, int], ...]]] = [
        (ReductionTopology.ORIGINAL, ()),
        (ReductionTopology.LINEAR, ()),
        (ReductionTopology.BALANCED, ()),
    ]
    for lanes in range(2, min(search.max_lane_counts, len(elements)) + 1):
        if len(elements) % lanes == 0:
            specs.append((ReductionTopology.LANE_GROUPED, (("lanes", lanes),)))
    for topology, parameters in specs[: min(search.max_topologies, len(specs))]:
        expression = _build(topology, elements, semantics, dict(parameters))
        depth = _depth(topology, len(elements), dict(parameters))
        parallelism = _parallelism(topology, len(elements), dict(parameters))
        legal = not (search.parallelism is not None and parallelism > search.parallelism)
        reason = "exact canonical reduction semantics" if legal else "parallelism exceeds search bound"
        candidate = ReductionArchitectureCandidate(
            semantics, topology, parameters, expression, depth, parallelism,
            TimingInfo(0, 1), legality=ReductionLegality(legal, reason),
        )
        candidates.append(candidate)
        if topology is ReductionTopology.BALANCED and all(
            isinstance(item, expr.Binary) and item.operator is expr.BinaryOperator.MULTIPLY
            for item in elements
        ):
            candidates.append(ReductionArchitectureCandidate(
                semantics, topology, parameters, expression, depth, parallelism,
                TimingInfo(0, 1), implementation_policy="dsp_preferred",
            ))
    return tuple(candidates[:search.max_candidates])


def extract_best_reduction(candidates, objective="lut", constraints=(), source_policy="estimate_only"):
    from zlang.costs import extract_best
    return extract_best(tuple(item for item in candidates if item.legal), objective, constraints,
                        source_policy, cost_fn=reduction_cost)


def reduction_cost(candidate: ReductionArchitectureCandidate):
    """Return the shared M28 structural estimate for one reduction candidate."""
    from zlang.costs import CandidateCost
    width = candidate.semantics.result_type.width
    lut = candidate.semantics.count * width + candidate.reduction_depth * width
    dsp = (
        candidate.semantics.count
        if candidate.implementation_policy == "dsp_preferred"
        else 0
    )
    if dsp:
        lut = max(width, lut // 2)
    return CandidateCost.estimate(
        lut=lut,
        ff=0,
        dsp=dsp,
        latency=candidate.timing.latency,
        ii=candidate.timing.ii,
        fmax_est=max(1, 800 // max(1, candidate.reduction_depth)),
        structural_cost=candidate.reduction_depth,
    )


def _flatten_add(value):
    if isinstance(value, expr.Add):
        return _flatten_add(value.left) + _flatten_add(value.right)
    return (value,)


def _build(topology, elements, semantics, parameters):
    if topology is ReductionTopology.ORIGINAL:
        return _canonical_linear(elements, semantics)
    if topology is ReductionTopology.LINEAR:
        return _canonical_linear(elements, semantics)
    if topology is ReductionTopology.BALANCED:
        return _canonical_balanced(elements, semantics)
    lanes = parameters["lanes"]
    groups = [tuple(elements[index::lanes]) for index in range(lanes)]
    return _canonical_balanced(tuple(_canonical_balanced(group, semantics) for group in groups), semantics)


def _canonical_linear(elements, semantics):
    result = _cast(elements[0], semantics.accumulator_type)
    for item in elements[1:]:
        result = expr.Add(result, _cast(item, semantics.accumulator_type), semantics.accumulator_type)
    return result


def _canonical_balanced(elements, semantics):
    if len(elements) == 1:
        return _cast(elements[0], semantics.accumulator_type)
    next_level = []
    for index in range(0, len(elements), 2):
        if index + 1 == len(elements):
            next_level.append(elements[index])
        else:
            next_level.append(expr.Add(_cast(elements[index], semantics.accumulator_type),
                                       _cast(elements[index + 1], semantics.accumulator_type),
                                       semantics.accumulator_type))
    return _canonical_balanced(tuple(next_level), semantics)


def _cast(value, type_):
    if value.type == type_:
        return value
    if type(value.type) is not type(type_):
        raise ReductionExplorationError("reduction candidate changes signedness")
    return expr.Extend(value, type_) if value.type.width < type_.width else expr.Truncate(value, type_)


def _depth(topology, count, parameters):
    if topology is ReductionTopology.LINEAR or topology is ReductionTopology.ORIGINAL:
        return max(0, count - 1)
    if topology is ReductionTopology.LANE_GROUPED:
        lanes = parameters["lanes"]
        return max(0, (count // lanes - 1).bit_length() + 1)
    return max(0, (count - 1).bit_length())


def _parallelism(topology, count, parameters):
    if topology is ReductionTopology.LANE_GROUPED:
        return parameters["lanes"]
    return 1 if topology in {ReductionTopology.LINEAR, ReductionTopology.ORIGINAL} else max(1, count // 2)
