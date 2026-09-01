"""M38 cross-backend artifact validation and deterministic miter generation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

from zlang.backend.identifiers import allocate_private_rtl_identifier
from zlang.backend.manifest import (
    BackendArtifact,
    MANIFEST_VERSION,
    PHYSICAL_DOMAIN_MANIFEST_VERSION,
    PhysicalDomainManifest,
)
from zlang.formal_domain import (
    FormalDomainRenderingError,
    render_formal_domain,
)
from zlang.formal_trace import TraceBinding, originating_sample_cycle
from zlang.ir.cdc import (
    ClockDomain,
    ClockEdge,
    PowerUpPolicy,
    ResetMode,
    ResetPolarity,
    ResetReleaseMode,
)
from zlang.ir.cross_backend import (
    CrossBackendCounterexample, CrossBackendError, CrossBackendMode,
    CrossBackendProperty, CrossBackendRelation, CrossBackendResult,
    CrossBackendStatus,
)
from zlang.ir.equivalence import SignalRole
from zlang.ir.formal import ProofMode
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import Module


@dataclass(frozen=True)
class CrossBackendTraceMetadata:
    """Exact checker-local names allocated by one M38 miter emission."""

    observable_names: tuple[tuple[str, str, str], ...]
    reset: str | None = None
    physical_reset: str | None = None
    comparison_valid: str | None = None


@dataclass(frozen=True)
class CrossBackendMiterEmission:
    source: str
    top: str
    trace_metadata: CrossBackendTraceMetadata


def validate_module_route(module: Module) -> None:
    """Reject semantic relations that are outside the frozen M38 subset.

    M38 consumes already-emitted artifacts, so its low-level miter API cannot
    recover protocol timing semantics from RTL names.  Compiler-owned callers
    must validate the selected module before constructing an M38 property.
    """

    if len(module.clock_domains) > 1:
        raise CrossBackendError(
            "M38 executable cross-backend equivalence supports at most one "
            "exact clock/reset domain"
        )
    if any(
        domain.power_up is not PowerUpPolicy.UNSPECIFIED
        for domain in module.clock_domains
    ):
        raise CrossBackendError(
            "M38 executable cross-backend equivalence does not support "
            "power_up reset"
        )
    if module.elastic_pipeline_regions:
        raise CrossBackendError(
            "M38 fixed-latency cross-backend equivalence does not support "
            "variable-latency elastic pipeline(auto) regions"
        )
    protocol_ports = tuple(
        port
        for port in module.ports
        if port.protocol is not InterfaceProtocol.WIRE
    )
    if protocol_ports:
        rendered = ", ".join(
            f"{port.name} ({port.protocol.value})" for port in protocol_ports
        )
        raise CrossBackendError(
            "M38 executable value equivalence does not support protocol-valued "
            f"top ports: {rendered}"
        )
    if module.aggregate_protocol_endpoints or module.aggregate_protocol_connections:
        raise CrossBackendError(
            "M38 executable cross-backend equivalence does not support "
            "aggregate protocol endpoints or connections, including "
            "scalar-only members"
        )
    transition = module.resolved_transition
    has_transition_state = bool(
        transition is not None
        and (
            transition.resources
            or transition.action_groups
            or transition.priorities
        )
    )
    if any((
        module.registers,
        module.next_assignments,
        module.rules,
        module.fifos,
        module.memories,
        module.roms,
        module.csr_blocks,
        module.request_responses,
        has_transition_state,
        module.children,
        module.elaborated_instances,
        module.protocol_endpoints,
        module.hierarchical_connections,
        module.request_response_connections,
    )):
        raise CrossBackendError(
            "M38 executable cross-backend equivalence does not support "
            "arbitrary stateful, protocol, storage, or hierarchical modules"
        )


def _manifest_clock_domain(item: PhysicalDomainManifest) -> ClockDomain:
    try:
        item.validate()
        return ClockDomain(
            item.clock,
            item.reset,
            ClockEdge(item.clock_edge),
            ResetMode(item.reset_mode),
            ResetPolarity(item.reset_polarity),
            PowerUpPolicy(item.power_up),
            item.source_origin,
            ResetReleaseMode(item.reset_release_mode),
            item.reset_release_cycles,
        )
    except (TypeError, ValueError) as error:
        raise CrossBackendError(
            f"backend physical domain contract is invalid: {error}"
        ) from error


def _physical_domain(
    artifact: BackendArtifact,
    property_: CrossBackendProperty,
    *,
    label: str,
) -> tuple[ClockDomain | None, PhysicalDomainManifest | None]:
    records = tuple(getattr(artifact, "physical_domains", ()))
    expected = property_.clock_domain_contract
    if not records:
        if expected is not None and not expected.is_legacy_default:
            raise CrossBackendError(
                f"{label} artifact does not publish the required physical "
                "clock/reset contract"
            )
        return expected, None
    if len(records) != 1:
        raise CrossBackendError(
            f"{label} artifact must publish exactly one physical clock/reset domain"
        )
    if artifact.manifest_version != PHYSICAL_DOMAIN_MANIFEST_VERSION:
        raise CrossBackendError(
            f"{label} physical-domain artifact requires manifest version "
            f"{PHYSICAL_DOMAIN_MANIFEST_VERSION}"
        )
    if expected is None:
        raise CrossBackendError(
            "cross-backend property is missing the exact physical "
            "clock/reset contract"
        )
    record = records[0]
    domain = _manifest_clock_domain(record)
    if domain != expected:
        raise CrossBackendError(
            f"{label} physical clock/reset contract disagrees with the "
            "cross-backend property"
        )
    if record.rtl_module != artifact.module:
        raise CrossBackendError(
            f"{label} physical domain belongs to a different RTL module"
        )
    return domain, record


def _find(artifact: BackendArtifact, semantic: str, role: SignalRole):
    binding = next(
        (
            item
            for item in artifact.bindings
            if item.semantic_signal_id == semantic and item.role is role
        ),
        None,
    )
    if binding is not None and (
        not binding.physical_available or not binding.rtl_path
    ):
        raise CrossBackendError(
            f"{artifact.backend} physical binding is unavailable for "
            f"{role.value} '{semantic}'"
        )
    return binding


def _binding_types_match(left: object, right: object) -> bool:
    """Compare physical shape and any published canonical value identity.

    Legacy hand-built M38 fixtures omit ``canonical_type``.  Current backend
    artifacts publish it, so require equality when both sides have the typed
    identity without changing the existing compatibility path.
    """

    if (left.width, left.signedness) != (right.width, right.signedness):
        return False
    return not (
        left.canonical_type is not None
        and right.canonical_type is not None
        and left.canonical_type != right.canonical_type
    )


def _observable_token(index: int, semantic_signal_id: str) -> str:
    readable = "".join(
        character if character.isalnum() or character == "_" else "_"
        for character in semantic_signal_id
    ).strip("_") or "signal"
    digest = hashlib.sha256(semantic_signal_id.encode()).hexdigest()[:8]
    return f"m38_observable_{index}_{readable}_{digest}"


def _render_property_domain(
    property_: CrossBackendProperty,
    *,
    used_names: set[str],
):
    domain = property_.clock_domain_contract
    if domain is None or domain.is_legacy_default:
        return None
    try:
        return render_formal_domain(
            domain,
            clock_name="clock",
            reset_name="reset",
            used_names=used_names,
        )
    except FormalDomainRenderingError as error:
        raise CrossBackendError(str(error)) from error


def _differing_observable(
    property_: CrossBackendProperty,
    values: tuple[tuple[str, str], ...],
) -> str | None:
    """Attribute a failure only from the shared typed trace decoder.

    Human log prose and generated source line numbers are not stable result
    artifacts.  The common VCD decoder publishes semantic ``left:``/``right:``
    values; exactly one unequal pair identifies the observable, while missing,
    unknown, or multiple differences remain deliberately ambiguous.
    """

    decoded = dict(values)
    differences = tuple(
        semantic
        for semantic in property_.observable_ids
        if f"left:{semantic}" in decoded
        and f"right:{semantic}" in decoded
        and decoded[f"left:{semantic}"] != decoded[f"right:{semantic}"]
    )
    if len(differences) == 1:
        return differences[0]
    return None


def validate_artifacts(left: BackendArtifact, right: BackendArtifact,
                       property_: CrossBackendProperty) -> None:
    resolved_domains: list[ClockDomain | None] = []
    domain_records: list[PhysicalDomainManifest | None] = []
    for label, artifact in (("left", left), ("right", right)):
        domain, domain_record = _physical_domain(
            artifact, property_, label=label
        )
        resolved_domains.append(domain)
        domain_records.append(domain_record)
        expected_manifest_version = (
            PHYSICAL_DOMAIN_MANIFEST_VERSION
            if domain_record is not None else MANIFEST_VERSION
        )
        if artifact.manifest_version != expected_manifest_version:
            raise CrossBackendError(f"{label} manifest version mismatch")
        if hashlib.sha256(artifact.text.encode()).hexdigest() != artifact.artifact_hash:
            raise CrossBackendError(f"{label} artifact hash mismatch")
        if not artifact.backend or not artifact.module:
            raise CrossBackendError(f"{label} artifact identity is incomplete")
        if any(item.artifact_hash != artifact.artifact_hash for item in artifact.bindings):
            raise CrossBackendError(f"{label} binding artifact hash mismatch")
        if any(
            item.map_version != expected_manifest_version
            for item in artifact.bindings
        ):
            raise CrossBackendError(f"{label} binding manifest version mismatch")
        if {item.selected_ir_identity for item in artifact.bindings} != {artifact.selected_ir_identity}:
            raise CrossBackendError(f"{label} selected-IR identity mismatch")
    if resolved_domains[0] != resolved_domains[1]:
        raise CrossBackendError(
            "cross-backend artifacts publish different physical clock/reset contracts"
        )
    if (
        domain_records[0] is None
        and domain_records[1] is not None
        or domain_records[0] is not None
        and domain_records[1] is None
    ):
        raise CrossBackendError(
            "cross-backend artifacts do not publish matching physical-domain metadata"
        )
    if {left.backend, right.backend} != {"clash", "direct_systemverilog"}:
        raise CrossBackendError("M38 requires Clash and direct SystemVerilog artifacts")
    if left.artifact_hash == right.artifact_hash:
        raise CrossBackendError("left and right artifact hashes must differ")
    if left.selected_ir_identity != right.selected_ir_identity or left.selected_ir_identity != property_.selected_ir_identity:
        raise CrossBackendError("selected-IR identity mismatch")
    for semantic in property_.observable_ids:
        l, r = _find(left, semantic, SignalRole.OUTPUT), _find(right, semantic, SignalRole.OUTPUT)
        if l is None or r is None:
            raise CrossBackendError(f"missing observable binding: {semantic}")
        if (
            not _binding_types_match(l, r)
            or (l.role, l.clock_domain, l.reset_domain)
            != (r.role, r.clock_domain, r.reset_domain)
        ):
            raise CrossBackendError(f"observable binding mismatch: {semantic}")
    if property_.clock_domain_contract is not None:
        for artifact, label, record in (
            (left, "left", domain_records[0]),
            (right, "right", domain_records[1]),
        ):
            clock, reset = _find(artifact, "clock", SignalRole.CLOCK), _find(artifact, "reset", SignalRole.RESET)
            if clock is None or reset is None or clock.clock_domain != property_.clock_domain or reset.reset_domain != property_.reset_domain:
                raise CrossBackendError(f"{label} clock/reset domain mismatch")
            if record is not None and (
                clock.rtl_path != record.rtl_clock_path
                or reset.rtl_path != record.rtl_reset_path
            ):
                raise CrossBackendError(
                    f"{label} physical domain paths disagree with clock/reset bindings"
                )


def emit_cross_backend_miter_with_metadata(
    property_: CrossBackendProperty,
    left: BackendArtifact,
    right: BackendArtifact,
    *,
    inputs: tuple[str, ...] = (),
) -> CrossBackendMiterEmission:
    validate_artifacts(left, right, property_)
    domain = property_.clock_domain_contract
    used_names: set[str] = set()
    if domain is not None:
        used_names.update(("clock", "reset"))
    input_bindings = []
    for semantic in inputs:
        l, r = _find(left, semantic, SignalRole.INPUT), _find(right, semantic, SignalRole.INPUT)
        if l is None or r is None:
            raise CrossBackendError(f"missing input binding: {semantic}")
        if not _binding_types_match(l, r):
            raise CrossBackendError(f"input binding mismatch: {semantic}")
        signal = allocate_private_rtl_identifier(
            semantic.replace(":", "_"),
            semantic_identity=f"{property_.id}|m38|input|{semantic}",
            used=used_names,
        )
        input_bindings.append((semantic, l, r, signal))
    module = f"m38_{property_.id.replace('.', '_')}"
    ports = [
        f"  input logic [{l.width - 1}:0] {signal}"
        for _, l, _, signal in input_bindings
    ]
    if domain is not None:
        ports = ["  input logic clock", "  input logic reset", *ports]
    lines = ["`default_nettype none", f"module {module}(", ",\n".join(ports) + ");"]
    observable_bindings = []
    left_connections: list[str] = []
    right_connections: list[str] = []
    for index, semantic in enumerate(property_.observable_ids):
        l, r = _find(left, semantic, SignalRole.OUTPUT), _find(right, semantic, SignalRole.OUTPUT)
        left_name = allocate_private_rtl_identifier(
            f"left_{index}",
            semantic_identity=f"{property_.id}|m38|left|{semantic}",
            used=used_names,
        )
        right_name = allocate_private_rtl_identifier(
            f"right_{index}",
            semantic_identity=f"{property_.id}|m38|right|{semantic}",
            used=used_names,
        )
        observable_bindings.append((semantic, l, r, left_name, right_name))
        left_connections.append(f".{l.rtl_path}({left_name})")
        right_connections.append(f".{r.rtl_path}({right_name})")
        lines.append(
            f"  logic [{l.width - 1}:0] {left_name}, {right_name};"
        )
    for _, li, ri, signal in input_bindings:
        left_connections.append(f".{li.rtl_path}({signal})")
        right_connections.append(f".{ri.rtl_path}({signal})")
    if domain is not None:
        lc, lr = _find(left, "clock", SignalRole.CLOCK), _find(left, "reset", SignalRole.RESET)
        rc, rr = _find(right, "clock", SignalRole.CLOCK), _find(right, "reset", SignalRole.RESET)
        assert lc is not None and lr is not None and rc is not None and rr is not None
        left_connections.extend((f".{lc.rtl_path}(clock)", f".{lr.rtl_path}(reset)"))
        right_connections.extend((f".{rc.rtl_path}(clock)", f".{rr.rtl_path}(reset)"))
    left_instance = allocate_private_rtl_identifier(
        "left_i",
        semantic_identity=f"{property_.id}|m38|left-instance",
        used=used_names,
    )
    right_instance = allocate_private_rtl_identifier(
        "right_i",
        semantic_identity=f"{property_.id}|m38|right-instance",
        used=used_names,
    )
    lines.extend((
        f"  {left.module} {left_instance}({', '.join(left_connections)});",
        f"  {right.module} {right_instance}({', '.join(right_connections)});",
    ))
    fill = None
    comparison_valid_name: str | None = None
    reset_trace_name: str | None = None
    if property_.relation is CrossBackendRelation.FIXED_LATENCY_VALUE:
        assert domain is not None
        assert property_.comparison_window is not None
        fill = property_.comparison_window.fill_cycles
        comparison_valid_name = allocate_private_rtl_identifier(
            "comparison_valid",
            semantic_identity=f"{property_.id}|m38|comparison-valid",
            used=used_names,
        )
        if domain.is_legacy_default:
            # Preserve the frozen M38 text for the original executable route.
            sample_event = "posedge clock"
            reset_active = "reset"
            lines.extend((
                f"  logic [{fill}:0] {comparison_valid_name};",
                f"  initial begin {comparison_valid_name} = '0; assume(reset); end",
                "  always @(posedge clock) begin",
                f"    if (reset) {comparison_valid_name} <= '0;",
                f"    else {comparison_valid_name} <= "
                f"{{{comparison_valid_name}[{fill - 1}:0], 1'b1}};",
                "  end",
            ))
        else:
            rendered = _render_property_domain(
                property_, used_names=used_names,
            )
            assert rendered is not None
            sample_event = rendered.sample_event
            reset_active = rendered.reset_active
            lines.extend(
                f"  {line}" for line in rendered.support_lines
            )
            history_lines = [
                f"  logic [{fill}:0] {comparison_valid_name};",
                f"  initial begin {comparison_valid_name} = '0; "
                f"assume({rendered.external_reset_asserted}); end",
                f"  always @({rendered.history_event}) begin",
            ]
            if rendered.asynchronous_assertion_event is not None:
                history_lines.append(
                    "    if ("
                    f"{rendered.external_reset_asserted}) "
                    f"{comparison_valid_name} <= '0;"
                )
                if reset_active != rendered.external_reset_asserted:
                    history_lines.append(
                        f"    else if ({reset_active}) "
                        f"{comparison_valid_name} <= '0;"
                    )
            else:
                history_lines.append(
                    f"    if ({reset_active}) {comparison_valid_name} <= '0;"
                )
            history_lines.extend((
                f"    else {comparison_valid_name} <= "
                f"{{{comparison_valid_name}[{fill - 1}:0], 1'b1}};",
                "  end",
            ))
            lines.extend(history_lines)
        reset_trace_name = reset_active
    for index, (semantic, _, _, left_name, right_name) in enumerate(observable_bindings):
        token = _observable_token(index, semantic)
        lines.append(f"  // observable {semantic} assertion {token}")
        if property_.relation is CrossBackendRelation.SAME_CYCLE_VALUE:
            lines.extend((
                f"  always @* begin : {token}",
                f"    assert({left_name} == {right_name});",
                "  end",
            ))
        else:
            assert fill is not None
            assert comparison_valid_name is not None
            lines.extend((
                f"  always @({sample_event}) begin : {token}",
                f"    if (!{reset_active} && {comparison_valid_name}[{fill}]) "
                f"assert({left_name} == {right_name});",
                "  end",
            ))
    lines.extend(("endmodule", "`default_nettype wire", ""))
    metadata = CrossBackendTraceMetadata(
        tuple(
            (semantic, left_name, right_name)
            for semantic, _, _, left_name, right_name in observable_bindings
        ),
        reset_trace_name,
        "reset" if property_.relation is CrossBackendRelation.FIXED_LATENCY_VALUE else None,
        comparison_valid_name,
    )
    return CrossBackendMiterEmission("\n".join(lines), module, metadata)


def emit_cross_backend_miter(property_: CrossBackendProperty, left: BackendArtifact,
                             right: BackendArtifact, *, inputs: tuple[str, ...] = ()) -> str:
    return emit_cross_backend_miter_with_metadata(
        property_, left, right, inputs=inputs,
    ).source


def run_cross_backend_formal(property_: CrossBackendProperty, left: BackendArtifact,
                             right: BackendArtifact, *, inputs: tuple[str, ...] = (),
                             mode: CrossBackendMode = CrossBackendMode.BMC,
                             depth: int = 20, solver: str = "z3",
                             timeout_seconds: int = 120,
                             work_directory: Path | None = None) -> CrossBackendResult:
    result_manifest_version = max(
        left.manifest_version, right.manifest_version
    )
    try:
        emission = emit_cross_backend_miter_with_metadata(
            property_, left, right, inputs=inputs,
        )
    except CrossBackendError as error:
        return CrossBackendResult(property_.id, CrossBackendStatus.SKIPPED, mode, None, solver, depth,
            property_.relation, property_.latency_delta, property_.selected_ir_identity,
            left.backend, right.backend, left.artifact_hash, right.artifact_hash,
            result_manifest_version,
            reason=str(error))
    assert property_.comparison_window is not None
    if (
        mode is CrossBackendMode.BMC
        and not property_.comparison_window.bmc_depth_reaches_comparison(depth)
    ):
        return CrossBackendResult(
            property_.id,
            CrossBackendStatus.UNKNOWN,
            mode,
            None,
            solver,
            depth,
            property_.relation,
            property_.latency_delta,
            property_.selected_ir_identity,
            left.backend,
            right.backend,
            left.artifact_hash,
            right.artifact_hash,
            result_manifest_version,
            reason=property_.comparison_window.bmc_unreached_reason(depth),
        )
    from zlang.formal import run_verilog_formal
    top = emission.top
    formal_source = left.text + "\n" + right.text + "\n" + emission.source
    observable_trace_names = {
        semantic: (left_name, right_name)
        for semantic, left_name, right_name
        in emission.trace_metadata.observable_names
    }
    trace_bindings: list[TraceBinding] = []
    for index, semantic in enumerate(property_.observable_ids):
        left_observation = _find(left, semantic, SignalRole.OUTPUT)
        right_observation = _find(right, semantic, SignalRole.OUTPUT)
        assert left_observation is not None and right_observation is not None
        left_trace_name, right_trace_name = observable_trace_names[semantic]
        trace_bindings.extend((
            TraceBinding(
                f"left:{semantic}",
                left_trace_name,
                left_observation.width,
                left_observation.canonical_type,
                left_observation.signedness,
            ),
            TraceBinding(
                f"right:{semantic}",
                right_trace_name,
                right_observation.width,
                right_observation.canonical_type,
                right_observation.signedness,
            ),
        ))
    if property_.relation is CrossBackendRelation.FIXED_LATENCY_VALUE:
        fill = property_.comparison_window.fill_cycles
        assert emission.trace_metadata.reset is not None
        assert emission.trace_metadata.physical_reset is not None
        assert emission.trace_metadata.comparison_valid is not None
        trace_bindings.extend((
            TraceBinding(
                "reset", emission.trace_metadata.reset, 1, "bit", "bit"
            ),
            TraceBinding(
                "physical_reset",
                emission.trace_metadata.physical_reset,
                1,
                "bit",
                "bit",
            ),
            TraceBinding(
                "comparison_valid",
                emission.trace_metadata.comparison_valid,
                fill + 1,
                f"bits<{fill + 1}>",
                "bits",
            ),
        ))
    result = run_verilog_formal(formal_source,
        top=top, property_id=property_.id,
        mode=ProofMode.BMC if mode is CrossBackendMode.BMC else ProofMode.PROVE,
        depth=depth, solver=solver, systemverilog=True,
        timeout_seconds=timeout_seconds,
        work_directory=work_directory,
        trace_bindings=tuple(trace_bindings),
        comparison_window=property_.comparison_window)
    counterexample = None
    observable_signal_id = (
        property_.observable_ids[0]
        if len(property_.observable_ids) == 1
        else None
    )
    if result.counterexample:
        observable_signal_id = _differing_observable(
            property_, result.counterexample.values
        )
        left_binding = (
            None
            if observable_signal_id is None
            else _find(left, observable_signal_id, SignalRole.OUTPUT)
        )
        right_binding = (
            None
            if observable_signal_id is None
            else _find(right, observable_signal_id, SignalRole.OUTPUT)
        )
        source_origin = property_.source_origin or (
            None if left_binding is None else left_binding.source_origin
        ) or (None if right_binding is None else right_binding.source_origin)
        failure_cycle = result.counterexample.cycle
        sample_cycle = originating_sample_cycle(
            failure_cycle, property_.comparison_window
        )
        counterexample = CrossBackendCounterexample(property_.id, observable_signal_id,
            failure_cycle, sample_cycle, left.backend, right.backend,
            left.artifact_hash, right.artifact_hash,
            left_rtl_path=None if left_binding is None else left_binding.rtl_path,
            right_rtl_path=None if right_binding is None else right_binding.rtl_path,
            values=result.counterexample.values,
            raw_trace=result.counterexample.raw_trace, source_origin=source_origin)
    return CrossBackendResult(property_.id, CrossBackendStatus(result.status.value), mode,
        result.engine, result.solver, result.depth, property_.relation, property_.latency_delta,
        property_.selected_ir_identity, left.backend, right.backend, left.artifact_hash,
        right.artifact_hash, result_manifest_version, observable_signal_id,
        property_.source_origin,
        counterexample, result.reason)


__all__ = [
    "CrossBackendMiterEmission",
    "CrossBackendTraceMetadata",
    "emit_cross_backend_miter",
    "emit_cross_backend_miter_with_metadata",
    "run_cross_backend_formal",
    "validate_artifacts",
    "validate_module_route",
]
