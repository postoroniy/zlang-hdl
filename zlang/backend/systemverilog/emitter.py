"""Fail-closed direct SystemVerilog emission for the production backend.

The backend consumes typed semantic IR, emits only validated shapes, and reports
unsupported regions before publishing a BackendArtifact.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib

from zlang.ir import expressions as expr
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir.normalization import normalize_selected_values
from zlang.ir import storage as ir_storage
from zlang.ir import state as ir_state
from zlang.backend.companions import collect_rom_companions
from zlang.backend.external import ExternalMappingError, ExternalPhysicalMapping
from zlang.backend import expression_materialization as materialization
from zlang.backend import identifiers as identifiers
from zlang.backend import naming as naming
from zlang.backend.manifest import BackendArtifact, publish_artifact
from zlang.backend import module_features as module_features
from zlang.backend.source_map import GeneratedSourceMap, build_generated_source_map
from zlang.backend.systemverilog import boundary as sv_boundary
from zlang.backend.systemverilog import cdc as sv_cdc
from zlang.backend.systemverilog import context as emission_context
from zlang.backend.systemverilog import csr as sv_csr
from zlang.backend.systemverilog import composed as composed
from zlang.backend.systemverilog import functional_scatter as _functional_scatter
from zlang.backend.systemverilog import formal as sv_formal
from zlang.backend.systemverilog import feature_accounting as sv_accounting
from zlang.backend.systemverilog import materialized as sv_materialized
from zlang.backend.systemverilog import module_rendering as sv_module_rendering
from zlang.backend.systemverilog import packet as sv_packet
from zlang.backend.systemverilog import physicalization as sv_physicalization
from zlang.backend.systemverilog import pipeline as sv_pipeline
from zlang.backend.systemverilog import protocols as sv_protocols
from zlang.backend.systemverilog import request_response as sv_request_response
from zlang.backend.systemverilog import rules as sv_rules
from zlang.backend.systemverilog import sequential as sv_sequential
from zlang.backend.systemverilog import storage as sv_storage
from zlang.ir import hierarchy as ir_hierarchy
from zlang.ir.recursive_formal import BackendPhysicalLocator
from zlang import memory_planning as memory_planning
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.systemverilog import expression as sv_expression
from zlang.backend.systemverilog import rendering as sv_rendering
from zlang.backend.systemverilog import state as sv_state
MAX_SCATTER_CHUNK_RESULT_BIT_UPDATES = (
    _functional_scatter.MAX_SCATTER_CHUNK_RESULT_BIT_UPDATES
)
MAX_SCATTER_CHUNK_WRITES = _functional_scatter.MAX_SCATTER_CHUNK_WRITES


# Functional scatter lowering is a backend-local physical choice.  These
# limits bound both emitted structure and the whole-vector update chains that
# Verilog process lowering otherwise constructs.  They do not change the
# typed-IR OR/collision semantics.


@dataclass(frozen=True)
class SystemVerilogCapabilityReport:
    supported: bool
    module: str
    semantic_path: tuple[str, ...] = ()
    reason: str | None = None


def _external_contracts(module: ir_module.Module) -> tuple[object, ...]:
    contracts: list[object] = []

    def visit(current: ir_module.Module) -> None:
        if current.external_contract is not None:
            contracts.append(current.external_contract)
        for child in current.children:
            visit(child)

    visit(module)
    return tuple(
        sorted(
            {item.semantic_identity: item for item in contracts}.values(),
            key=lambda item: item.semantic_identity,
        )
    )


def _external_mapping_index(
    module: ir_module.Module,
    mappings: tuple[ExternalPhysicalMapping, ...],
) -> dict[str, ExternalPhysicalMapping]:
    contracts = _external_contracts(module)
    by_identity: dict[str, ExternalPhysicalMapping] = {}
    for mapping in mappings:
        if mapping.logical_extern_identity in by_identity:
            raise SystemVerilogEmissionError(
                "duplicate physical mapping for external identity "
                f"'{mapping.logical_extern_identity}'"
            )
        by_identity[mapping.logical_extern_identity] = mapping
    required = {item.semantic_identity for item in contracts}
    unknown = set(by_identity) - required
    if unknown:
        raise SystemVerilogEmissionError(
            f"external mapping '{sorted(unknown)[0]}' is not used by this design"
        )
    for contract in contracts:
        mapping = by_identity.get(contract.semantic_identity)
        if mapping is None:
            raise SystemVerilogEmissionError(
                f"external module '{contract.logical_name}' requires an exact "
                "direct-SystemVerilog physical mapping"
            )
        try:
            mapping.validate(contract, backend="direct_systemverilog")
        except ExternalMappingError as error:
            raise SystemVerilogEmissionError(str(error)) from error
    return by_identity


def _external_sources(
    mappings: dict[str, ExternalPhysicalMapping],
) -> str:
    seen_hashes: set[str] = set()
    sources: list[str] = []
    for identity in sorted(mappings):
        mapping = mappings[identity]
        if mapping.source_sha256 in seen_hashes:
            continue
        seen_hashes.add(mapping.source_sha256)
        text = mapping.source_text
        sources.append(text if text.endswith("\n") else text + "\n")
    return "".join(sources)


def _emit_external_wrapper(
    module: ir_module.Module,
    wrapper_name: str,
    mapping: ExternalPhysicalMapping,
) -> str:
    if module.external_contract is None:
        raise SystemVerilogEmissionError("external wrapper requires an external contract")
    if mapping.physical_module_name == wrapper_name:
        raise SystemVerilogEmissionError(
            f"external physical module '{mapping.physical_module_name}' collides "
            "with its generated wrapper"
        )
    mapped = dict(mapping.port_map)
    connections = [
        f".{mapped[port.name]}({sv_rendering._identifier(port.name)})"
        for port in module.ports
    ]
    body = [
        f"  {mapping.physical_module_name} external_impl (\n    "
        + ",\n    ".join(connections)
        + "\n  );"
    ]
    return sv_module_rendering._named_module(
        wrapper_name,
        sv_boundary._physical_port_declarations(module),
        body,
        typed_module=replace(
            module,
            assignments=(),
            functions=(),
            callable_definitions=(),
        ),
    )


def capability_report(module: ir_module.Module) -> SystemVerilogCapabilityReport:
    """Return the fail-closed result of lowering the complete selected module."""
    try:
        emit(module)
    except SystemVerilogEmissionError as error:
        return SystemVerilogCapabilityReport(False, module.name, error.semantic_path, str(error))
    return SystemVerilogCapabilityReport(True, module.name)


def emit(
    module: ir_module.Module,
    *,
    external_mappings: tuple[ExternalPhysicalMapping, ...] = (),
    _formal_buffer_counts: tuple[composed.FormalBufferCountProjection, ...] = (),
    _formal_adapter_counts: tuple[sv_protocols.FormalAdapterCountProjection, ...] = (),
) -> str:
    """Emit one design, reporting impossible physical allocations structurally."""

    try:
        return _emit_named_design(
            module,
            external_mappings=external_mappings,
            _formal_buffer_counts=_formal_buffer_counts,
            _formal_adapter_counts=_formal_adapter_counts,
        )
    except naming.RtlNamingError as error:
        raise SystemVerilogEmissionError(
            str(error), semantic_path=(module.name,),
            code="ZL-BACKEND-SYSTEMVERILOG-NAMING",
        ) from error


def _emit_named_design(
    module: ir_module.Module,
    *,
    external_mappings: tuple[ExternalPhysicalMapping, ...] = (),
    _formal_buffer_counts: tuple[composed.FormalBufferCountProjection, ...] = (),
    _formal_adapter_counts: tuple[sv_protocols.FormalAdapterCountProjection, ...] = (),
) -> str:
    """Emit one design with its public aggregate ABI in the selected top.

    Component modules intentionally retain the compiler's compact packed ABI.
    The selected top alone exposes typed public leaves and owns the exact
    pack/unpack bridges required to connect them to its internal packed roots.
    """

    # Generic specialization identities deliberately retain exact dependency
    # provenance for build/proof caching.  That provenance must not leak into
    # physical helper names, otherwise a spelling-only dependency edit changes
    # byte-identical RTL.  Render from an ephemeral copy whose generic callable
    # names are derived from the exact typed body/signature instead.
    physical_module = sv_physicalization.physicalize_generic_callables(
        normalize_selected_values(module, prune_callables=False)
    )
    with emission_context.emission_scope(physical_module.name) as emission:
        hierarchy_cache = ir_hierarchy.HierarchyTraversalCache()
        public_abi = emission_context.top_physical_abi(physical_module)
        sv_boundary._validate_public_leaf_identifiers(tuple(public_abi.leaves))
        _validate_state_storage_rtl_namespace(
            physical_module, hierarchy_cache=hierarchy_cache
        )
        boundary = sv_boundary._build_top_boundary_plan(
            physical_module, leaves=tuple(public_abi.leaves),
        )
        packed = _emit_packed(
            physical_module,
            external_mappings=external_mappings,
            formal_buffer_counts=_formal_buffer_counts,
            formal_adapter_counts=_formal_adapter_counts,
            hierarchy_cache=hierarchy_cache,
            top_boundary=boundary,
        )
    scatter_helpers = emission.functional.scatter_helpers
    if not scatter_helpers:
        return packed
    suffix = "`default_nettype wire\n"
    if not packed.endswith(suffix):
        raise SystemVerilogEmissionError(
            "direct-SystemVerilog output lost its default-nettype boundary"
        )
    helper_text = "\n".join(scatter_helpers[name] for name in sorted(scatter_helpers))
    return f"{packed[:-len(suffix)]}{helper_text}{suffix}"


def _validate_state_storage_rtl_namespace(
    module: ir_module.Module,
    *,
    hierarchy_cache: ir_hierarchy.HierarchyTraversalCache | None = None,
) -> None:
    """Reject colliding architectural-state tokens before rendering RTL.

    Writable-memory storage uses two backend-owned suffixes.  A source register
    such as ``foo_cells`` must therefore not alias the cells of memory ``foo``.
    The access companion consumes the same identifier helpers, so accepting a
    collision here would also give two semantic bindings one VPI locator.
    """

    selected_hierarchy_cache = hierarchy_cache or ir_hierarchy.HierarchyTraversalCache()
    sv_physicalization.validated_hierarchy(
        module, cache=selected_hierarchy_cache
    )
    tokens: dict[str, str] = {}
    local_names = emission_context.module_rtl_names(module)

    def claim(token: str, owner: str) -> None:
        previous = tokens.get(token)
        if previous is not None and previous != owner:
            raise SystemVerilogEmissionError(
                "direct-SystemVerilog module identifier collision in module "
                f"'{module.name}': {previous} and {owner} both map to "
                f"'{token}'",
                semantic_path=(module.name,),
                code="ZL-BACKEND-SYSTEMVERILOG-STATE-NAME-COLLISION",
            )
        tokens[token] = owner

    for leaf in emission_context.top_physical_abi(module).leaves:
        logical = leaf.packed_root_external_name or leaf.external_name
        claim(identifiers.rtl_identifier(logical), f"port '{logical}'")
    for elaborated in module.elaborated_instances:
        name = elaborated.instance.name
        claim(local_names.instance(name), f"child instance '{name}'")
    if (
        module.elaborated_instances
        or module.hierarchical_connections
        or module.request_response_connections
        or module.aggregate_protocol_connections
    ):
        for token, owner in composed.composed_component_identifier_claims(
            module,
            _composed_rendering(hierarchy_cache=selected_hierarchy_cache),
        ):
            claim(token, owner)
    for item in sv_materialized._materialization_plan(module):
        claim(item.name, f"materialized expression '{item.name}'")
    for root in sv_materialized._module_expression_roots(module):
        for value in materialization.walk_expression(root):
            if isinstance(value, (expr.Delay, expr.Pipeline)):
                count = value.cycles if isinstance(value, expr.Delay) else value.stages
                kind = "delay" if isinstance(value, expr.Delay) else "pipeline"
                for index in range(1, count + 1):
                    claim(
                        local_names.stage(kind, value.instance, index),
                        f"{kind} stage {value.instance}:{index}",
                    )
    for register in module.registers:
        claim(
            identifiers.rtl_register_state_identifier(register.name),
            f"register '{register.name}'",
        )
    for fifo in module.fifos:
        name = identifiers.rtl_identifier(fifo.name)
        if fifo.scheduled:
            suffixes = (
                "storage", "count", "rd", "wr", "push", "pop", "push_data",
                "front", "empty", "full", "valid", "ready", "overflow",
                "underflow",
            )
        else:
            referenced = sv_storage._referenced_fifo_signals(module, fifo)
            suffixes = (
                "storage", "count", "rd", "wr", "push_request", "pop_request",
                "push", "pop",
                *(
                    signal.value
                    for signal in (
                        ir_storage.FifoSignal.FRONT,
                        ir_storage.FifoSignal.EMPTY,
                        ir_storage.FifoSignal.FULL,
                        ir_storage.FifoSignal.VALID,
                        ir_storage.FifoSignal.READY,
                        ir_storage.FifoSignal.OVERFLOW,
                        ir_storage.FifoSignal.UNDERFLOW,
                    )
                    if signal in referenced
                ),
            )
        for suffix in suffixes:
            claim(f"{name}_{suffix}", f"FIFO '{fifo.name}' {suffix}")
    for memory in module.memories:
        name = identifiers.rtl_identifier(memory.name)
        if memory.ported:
            implementation = memory_planning.plan_memory_implementation(memory)
            if (
                implementation.implementation
                is memory_planning.MemoryImplementationKind.REPLICATED_1R1W
            ):
                for port in memory.ports:
                    if port.kind in {ir_storage.MemoryPortKind.READ, ir_storage.MemoryPortKind.READ_WRITE}:
                        claim(
                            f"{identifiers.rtl_memory_cells_identifier(memory.name)}_"
                            f"{identifiers.rtl_identifier(port.name)}",
                            f"memory '{memory.name}' port '{port.name}' cells",
                        )
            else:
                claim(
                    identifiers.rtl_memory_cells_identifier(memory.name),
                    f"memory '{memory.name}' cells",
                )
            for port in memory.ports:
                if port.kind in {ir_storage.MemoryPortKind.READ, ir_storage.MemoryPortKind.READ_WRITE}:
                    claim(
                        f"{name}_{identifiers.rtl_identifier(port.name)}_read_data",
                        f"memory '{memory.name}' port '{port.name}' read data",
                    )
        else:
            claim(
                identifiers.rtl_memory_cells_identifier(memory.name),
                f"memory '{memory.name}' cells",
            )
            claim(
                identifiers.rtl_memory_read_data_identifier(memory.name),
                f"memory '{memory.name}' read data",
            )
        if memory.scheduled:
            for suffix in (
                "read_fire", "write_fire", "read_address", "write_address",
                "write_data",
            ):
                claim(f"{name}_{suffix}", f"memory '{memory.name}' {suffix}")
        if memory.write_mask_width is not None:
            for suffix in ("write_mask", "write_mask_expanded", "write_merged"):
                claim(f"{name}_{suffix}", f"memory '{memory.name}' {suffix}")
    for rom in module.roms:
        name = identifiers.rtl_identifier(rom.name)
        claim(f"{name}_cells", f"ROM '{rom.name}' cells")
        claim(f"{name}_read_data", f"ROM '{rom.name}' read data")
    # The compact register path emits no guard/fire helpers. Claim only real
    # objects, and allocate emitted helpers around source state names.
    if sv_state.requires_unified_state(module):
        for rule in module.rules:
            claim(local_names.rule(rule.name, "guard"), f"rule '{rule.name}' guard")
            claim(local_names.rule(rule.name, "fire"), f"rule '{rule.name}' fire")
    if module.resolved_transition is not None:
        for index, _activation in enumerate(
            ir_state.conditional_activation_predicates(module.resolved_transition)
        ):
            claim(
                f"zlang_condition_{index}_active",
                f"conditional-action predicate {index}",
            )
    for child in module.children:
        _validate_state_storage_rtl_namespace(
            child, hierarchy_cache=selected_hierarchy_cache
        )


def _emit_packed(
    module: ir_module.Module,
    *,
    external_mappings: tuple[ExternalPhysicalMapping, ...] = (),
    formal_buffer_counts: tuple[composed.FormalBufferCountProjection, ...] = (),
    formal_adapter_counts: tuple[sv_protocols.FormalAdapterCountProjection, ...] = (),
    hierarchy_cache: ir_hierarchy.HierarchyTraversalCache | None = None,
    top_boundary: sv_boundary.TopBoundaryPlan | None = None,
) -> str:
    """Emit one supported typed module as direct synthesizable SystemVerilog."""

    if len(module.clock_domains) == 1:
        try:
            sv_sequential.module_domain(module)
        except sv_sequential.PhysicalDomainError as error:
            raise SystemVerilogEmissionError(str(error)) from error
    else:
        try:
            for domain in module.clock_domains:
                sv_sequential.module_domain(module, domain.clock)
        except sv_sequential.PhysicalDomainError as error:
            raise SystemVerilogEmissionError(str(error)) from error

    unsafe_state_engines = module_features.unsupported_legacy_state_mix(module)
    if unsafe_state_engines:
        engines = ", ".join(unsafe_state_engines)
        raise SystemVerilogEmissionError(
            f"module '{module.name}' combines user registers/rules with {engines}; "
            "the selected direct-SystemVerilog state engines are not yet "
            "compositional",
            semantic_path=(module.name,),
            code="ZL-BACKEND-SYSTEMVERILOG-UNCLAIMED-STATE",
            notes=(
                "emission stopped before artifact publication so no semantic "
                "state can be silently omitted",
            ),
            fixes=(
                "place the stateful feature in a typed child module until the "
                "corresponding unified-state slice is supported",
            ),
        )

    crossing = next(
        (
            connection.crossing
            for connection in module.connections
            if connection.crossing is not None
        ),
        None,
    )
    if crossing is not None:
        sv_accounting.account_emission_plan(
            module,
            "cdc",
            module_features.ModuleFeatureGroup.PROTOCOL_PORTS,
            module_features.ModuleFeatureGroup.HIERARCHICAL_PROTOCOL_ENDPOINTS,
            module_features.ModuleFeatureGroup.AGGREGATE_PROTOCOL_ENDPOINTS,
            module_features.ModuleFeatureGroup.CONNECTIONS,
            module_features.ModuleFeatureGroup.AGGREGATE_PROTOCOL_CONNECTIONS,
            *sv_accounting.STATE_GROUPS,
        )
        with emission_context.top_boundary_scope(top_boundary):
            body = sv_cdc.emit_cdc_module(module, _cdc_rendering(module))
        return "`default_nettype none\n" + body + "`default_nettype wire\n"
    if any(connection.adapter is not None for connection in module.connections):
        sv_accounting.account_emission_plan(
            module,
            "connection_adapter",
            module_features.ModuleFeatureGroup.PROTOCOL_PORTS,
            module_features.ModuleFeatureGroup.CONNECTIONS,
            module_features.ModuleFeatureGroup.CREDIT_PORTS,
        )
        with emission_context.top_boundary_scope(top_boundary):
            body = sv_protocols._emit_connection_adapter(
                module, formal_adapter_counts=formal_adapter_counts
            )
        return "`default_nettype none\n" + body + "`default_nettype wire\n"
    if module.elastic_pipeline_regions:
        sv_accounting.account_emission_plan(
            module,
            "elastic_pipeline",
            module_features.ModuleFeatureGroup.PROTOCOL_PORTS,
            module_features.ModuleFeatureGroup.ELASTIC_PIPELINE_REGIONS,
        )
        with emission_context.top_boundary_scope(top_boundary):
            body = sv_pipeline._emit_elastic_pipeline(module)
        return "`default_nettype none\n" + body + "`default_nettype wire\n"
    if (
        not module.csr_blocks
        and (
        module.elaborated_instances
        or (
            module.registers
            and module.next_assignments
            and not module.rules
            and not any(
                interface.ordering is ir_interfaces.RequestResponseOrdering.IN_ORDER
                for interface in module.request_responses
            )
        )
        or (
            module.is_sequential
            and any(
                port.protocol is ir_interfaces.InterfaceProtocol.READY_VALID
                for port in module.ports
            )
            and bool(module.registers or module.rules or module.next_assignments)
        )
        or any(connection.buffer_depth for connection in module.connections)
        )
    ):
        sv_accounting.account_emission_plan(
            module, "composed", *sv_accounting.COMPOSED_GROUPS
        )
        mapping_index = _external_mapping_index(module, external_mappings)
        with emission_context.top_boundary_scope(top_boundary):
            body = composed.emit_composed_design(
                module,
                _composed_rendering(
                    mapping_index,
                    formal_buffer_counts=formal_buffer_counts,
                    hierarchy_cache=hierarchy_cache,
                ),
            )
        return (
            "`default_nettype none\n"
            + _external_sources(mapping_index)
            + body
            + "`default_nettype wire\n"
        )
    if module.external_contract is not None:
        sv_accounting.account_emission_plan(module, "external_wrapper")
        mapping_index = _external_mapping_index(module, external_mappings)
        with emission_context.top_boundary_scope(top_boundary):
            body = _emit_external_wrapper(
                module,
                identifiers.rtl_identifier(module.name),
                mapping_index[module.external_contract.semantic_identity],
            )
        return (
            "`default_nettype none\n"
            + _external_sources(mapping_index)
            + body
            + "`default_nettype wire\n"
        )
    with emission_context.top_boundary_scope(top_boundary):
        if module.csr_blocks:
            sv_accounting.account_emission_plan(
                module,
                "csr_composed_state",
                *sv_accounting.STATE_GROUPS,
                module_features.ModuleFeatureGroup.CSR_BLOCKS,
                module_features.ModuleFeatureGroup.PROTOCOL_PORTS,
            )
            body = sv_csr._emit_csr(module)
        elif module.request_responses:
            sv_accounting.account_emission_plan(
                module,
                "request_response",
                module_features.ModuleFeatureGroup.REQUEST_RESPONSE_INTERFACES,
                *sv_accounting.STATE_GROUPS,
            )
            body = sv_request_response._emit_request_response(module)
        elif sv_state.requires_unified_state(module):
            sv_accounting.account_emission_plan(
                module, "unified_state",
                *sv_accounting.STATE_GROUPS,
                *sv_accounting.STORAGE_GROUPS,
                module_features.ModuleFeatureGroup.PROTOCOL_PORTS,
            )
            body = sv_storage._emit_unified_state_module(module)
        elif module.roms:
            sv_accounting.account_emission_plan(
                module, "rom", module_features.ModuleFeatureGroup.ROMS
            )
            body = sv_storage._emit_rom_module(module)
        elif module.memories:
            sv_accounting.account_emission_plan(
                module, "memory", module_features.ModuleFeatureGroup.MEMORIES
            )
            body = sv_storage._emit_memory(module)
        elif module.fifos:
            sv_accounting.account_emission_plan(
                module,
                "fifo",
                module_features.ModuleFeatureGroup.FIFOS,
                module_features.ModuleFeatureGroup.PROTOCOL_PORTS,
            )
            body = sv_storage._emit_fifo(module)
        elif module.arbiters:
            sv_accounting.account_emission_plan(
                module,
                "packet_arbiter",
                module_features.ModuleFeatureGroup.ARBITERS,
                module_features.ModuleFeatureGroup.PROTOCOL_PORTS,
            )
            body = sv_packet._emit_packet_arbiter(module)
        elif any(
            port.protocol is ir_interfaces.InterfaceProtocol.VC_CREDIT for port in module.ports
        ):
            sv_accounting.account_emission_plan(
                module,
                "vc_credit",
                module_features.ModuleFeatureGroup.PROTOCOL_PORTS,
                module_features.ModuleFeatureGroup.VC_CREDIT_PORTS,
            )
            body = sv_protocols._emit_vc_credit(module)
        elif any(port.protocol is ir_interfaces.InterfaceProtocol.CREDIT for port in module.ports):
            sv_accounting.account_emission_plan(
                module,
                "credit",
                module_features.ModuleFeatureGroup.PROTOCOL_PORTS,
                module_features.ModuleFeatureGroup.CREDIT_PORTS,
            )
            body = sv_protocols._emit_credit(module)
        elif any(
            port.protocol is ir_interfaces.InterfaceProtocol.READY_VALID for port in module.ports
        ):
            sv_accounting.account_emission_plan(
                module,
                "ready_valid",
                module_features.ModuleFeatureGroup.PROTOCOL_PORTS,
                module_features.ModuleFeatureGroup.HIERARCHICAL_PROTOCOL_ENDPOINTS,
                module_features.ModuleFeatureGroup.AGGREGATE_PROTOCOL_ENDPOINTS,
                module_features.ModuleFeatureGroup.CONNECTIONS,
                module_features.ModuleFeatureGroup.AGGREGATE_PROTOCOL_CONNECTIONS,
            )
            body = sv_protocols._emit_ready_valid(module)
        elif module.rules:
            sv_accounting.account_emission_plan(
                module,
                "rules",
                *sv_accounting.STATE_GROUPS,
            )
            body = sv_rules._emit_rules(module)
        elif module.is_sequential:
            sv_accounting.account_emission_plan(
                module,
                "sequential",
                module_features.ModuleFeatureGroup.REGISTERS,
                module_features.ModuleFeatureGroup.NEXT_ASSIGNMENTS,
            )
            has_staging = any(
                isinstance(value, (expr.Delay, expr.Pipeline))
                for assignment in module.assignments
                for value in materialization.walk_expression(assignment.expression)
            )
            body = (
                sv_pipeline._emit_pipeline(module)
                if has_staging else sv_module_rendering.emit_combinational(module)
            )
        else:
            sv_accounting.account_emission_plan(
                module,
                "combinational",
                module_features.ModuleFeatureGroup.CONNECTIONS,
            )
            body = sv_module_rendering.emit_combinational(module)
    return "`default_nettype none\n" + body + "`default_nettype wire\n"


def _composed_rendering(
    external_mapping_index: dict[str, ExternalPhysicalMapping] | None = None,
    *,
    formal_buffer_counts: tuple[composed.FormalBufferCountProjection, ...] = (),
    hierarchy_cache: ir_hierarchy.HierarchyTraversalCache | None = None,
) -> composed.ComposedRendering:
    return composed.ComposedRendering(
        emit_external=lambda module, name: _emit_external_wrapper(
            module,
            name,
            (external_mapping_index or {})[
                module.external_contract.semantic_identity
            ],
        ),
        emit_cdc_child=_emit_composed_cdc_child,
        hierarchy_cache=(
            hierarchy_cache or ir_hierarchy.HierarchyTraversalCache()
        ),
        formal_buffer_counts=formal_buffer_counts,
    )


def _emit_composed_cdc_child(module: ir_module.Module) -> str:
    return sv_cdc.emit_cdc_module(module, _cdc_rendering(module))


def _cdc_rendering(
    module: ir_module.Module | None = None, *, ram_style: str | None = None,
) -> sv_cdc.CDCRendering:
    def emit_module(
        typed_module: ir_module.Module,
        ports: list[str],
        lines: list[str],
    ) -> str:
        if module is None or not (
            typed_module.registers
            or typed_module.next_assignments
            or typed_module.rules
            or typed_module.assignments
        ):
            return sv_module_rendering._module(typed_module, ports, lines)

        crossing = next(
            item for item in typed_module.connections
            if item.crossing is not None
        )
        endpoint_names = {
            crossing.source.name,
            crossing.destination.name,
        }
        extra_ports = [
            sv_boundary._port_declaration(port)
            for port in typed_module.ports
            if port.name not in endpoint_names
            and port.protocol is ir_interfaces.InterfaceProtocol.WIRE
        ]
        declarations, _assignments, render = (
            sv_materialized._materialized_emission(typed_module)
        )
        state_lines: list[str] = []
        if typed_module.registers or typed_module.next_assignments or typed_module.rules:
            sv_state._append_unified_state(
                typed_module,
                declarations,
                state_lines,
                render,
            )
        else:
            state_lines.extend(
                f"  assign {sv_rendering._identifier(sv_rendering._assignment_name(assignment))} = "
                f"{render(assignment.expression)};"
                for assignment in typed_module.assignments
            )
        return sv_module_rendering._module(
            typed_module,
            [*ports, *extra_ports],
            [*declarations, *lines, *state_lines],
        )

    return sv_cdc.CDCRendering(
        emit_module,
        lambda typed_module, memory: sv_state._ported_memory_fragments(
            typed_module, memory, sv_expression._expression, ram_style=ram_style
        ),
    )


def _emit_target_async_fifo(module: ir_module.Module) -> str:
    """Emit the selected typed FIFO decomposition with inferred block storage."""

    return (
        "`default_nettype none\n"
        + sv_cdc.emit_cdc_module(module, _cdc_rendering(module, ram_style="block"))
        + "`default_nettype wire\n"
    )


def emit_artifact(module: ir_module.Module, *, selected_ir_identity: str | None = None,
                  recursive_design: object | None = None,
                  external_mappings: tuple[ExternalPhysicalMapping, ...] = ()) -> BackendArtifact:
    """Emit direct SV and publish its explicit artifact-bound manifest."""
    recursive_hierarchy = (
        sv_formal.validate_recursive_hierarchy(module, recursive_design)
        if recursive_design is not None else None
    )
    text = emit(module, external_mappings=external_mappings)
    identity = selected_ir_identity or ir_module.default_selected_ir_identity(module)
    names = sv_boundary.top_physical_rtl_names(module)
    if recursive_design is not None:
        assert recursive_hierarchy is not None
        hierarchy, _ = recursive_hierarchy
        # Reserve the exact helper spellings used by emission. Semantic call
        # names carry provenance, while physical helper names intentionally do
        # not; a collision must resolve identically in RTL and its locators.
        naming_hierarchy = sv_physicalization.validated_hierarchy(
            sv_physicalization.physicalize_generic_callables(module)
        )
        component_names = naming.build_component_name_plan(naming_hierarchy)
        local_plans = {
            entry.physical_path: emission_context.module_rtl_names(entry.module)
            for entry in naming_hierarchy.entries
        }
        # The selected public top owns both boundary bridges and architectural
        # state. Production locators therefore begin directly at that module.
        # Formal-only observation ports have their own publication route.
        state_root = sv_boundary.physical_state_root_path(
            naming_hierarchy.root.module
        )
        core_path = state_root[2:]
        root_rtl_module = identifiers.rtl_identifier(module.name)
        digest = hashlib.sha256(text.encode()).hexdigest()
        bound = []
        for item in recursive_design.bindings:
            hierarchy_entry = hierarchy.at(tuple(item.physical_instance_path))
            observed_module = naming_hierarchy.at(tuple(item.physical_instance_path)).module
            token = sv_formal.recursive_signal_token(
                observed_module, item.ref.local_semantic_id
            )
            locator = None
            if token is not None:
                rtl_module = (
                    root_rtl_module
                    if item.ref.instance_identity == recursive_design.root_instance_identity
                    else composed.component_name(
                        hierarchy_entry.module,
                        hierarchy_entry.specialization_identity,
                        replace(
                            _composed_rendering(),
                            component_names=component_names,
                        ),
                    )
                )
                locator = BackendPhysicalLocator(
                    "direct_systemverilog", digest, rtl_module,
                    core_path + naming.rtl_hierarchy_instance_path(
                        naming_hierarchy, tuple(item.physical_instance_path),
                        plans=local_plans,
                    ),
                    token, None,
                )
            bound.append(replace(item, locator=locator))
        recursive_design = replace(recursive_design, bindings=tuple(bound))
    return publish_artifact(module, text, backend="direct_systemverilog",
                            selected_ir_identity=identity, rtl_names=names,
                            recursive_design=recursive_design,
                            companions=collect_rom_companions(module))


def emit_artifact_with_source_map(
    module: ir_module.Module,
    *,
    selected_ir_identity: str | None = None,
    recursive_design: object | None = None,
    external_mappings: tuple[ExternalPhysicalMapping, ...] = (),
) -> tuple[BackendArtifact, GeneratedSourceMap]:
    """Publish direct SV plus an exact, deterministic source-map sidecar."""

    artifact = emit_artifact(
        module,
        selected_ir_identity=selected_ir_identity,
        recursive_design=recursive_design,
        external_mappings=external_mappings,
    )
    return artifact, build_generated_source_map(module, artifact)


def emit_formal_artifact(module: ir_module.Module, recursive_design: object, *,
                         selected_ir_identity: str | None = None) -> BackendArtifact:
    """Emit a formal-only closed component ABI with explicit observations."""
    sv_formal.validate_recursive_hierarchy(module, recursive_design)
    observations = sorted(
        recursive_design.bindings, key=lambda item: item.semantic_binding_id
    )
    top_tokens = {
        item.semantic_binding_id: f"zlang_formal_obs_{index}"
        for index, item in enumerate(observations)
    }
    (
        formal_module,
        available,
        formal_buffer_counts,
        formal_adapter_counts,
    ) = sv_formal.instrument_direct_formal_module(
        module, recursive_design, top_tokens
    )
    text = emit(
        formal_module,
        _formal_buffer_counts=formal_buffer_counts,
        _formal_adapter_counts=formal_adapter_counts,
    )
    formal_module_name = sv_rendering._identifier(formal_module.name)
    digest = hashlib.sha256(text.encode()).hexdigest()
    bound = []
    for item in recursive_design.bindings:
        locator = None
        if item.semantic_binding_id in available:
            locator = BackendPhysicalLocator(
                "direct_systemverilog", digest, formal_module_name,
                (), item.ref.local_semantic_id,
                top_tokens[item.semantic_binding_id],
            )
        bound.append(replace(item, locator=locator))
    formal_design = replace(recursive_design, bindings=tuple(bound))
    identity = selected_ir_identity or ir_module.default_selected_ir_identity(module)
    artifact = publish_artifact(
        module, text, backend="direct_systemverilog",
        selected_ir_identity=identity,
        rtl_names=sv_boundary.top_physical_rtl_names(module),
        recursive_design=formal_design,
        formal_artifact_hash=digest,
        companions=collect_rom_companions(module),
    )
    # ``publish_artifact`` also carries production-only internal equivalence
    # locators (for example the parent RR ledger).  Those signals may appear in
    # the formal implementation text, but they are not public observation ports and
    # must never be instantiated as such by the safety verification harness.  Formal-only
    # observations are published separately through ``formal_observations``.
    public_ids = {
        "clock",
        "reset",
        *(leaf.leaf_semantic_id for leaf in module.top_physical_abi.leaves),
    }
    artifact = replace(
        artifact,
        bindings=tuple(
            item
            if item.semantic_signal_id in public_ids
            else replace(item, rtl_path="", physical_available=False)
            for item in artifact.bindings
        ),
    )
    # ``publish_artifact`` starts from the semantic module so that all public
    # and recursive identities remain those of the production design.  This
    # artifact's physical implementation, however, is the closed formal
    # module above. Keep the formal-only physical ABI truthful even when a
    # property needs only clock/reset (and therefore has no observation port
    # from which the connector could otherwise identify the formal module).
    artifact = replace(
        artifact,
        module=formal_module_name,
        bindings=tuple(
            replace(item, rtl_module=formal_module_name)
            for item in artifact.bindings
        ),
        physical_domains=tuple(
            replace(item, rtl_module=formal_module_name)
            for item in artifact.physical_domains
        ),
    )
    # Exercise the complete manifest/link codec before formal metadata can be
    # cached or published in an immutable verification bundle.
    BackendArtifact.from_json(artifact.to_json())
    return artifact
