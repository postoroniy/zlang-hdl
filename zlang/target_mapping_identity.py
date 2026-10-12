"""Deterministic target architecture selection and mapping."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from hashlib import sha256

from zlang.ir import expressions as expr
from zlang.ir.module import Module
from zlang.ir.pipelines import PipelinePlan
from zlang.ir.timing import TimingKnowledge
from zlang.source import SourceOrigin

from zlang import target_catalog as catalog


def require_named_resource(resources, template):
    matches = tuple(item for item in resources if item.name == template.resource_name)
    if len(matches) != 1:
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' requires unavailable resource "
            f"'{template.resource_name}'"
        )
    return matches[0]


def _module_implementation_latency(module: Module) -> tuple[str, int]:
    """Translate semantic output timing into the integer graph compatibility ABI.

    ``ImplementationGraph.latency`` predates public module timing and is an
    integer consumed by deterministic cost selection/pipeline scheduling cost code.  Preserve that field while carrying
    the knowledge class separately, so an unknown stateful output is never
    described as a proven zero-cycle result.
    """
    contract = getattr(module, "timing_contract", None)
    if contract is not None:
        return TimingKnowledge.KNOWN.value, contract.latency

    output_timings = tuple(getattr(module, "output_timings", ()))
    if output_timings:
        unknown = tuple(
            item for item in output_timings
            if item.timing.knowledge is TimingKnowledge.UNKNOWN
        )
        if unknown:
            return TimingKnowledge.UNKNOWN.value, 0
        known = {
            item.timing.latency
            for item in output_timings
            if item.timing.knowledge is TimingKnowledge.KNOWN
        }
        if len(known) > 1:
            raise catalog.TargetArchitectureError(
                "module outputs have inconsistent derived implementation latency"
            )
        if known:
            return TimingKnowledge.KNOWN.value, next(iter(known))
        return TimingKnowledge.TIMELESS.value, 0

    if (
        module.has_protocol_interfaces
        or module.hierarchical_connections
        or module.registers
        or module.rules
        or module.fifos
        or module.memories
        or module.roms
        or module.request_responses
        or module.csr_blocks
    ):
        # Legacy/uncontracted stateful modules may predate derived output
        # records.  Preserve their uncertainty instead of reviving the old
        # misleading "latency zero" claim.
        return TimingKnowledge.UNKNOWN.value, 0

    # Compatibility for old hand-built typed modules without timing records.
    # The shared traversal is recursive, so explicit delays/pipelines nested
    # below conversions and arithmetic no longer collapse to zero.
    from zlang.timing import timing_info

    assignment_latencies = tuple(
        timing_info(item.expression, module=module).latency
        for item in module.assignments
    )
    return (
        TimingKnowledge.KNOWN.value,
        max(assignment_latencies, default=0),
    )


def _expression_identity(value: expr.Expression) -> str:
    """Historical resource-port identity, not the scheduled value DAG key.

    Existing measured DSP graph keys use this serializer.  Scheduled
    operation joins deliberately recompute the typed value identity from the
    retained expression rather than comparing these two hash namespaces.
    """

    return sha256(_semantic_payload(value).encode()).hexdigest()


def _semantic_mapping_identity(value: expr.Expression) -> str:
    if isinstance(value, expr.VectorIndex) and isinstance(value.expression, expr.InputRef):
        return f"port:{value.expression.name}[{value.index}]"
    if isinstance(value, expr.InputRef):
        return f"port:{value.name}"
    return "value:" + _expression_identity(value)


def _semantic_field_names(type_: type) -> tuple[str, ...]:
    return tuple(
        item.name
        for item in fields(type_)
        if item.name not in {"origin", "source_origin"}
    )


def _semantic_payload(value) -> str:
    """Render the historical resource payload with cached field schemas."""

    if isinstance(value, tuple):
        return "(" + ",".join(_semantic_payload(item) for item in value) + ")"
    if is_dataclass(value):
        body = ",".join(
            f"{name}={_semantic_payload(getattr(value, name))}"
            for name in _semantic_field_names(type(value))
        )
        return f"{type(value).__module__}.{type(value).__name__}({body})"
    return repr(value)


def _generic_physical_digest(value: object) -> str:
    """Return a stable physical digest without expanding a shared DAG.

    The historical serializer returned a recursively nested Python tuple.  A
    semantic node with multiple consumers was consequently copied into that
    tuple once per logical path, so merely asking for a generic implementation
    identity could consume quadratic time and memory.  This serializer hashes
    each object once and lets parents refer to child content digests.  The
    result remains independent of Python object identities and source spans.
    """

    memo: dict[int, tuple[object, str]] = {}

    def digest(item: object) -> str:
        if item is None or isinstance(item, (str, int, float, bool)):
            return sha256(repr(("scalar", item)).encode()).hexdigest()
        if isinstance(item, SourceOrigin):
            return sha256(b"source_origin:omitted").hexdigest()
        if isinstance(item, Enum):
            return sha256(
                repr(
                    (
                        "enum",
                        type(item).__module__,
                        type(item).__qualname__,
                        item.value,
                    )
                ).encode()
            ).hexdigest()

        cached = memo.get(id(item))
        if cached is not None and cached[0] is item:
            return cached[1]

        if isinstance(item, PipelinePlan):
            if item.scheduled_value_graph is not None:
                payload: object = (
                    "scheduled_pipeline",
                    item.scheduled_value_graph.identity,
                )
            else:
                payload = (
                    "legacy_pipeline",
                    item.stage_boundaries,
                    item.inserted_registers,
                    item.alignment_delays,
                    item.requested_latency,
                    item.initiation_interval,
                )
        elif isinstance(item, tuple):
            payload = ("tuple", tuple(digest(value) for value in item))
        elif isinstance(item, list):
            payload = ("list", tuple(digest(value) for value in item))
        elif isinstance(item, dict):
            pairs = tuple(
                sorted(
                    ((digest(key), digest(value)) for key, value in item.items()),
                )
            )
            payload = ("dict", pairs)
        elif isinstance(item, (set, frozenset)):
            payload = ("set", tuple(sorted(digest(value) for value in item)))
        elif is_dataclass(item) and not isinstance(item, type):
            excluded = {
                "origin",
                "origins",
                "source_origin",
                "source_identity",
                "source_hash",
                "source_path",
                "formal_records",
                "formal_eligible",
                "estimate",
                "measurement",
                "cost_policy",
                "semantic_expression_arena_statistics",
                "semantic_expression_provenance",
                "selected_value_normalization_statistics",
            }
            if isinstance(item, Module):
                excluded.update(
                    {
                        "pipeline_explorations",
                        "equivalences",
                        "verification_scopes",
                    }
                )
            payload = (
                "dataclass",
                type(item).__module__,
                type(item).__qualname__,
                tuple(
                    (field.name, digest(getattr(item, field.name)))
                    for field in fields(item)
                    if field.name not in excluded
                ),
            )
        else:
            raise TypeError(
                "generic module physical identity cannot serialize "
                f"{type(item).__module__}.{type(item).__qualname__}"
            )
        result = sha256(repr(payload).encode()).hexdigest()
        memo[id(item)] = (item, result)
        return result

    return digest(value)
