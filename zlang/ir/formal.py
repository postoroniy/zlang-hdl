"""Backend-independent formal verification IR and safety-property generation.

The objects in this module deliberately contain semantic names, not RTL names.
Backends publish :class:`SignalBinding` records separately; the harness emitter
is the only layer that resolves those records to implementation signals.
"""

from __future__ import annotations

from zlang.ir.formal_models import (
    Counterexample as Counterexample,
    CoverProperty as CoverProperty,
    CoverResult as CoverResult,
    CoverStatus as CoverStatus,
    CoverWitness as CoverWitness,
    FormalDesign as FormalDesign,
    FormalError as FormalError,
    FormalProperty as FormalProperty,
    FormalPropertyClassification as FormalPropertyClassification,
    FormalResult as FormalResult,
    FormalStatus as FormalStatus,
    Ownership as Ownership,
    ProofMode as ProofMode,
    PropertyKind as PropertyKind,
    SignalBinding as SignalBinding,
    TemporalForm as TemporalForm,
)
from zlang.ir.formal_domains import (
    mark_formal_domain_applicability as mark_formal_domain_applicability,
)
from zlang.ir.formal_binding import (
    connect_formal_design as connect_formal_design,
    signal_bindings as signal_bindings,
    top_aggregate_ownership as top_aggregate_ownership,
)
from zlang.ir.formal_harness import (
    cover_harness_top as cover_harness_top,
    emit_cover_harness as emit_cover_harness,
    emit_harness as emit_harness,
    formal_harness_domain_rendering as formal_harness_domain_rendering,
    render_bound_predicate as render_bound_predicate,
)
from zlang.ir.formal_property_generation import (
    generate_properties as generate_properties,
)
from zlang.source import SourceOrigin

def classify_result(*, property_id: str, mode: ProofMode, outcome: str,
                    engine: str | None, solver: str | None, depth: int | None = None,
                    source_origin: SourceOrigin | None = None, reason: str | None = None) -> FormalResult:
    """Map an external runner outcome without conflating BMC and proof."""
    normalized = outcome.lower().strip()
    if normalized == "proven" and mode is not ProofMode.PROVE:
        raise FormalError("proven is only valid for prove mode")
    mapping = {"proven": FormalStatus.PROVEN, "failed": FormalStatus.FAILED,
               "unknown": FormalStatus.UNKNOWN, "skipped": FormalStatus.SKIPPED,
               "pass": FormalStatus.BOUNDED_PASS if mode is ProofMode.BMC else FormalStatus.PROVEN}
    if normalized not in mapping:
        raise FormalError(f"unknown formal outcome: {outcome}")
    if normalized == "failed":
        raise FormalError(
            "failed outcomes require typed counterexample metadata; construct "
            "FormalResult with a Counterexample"
        )
    return FormalResult(property_id, mapping[normalized], mode, engine, solver, depth,
                        source_origin=source_origin, reason=reason)
