"""Fail-closed direct SystemVerilog emission for the supported-secondary subset.

The backend consumes typed semantic IR, emits only validated shapes, and reports
unsupported regions before publishing a BackendArtifact.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
import hashlib
import re
from typing import Callable

from zlang.ir import csr as ir_csr
from zlang.ir import expressions as expr
from zlang.ir.arbitration import ArbitrationPolicy, GrantScope
from zlang.ir.callables import (
    CallableReachabilityError,
    reachable_module_callables,
)
from zlang.ir.interfaces import (
    ConnectionAdapter,
    CreditSignal,
    InterfaceProtocol,
    ReadyValidSignal,
    RequestResponseChannel,
    RequestResponseOrdering,
    RequestResponseRole,
    VirtualChannelCreditSignal,
)
from zlang.ir.module import (
    Assignment,
    Function,
    Module,
    Port,
    PortDirection,
    Register,
    Rule,
    default_selected_ir_identity,
)
from zlang.ir.storage import (
    FifoSignal,
    MemoryCollision,
    MemoryResetPolicy,
    MemorySignal,
    RomSignal,
)
from zlang.ir.state import (
    FifoOccupancy,
    StateActionKind,
    StateResourceKind,
    action_activation_predicate_index,
    conditional_activation_predicates,
    conditional_actions,
    groups_conflict,
    ordered_groups as ordered_state_groups,
    selection_regions,
)
from zlang.ir.types import (
    BitType,
    BitsType,
    EnumType,
    FixedType,
    HardwareType,
    SIntType,
    StructType,
    TaggedUnionType,
    TupleType,
    UFixedType,
    UIntType,
    VecType,
)
from zlang.backend.companions import collect_rom_companions, companion_for_rom
from zlang.backend.external import ExternalMappingError, ExternalPhysicalMapping
from zlang.backend.expression_materialization import (
    MaterializedExpression as _MaterializedExpression,
    dependency_ordered_materialization,
    expression_children as _expression_children,
    module_expression_roots as _common_module_expression_roots,
    plan_materialization,
    replace_materialized as _replace_materialized,
    walk_expression as _walk_expression,
)
from zlang.backend.identifiers import (
    allocate_private_rtl_identifier,
    first_rtl_leaf_identifier_collision,
    rtl_identifier,
    rtl_instance_identifier,
    rtl_memory_cells_identifier,
    rtl_memory_read_data_identifier,
    rtl_register_state_identifier,
)
from zlang.backend.naming import (
    ComponentNamePlan,
    ModuleRtlNames,
    RtlNamingError,
    build_component_name_plan,
    module_rtl_names,
    rtl_hierarchy_instance_path,
)
from zlang.backend.manifest import BackendArtifact, publish_artifact
from zlang.backend.module_features import (
    ModuleFeatureAccountingError,
    ModuleFeatureGroup,
    claims_for_groups,
    module_feature_inventory,
    unsupported_legacy_state_mix,
    validate_feature_claims,
)
from zlang.backend.source_map import GeneratedSourceMap, build_generated_source_map
from zlang.backend.systemverilog.cdc import (
    CDCRendering,
    emit_cdc_module as _emit_cdc_subsystem,
)
from zlang.backend.systemverilog.composed import (
    ComposedLeafServices,
    ComposedRendering,
    FormalBufferCountProjection,
    SVPhysicalSyntax,
    composed_component_identifier_claims,
    component_name as _composed_component_name,
    emit_composed_design as _emit_composed_subsystem,
    request_response_tracker_name as _composed_rr_tracker_name,
    rv_fifo_helper as _composed_rv_fifo_helper,
)
from zlang.backend.systemverilog.syntax import sized_decimal as _sized_decimal
from zlang.common.systemverilog import (
    render_ordered_comparison,
    render_right_shift,
    render_typed_resize,
)
from zlang.backend.systemverilog.sequential import (
    PhysicalDomainError as _PhysicalDomainError,
    clock_event as _clock_event,
    effective_reset_signal as _effective_reset_signal,
    module_domain as _module_domain,
    native_release_module as _native_release_module,
    reset_conditioner_lines as _reset_conditioner_lines,
    reset_asserted as _reset_asserted,
    reset_deasserted as _reset_deasserted,
)
from zlang.fixed_point import quantize_rational
from zlang.ir.cdc import ResetPolarity
from zlang.ir.hierarchy import (
    HierarchyError,
    HierarchyIndex,
    HierarchyTraversalCache,
    build_hierarchy_index,
)
from zlang.ir.functional import (
    lower_reduction,
    materialize_exact_reduction,
    materialize_functional_region,
)
from zlang.ir.recursive_formal import BackendPhysicalLocator
from zlang.ir.top_abi import build_top_physical_abi
from zlang.ir.formal_observations import (
    RequestResponseObservationSignal,
    port_observation_id,
    request_response_observation_id,
    rule_fire_observation_id,
)
from zlang.diagnostics import DiagnosticError


class SystemVerilogEmissionError(DiagnosticError):
    """Typed IR is outside the supported direct-SystemVerilog subset."""

    default_code = "ZL-BACKEND-SYSTEMVERILOG-001"

    def __init__(
        self,
        message: str,
        *,
        semantic_path: tuple[str, ...] = (),
        code: str | None = None,
        primary=None,
        notes=(),
        fixes=(),
    ) -> None:
        super().__init__(
            message,
            code=code,
            primary=primary,
            notes=notes,
            fixes=fixes,
        )
        self.semantic_path = semantic_path


@dataclass(frozen=True)
class FormalAdapterCountProjection:
    """Typed formal-only count projection for one closed protocol adapter.

    Every field is checked against the authoritative ``Connection`` before RTL
    is emitted.  The physical signal is allocated here and passed explicitly
    to the FIFO helper; no generated instance or counter name is recovered.
    """

    observation_semantic_id: str
    source_port: str
    destination_port: str
    adapter: ConnectionAdapter
    signal: str
    width: int
    depth: int


@dataclass(frozen=True)
class SystemVerilogCapabilityReport:
    supported: bool
    module: str
    semantic_path: tuple[str, ...] = ()
    reason: str | None = None


def _external_contracts(module: Module) -> tuple[object, ...]:
    contracts: list[object] = []

    def visit(current: Module) -> None:
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
    module: Module,
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
    module: Module,
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
        f".{mapped[port.name]}({_identifier(port.name)})"
        for port in module.ports
    ]
    body = [
        f"  {mapping.physical_module_name} external_impl (\n    "
        + ",\n    ".join(connections)
        + "\n  );"
    ]
    return _named_module(
        wrapper_name,
        _physical_port_declarations(module),
        body,
        typed_module=replace(
            module,
            assignments=(),
            functions=(),
            callable_definitions=(),
        ),
    )


def capability_report(module: Module) -> SystemVerilogCapabilityReport:
    """Return the fail-closed result of lowering the complete selected module."""
    try:
        emit(module)
    except SystemVerilogEmissionError as error:
        return SystemVerilogCapabilityReport(False, module.name, error.semantic_path, str(error))
    return SystemVerilogCapabilityReport(True, module.name)


_VALUE_GROUPS = (
    ModuleFeatureGroup.ASSIGNMENTS,
    ModuleFeatureGroup.LOCALS,
)
_STATE_GROUPS = (
    ModuleFeatureGroup.REGISTERS,
    ModuleFeatureGroup.NEXT_ASSIGNMENTS,
    ModuleFeatureGroup.RULES,
)
_STORAGE_GROUPS = (
    ModuleFeatureGroup.FIFOS,
    ModuleFeatureGroup.MEMORIES,
    ModuleFeatureGroup.ROMS,
)
_PROTOCOL_GROUPS = (
    ModuleFeatureGroup.REQUEST_RESPONSE_INTERFACES,
    ModuleFeatureGroup.PROTOCOL_PORTS,
    ModuleFeatureGroup.HIERARCHICAL_PROTOCOL_ENDPOINTS,
    ModuleFeatureGroup.AGGREGATE_PROTOCOL_ENDPOINTS,
    ModuleFeatureGroup.CREDIT_PORTS,
    ModuleFeatureGroup.VC_CREDIT_PORTS,
)
_HIERARCHY_GROUPS = (
    ModuleFeatureGroup.INSTANCES,
    ModuleFeatureGroup.CONNECTIONS,
    ModuleFeatureGroup.HIERARCHICAL_CONNECTIONS,
    ModuleFeatureGroup.AGGREGATE_PROTOCOL_CONNECTIONS,
    ModuleFeatureGroup.REQUEST_RESPONSE_LEDGERS,
)
_COMPOSED_GROUPS = (
    *_STATE_GROUPS,
    *_STORAGE_GROUPS,
    ModuleFeatureGroup.CSR_BLOCKS,
    *_PROTOCOL_GROUPS,
    ModuleFeatureGroup.ARBITERS,
    *_HIERARCHY_GROUPS,
)


def _account_emission_plan(
    module: Module,
    plan: str,
    *extra: ModuleFeatureGroup,
) -> None:
    """Fail before rendering when a selected plan would omit typed entities."""

    try:
        inventory = module_feature_inventory(module)
        validate_feature_claims(
            inventory,
            claims_for_groups(module, plan, (*_VALUE_GROUPS, *extra)),
            backend="direct_systemverilog",
            plan=plan,
        )
    except ModuleFeatureAccountingError as error:
        raise SystemVerilogEmissionError(
            str(error),
            semantic_path=(module.name,),
            code="ZL-BACKEND-SYSTEMVERILOG-FEATURE-ACCOUNTING",
        ) from error


def emit(
    module: Module,
    *,
    external_mappings: tuple[ExternalPhysicalMapping, ...] = (),
    _formal_buffer_counts: tuple[FormalBufferCountProjection, ...] = (),
    _formal_adapter_counts: tuple[FormalAdapterCountProjection, ...] = (),
) -> str:
    """Emit one design, reporting impossible physical allocations structurally."""

    try:
        return _emit_named_design(
            module,
            external_mappings=external_mappings,
            _formal_buffer_counts=_formal_buffer_counts,
            _formal_adapter_counts=_formal_adapter_counts,
        )
    except RtlNamingError as error:
        raise SystemVerilogEmissionError(
            str(error), semantic_path=(module.name,),
            code="ZL-BACKEND-SYSTEMVERILOG-NAMING",
        ) from error


def _emit_named_design(
    module: Module,
    *,
    external_mappings: tuple[ExternalPhysicalMapping, ...] = (),
    _formal_buffer_counts: tuple[FormalBufferCountProjection, ...] = (),
    _formal_adapter_counts: tuple[FormalAdapterCountProjection, ...] = (),
) -> str:
    """Emit one supported design with an always-leaf public top boundary.

    Component modules intentionally retain the compiler's compact packed ABI.
    When the selected top contains an aggregate endpoint or a struct-valued
    port, the packed implementation is renamed to a backend-private core and a
    deterministic public wrapper exposes the typed leaves.  This keeps the
    hierarchy efficient without making users reconstruct ZLang's packing
    convention when integrating the generated IP.
    """

    # Generic specialization identities deliberately retain exact dependency
    # provenance for build/proof caching.  That provenance must not leak into
    # physical helper names, otherwise a spelling-only dependency edit changes
    # byte-identical RTL.  Render from an ephemeral copy whose generic callable
    # names are derived from the exact typed body/signature instead.
    physical_module = _physicalize_generic_callables(module)
    hierarchy_cache = HierarchyTraversalCache()
    public_abi = build_top_physical_abi(physical_module)
    _validate_public_leaf_identifiers(tuple(public_abi.leaves))
    _validate_state_storage_rtl_namespace(
        physical_module, hierarchy_cache=hierarchy_cache
    )
    packed = _emit_packed(
        physical_module,
        external_mappings=external_mappings,
        formal_buffer_counts=_formal_buffer_counts,
        formal_adapter_counts=_formal_adapter_counts,
        hierarchy_cache=hierarchy_cache,
    )
    return _emit_public_leaf_boundary(
        physical_module, packed, leaves=tuple(public_abi.leaves)
    )


def _validate_state_storage_rtl_namespace(
    module: Module,
    *,
    hierarchy_cache: HierarchyTraversalCache | None = None,
) -> None:
    """Reject colliding architectural-state tokens before rendering RTL.

    Writable-memory storage uses two backend-owned suffixes.  A source register
    such as ``foo_cells`` must therefore not alias the cells of memory ``foo``.
    The access companion consumes the same identifier helpers, so accepting a
    collision here would also give two semantic bindings one VPI locator.
    """

    selected_hierarchy_cache = hierarchy_cache or HierarchyTraversalCache()
    _validated_hierarchy(module, cache=selected_hierarchy_cache)
    tokens: dict[str, str] = {}
    local_names = module_rtl_names(module)

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

    for leaf in build_top_physical_abi(module).leaves:
        logical = leaf.packed_root_external_name or leaf.external_name
        claim(rtl_identifier(logical), f"port '{logical}'")
    for elaborated in module.elaborated_instances:
        name = elaborated.instance.name
        claim(local_names.instance(name), f"child instance '{name}'")
    if (
        module.elaborated_instances
        or module.hierarchical_connections
        or module.request_response_connections
        or module.aggregate_protocol_connections
    ):
        for token, owner in composed_component_identifier_claims(
            module,
            _composed_rendering(hierarchy_cache=selected_hierarchy_cache),
        ):
            claim(token, owner)
    for item in _materialization_plan(module):
        claim(item.name, f"materialized expression '{item.name}'")
    for root in _module_expression_roots(module):
        for value in _walk_expression(root):
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
            rtl_register_state_identifier(register.name),
            f"register '{register.name}'",
        )
    for fifo in module.fifos:
        name = rtl_identifier(fifo.name)
        if fifo.scheduled:
            suffixes = (
                "storage", "count", "rd", "wr", "push", "pop", "push_data",
                "front", "empty", "full", "valid", "ready", "overflow",
                "underflow",
            )
        else:
            referenced = _referenced_fifo_signals(module, fifo)
            suffixes = (
                "storage", "count", "rd", "wr", "push_request", "pop_request",
                "push", "pop",
                *(
                    signal.value
                    for signal in (
                        FifoSignal.FRONT,
                        FifoSignal.EMPTY,
                        FifoSignal.FULL,
                        FifoSignal.VALID,
                        FifoSignal.READY,
                        FifoSignal.OVERFLOW,
                        FifoSignal.UNDERFLOW,
                    )
                    if signal in referenced
                ),
            )
        for suffix in suffixes:
            claim(f"{name}_{suffix}", f"FIFO '{fifo.name}' {suffix}")
    for memory in module.memories:
        name = rtl_identifier(memory.name)
        claim(rtl_memory_cells_identifier(memory.name), f"memory '{memory.name}' cells")
        claim(
            rtl_memory_read_data_identifier(memory.name),
            f"memory '{memory.name}' read data",
        )
        if memory.scheduled:
            for suffix in (
                "read_fire", "write_fire", "read_address", "write_address",
                "write_data", "reset_index",
            ):
                claim(f"{name}_{suffix}", f"memory '{memory.name}' {suffix}")
        else:
            claim("zlang_memory_reset_index", "memory reset iterator")
        if memory.write_mask_width is not None:
            for suffix in ("write_mask", "write_mask_expanded", "write_merged"):
                claim(f"{name}_{suffix}", f"memory '{memory.name}' {suffix}")
    for rom in module.roms:
        name = rtl_identifier(rom.name)
        claim(f"{name}_cells", f"ROM '{rom.name}' cells")
        claim(f"{name}_read_data", f"ROM '{rom.name}' read data")
    # The compact register path emits no guard/fire helpers. Claim only real
    # objects, and allocate emitted helpers around source state names.
    if _requires_unified_state(module):
        for rule in module.rules:
            claim(local_names.rule(rule.name, "guard"), f"rule '{rule.name}' guard")
            claim(local_names.rule(rule.name, "fire"), f"rule '{rule.name}' fire")
    if module.resolved_transition is not None:
        for index, _activation in enumerate(
            conditional_activation_predicates(module.resolved_transition)
        ):
            claim(
                f"zlang_condition_{index}_active",
                f"conditional-action predicate {index}",
            )
    for child in module.children:
        _validate_state_storage_rtl_namespace(
            child, hierarchy_cache=selected_hierarchy_cache
        )


_PHYSICAL_CALLABLE_SCHEMA = "zlang-systemverilog-physical-callable-v1"


def _physicalize_generic_callables(module: Module) -> Module:
    """Return an emission-only module with provenance-free helper names.

    Semantic callable identities remain authoritative on the input IR and in
    BackendArtifact metadata.  This copy only prevents dependency/source
    provenance from becoming an RTL identifier.  Nested calls use the physical
    identity of the referenced typed body, so a dependency-sensitive callee ID
    cannot leak indirectly through a caller's body fingerprint.
    """

    definitions: dict[str, object] = {}
    specializations: dict[str, object] = {}

    def collect(current: Module) -> None:
        for function in (*current.functions, *current.callable_definitions):
            identity = str(function.callee_identity)
            # A hierarchy may carry the same semantic definition through
            # several specialized children with different diagnostic origins.
            # The semantic identity is authoritative; each child still runs
            # the normal local reachability/conflict validation before text is
            # published.
            definitions.setdefault(identity, function)
        for specialization in current.generic_specializations:
            specializations.setdefault(specialization.identity, specialization)
        for child in current.children:
            collect(child)

    collect(module)
    memo: dict[str, str] = {}
    active: set[str] = set()

    def normalize(value: object) -> object:
        if isinstance(value, expr.Call):
            target = definitions.get(value.callee_identity or "")
            callee = (
                digest(target)
                if target is not None
                else (value.callee_identity or f"legacy:{value.function}")
            )
            return (
                "Call",
                callee,
                tuple(normalize(item) for item in value.arguments),
                normalize(value.type),
            )
        if isinstance(value, Enum):
            return (type(value).__module__, type(value).__name__, value.value)
        if isinstance(value, tuple):
            return tuple(normalize(item) for item in value)
        if is_dataclass(value) and not isinstance(value, type):
            return (
                type(value).__module__,
                type(value).__name__,
                tuple(
                    (item.name, normalize(getattr(value, item.name)))
                    for item in fields(value)
                    if item.name not in {
                        "origin",
                        "source_origin",
                        "formal_records",
                        "formal_eligible",
                        "callee_identity",
                    }
                ),
            )
        return value

    def digest(function: object) -> str:
        semantic_identity = str(getattr(function, "callee_identity"))
        cached = memo.get(semantic_identity)
        if cached is not None:
            return cached
        if semantic_identity in active:
            raise SystemVerilogEmissionError(
                "recursive typed callable cycle while deriving physical helper names"
            )
        active.add(semantic_identity)
        metadata = getattr(function, "metadata", None)
        source_name = (
            str(getattr(metadata, "source_name", ""))
            if metadata is not None
            else str(getattr(function, "name"))
        )
        specialization = specializations.get(semantic_identity)
        binding_payloads: dict[str, object] = {}
        if specialization is not None:
            for binding in specialization.bindings:
                if binding.kind.value == "constant":
                    binding_payloads[binding.name] = (
                        "constant",
                        normalize(binding.canonical_type),
                        normalize(binding.canonical_value),
                        binding.evaluator_schema,
                    )
                else:
                    target = definitions.get(binding.callee_identity or "")
                    binding_payloads[binding.name] = (
                        "callable",
                        tuple(normalize(item) for item in binding.parameter_types),
                        normalize(binding.return_type),
                        digest(target) if target is not None else binding.callee_identity,
                        binding.evaluator_schema,
                    )
        generic_arguments = (
            tuple(
                (name, binding_payloads.get(name, rendered))
                for name, rendered in specialization.arguments
            )
            if specialization is not None
            else tuple(getattr(metadata, "arguments", ()))
        )
        payload = (
            _PHYSICAL_CALLABLE_SCHEMA,
            source_name,
            generic_arguments,
            tuple(
                (parameter.name, normalize(parameter.type))
                for parameter in getattr(function, "parameters")
            ),
            normalize(getattr(function, "return_type")),
            normalize(getattr(function, "body")),
        )
        result = hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()
        active.remove(semantic_identity)
        memo[semantic_identity] = result
        return result

    generic_identities = {
        identity
        for identity, function in definitions.items()
        if str(getattr(function, "name", "")).startswith("zlang_spec_")
        or (
            getattr(function, "metadata", None) is not None
            and getattr(function.metadata, "specialization_identity", None) is not None
        )
    }
    physical = {identity: digest(definitions[identity]) for identity in generic_identities}
    if not physical:
        return module

    def rewrite(value: object) -> object:
        if isinstance(value, expr.Call):
            arguments = tuple(rewrite(item) for item in value.arguments)
            identity = value.callee_identity or ""
            replacement = physical.get(identity)
            if replacement is None:
                return replace(value, arguments=arguments)
            return replace(
                value,
                function=f"zlang_spec_{replacement}",
                arguments=arguments,
                callee_identity=replacement,
            )
        if isinstance(value, Function):
            body = rewrite(value.body)
            identity = value.callee_identity
            replacement = physical.get(identity)
            if replacement is None:
                return replace(value, body=body)
            metadata = value.metadata
            return replace(
                value,
                name=f"zlang_spec_{replacement}",
                body=body,
                callee_identity=replacement,
                metadata=(
                    replace(metadata, specialization_identity=replacement)
                    if metadata is not None
                    else None
                ),
            )
        if isinstance(value, tuple):
            return tuple(rewrite(item) for item in value)
        if is_dataclass(value) and not isinstance(value, type):
            field_names = {item.name for item in fields(value)}
            updates = {
                item.name: rewrite(getattr(value, item.name))
                for item in fields(value)
                if item.init and item.name not in {"type", "origin", "source_origin"}
            }
            # Functional reduction descriptors carry callable references but
            # are not Function definitions.  Rewrite that exact typed pair;
            # leave specialization-binding provenance records untouched.
            if {"function", "callee_identity"} <= field_names:
                identity = str(getattr(value, "callee_identity", ""))
                replacement = physical.get(identity)
                if replacement is not None:
                    updates["function"] = f"zlang_spec_{replacement}"
                    updates["callee_identity"] = replacement
            return replace(value, **updates) if updates else value
        return value

    rewritten = rewrite(module)
    if not isinstance(rewritten, Module):
        raise SystemVerilogEmissionError("physical callable lowering lost module type")
    return rewritten


def _uses_dedicated_legacy_storage(module: Module) -> bool:
    """Return whether the frozen global-control storage emitter owns state."""

    transition = module.resolved_transition
    if (
        transition is None
        or transition.action_groups
        or module.registers
        or module.rules
        or module.next_assignments
        or module.elaborated_instances
        or module.children
        or module.connections
        or module.hierarchical_connections
        or module.request_response_connections
        or module.aggregate_protocol_connections
    ):
        return False
    resource_kinds = {resource.kind for resource in transition.resources}
    if (
        module.fifos
        and all(not fifo.scheduled for fifo in module.fifos)
        and not module.memories
        and not module.roms
    ):
        return resource_kinds <= {StateResourceKind.FIFO}
    if (
        module.memories
        and all(not memory.scheduled for memory in module.memories)
        and not module.fifos
        and not module.roms
    ):
        return resource_kinds <= {StateResourceKind.MEMORY}
    return False


def _requires_unified_state(module: Module) -> bool:
    """Return whether typed state needs the exact unified scheduler.

    The compact rule emitter is deliberately limited to scalar register writes
    whose accepted effects are exactly representable by its per-register
    conditional chains.  A conflict between two one-effect groups is safe on
    that path; a conflict involving any multi-effect group is not, because it
    could commit part of a lower-priority rule after that rule lost elsewhere.
    Protocol state, storage legality, conditional action activation, output
    effects, and those atomic multi-effect conflicts therefore stay on the
    unified path.
    """

    transition = module.resolved_transition
    if transition is None:
        return False
    if _uses_dedicated_legacy_storage(module):
        return False
    if not (
        transition.resources
        or transition.action_groups
        or module.registers
        or module.rules
        or module.next_assignments
        or any(fifo.scheduled for fifo in module.fifos)
        or any(memory.scheduled for memory in module.memories)
        or module.roms
    ):
        return False
    # Closed zero-rule sequential state historically uses the unified body,
    # including its begin/end reset spelling.  It has no rule dimensions to
    # enumerate, so retaining that route is both byte-compatible and bounded.
    if (
        not module.rules
        and not transition.action_groups
        and (module.registers or module.next_assignments)
    ):
        return True
    if (
        any(fifo.scheduled for fifo in module.fifos)
        or any(memory.scheduled for memory in module.memories)
        or bool(module.roms)
        or bool(module.request_responses)
        or bool(module.aggregate_protocol_endpoints)
        or any(
            port.protocol is not InterfaceProtocol.WIRE
            for port in module.ports
        )
        or any(
            endpoint.protocol is not InterfaceProtocol.WIRE
            for endpoint in module.protocol_endpoints
        )
        or any(
            resource.kind is not StateResourceKind.REGISTER
            for resource in transition.resources
        )
        or any(
            action.kind is not StateActionKind.REGISTER_WRITE
            or action.activation is not None
            for group in transition.action_groups
            for action in group.actions
        )
    ):
        return True
    groups = transition.action_groups
    return any(
        groups_conflict(left, right)
        and (len(left.actions) != 1 or len(right.actions) != 1)
        for index, left in enumerate(groups)
        for right in groups[index + 1:]
    )


def _emit_packed(
    module: Module,
    *,
    external_mappings: tuple[ExternalPhysicalMapping, ...] = (),
    formal_buffer_counts: tuple[FormalBufferCountProjection, ...] = (),
    formal_adapter_counts: tuple[FormalAdapterCountProjection, ...] = (),
    hierarchy_cache: HierarchyTraversalCache | None = None,
) -> str:
    """Emit one supported typed module as direct synthesizable SystemVerilog."""

    if len(module.clock_domains) == 1:
        try:
            _module_domain(module)
        except _PhysicalDomainError as error:
            raise SystemVerilogEmissionError(str(error)) from error
    elif any(not domain.is_legacy_default for domain in module.clock_domains):
        raise SystemVerilogEmissionError(
            "non-default physical clock/reset contracts are not supported on "
            "multi-domain direct-SystemVerilog modules"
        )

    unsafe_state_engines = unsupported_legacy_state_mix(module)
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
        _account_emission_plan(
            module,
            "cdc",
            ModuleFeatureGroup.PROTOCOL_PORTS,
            ModuleFeatureGroup.HIERARCHICAL_PROTOCOL_ENDPOINTS,
            ModuleFeatureGroup.AGGREGATE_PROTOCOL_ENDPOINTS,
            ModuleFeatureGroup.CONNECTIONS,
            ModuleFeatureGroup.AGGREGATE_PROTOCOL_CONNECTIONS,
        )
        body = _emit_cdc_subsystem(module, _cdc_rendering())
        return "`default_nettype none\n" + body + "`default_nettype wire\n"
    if any(connection.adapter is not None for connection in module.connections):
        _account_emission_plan(
            module,
            "connection_adapter",
            ModuleFeatureGroup.PROTOCOL_PORTS,
            ModuleFeatureGroup.CONNECTIONS,
            ModuleFeatureGroup.CREDIT_PORTS,
        )
        body = _emit_connection_adapter(
            module, formal_adapter_counts=formal_adapter_counts
        )
        return "`default_nettype none\n" + body + "`default_nettype wire\n"
    if module.elastic_pipeline_regions:
        _account_emission_plan(
            module,
            "elastic_pipeline",
            ModuleFeatureGroup.PROTOCOL_PORTS,
            ModuleFeatureGroup.ELASTIC_PIPELINE_REGIONS,
        )
        body = _emit_elastic_pipeline(module)
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
                interface.ordering is RequestResponseOrdering.IN_ORDER
                for interface in module.request_responses
            )
        )
        or (
            module.is_sequential
            and any(
                port.protocol is InterfaceProtocol.READY_VALID
                for port in module.ports
            )
            and bool(module.registers or module.rules or module.next_assignments)
        )
        or any(connection.buffer_depth for connection in module.connections)
        )
    ):
        _account_emission_plan(module, "composed", *_COMPOSED_GROUPS)
        mapping_index = _external_mapping_index(module, external_mappings)
        body = _emit_composed_design(
            module, mapping_index,
            formal_buffer_counts=formal_buffer_counts,
            hierarchy_cache=hierarchy_cache,
        )
        return (
            "`default_nettype none\n"
            + _external_sources(mapping_index)
            + body
            + "`default_nettype wire\n"
        )
    if module.external_contract is not None:
        _account_emission_plan(module, "external_wrapper")
        mapping_index = _external_mapping_index(module, external_mappings)
        body = _emit_external_wrapper(
            module,
            _identifier(module.name),
            mapping_index[module.external_contract.semantic_identity],
        )
        return (
            "`default_nettype none\n"
            + _external_sources(mapping_index)
            + body
            + "`default_nettype wire\n"
        )
    if module.csr_blocks:
        _account_emission_plan(
            module,
            "csr_composed_state",
            *_STATE_GROUPS,
            ModuleFeatureGroup.CSR_BLOCKS,
            ModuleFeatureGroup.PROTOCOL_PORTS,
        )
        body = _emit_csr(module)
    elif module.request_responses:
        _account_emission_plan(
            module,
            "request_response",
            ModuleFeatureGroup.REQUEST_RESPONSE_INTERFACES,
            *_STATE_GROUPS,
        )
        body = _emit_request_response(module)
    elif _requires_unified_state(module):
        _account_emission_plan(
            module, "unified_state",
            *_STATE_GROUPS,
            *_STORAGE_GROUPS,
            ModuleFeatureGroup.PROTOCOL_PORTS,
        )
        body = _emit_unified_state_module(module)
    elif module.roms:
        _account_emission_plan(module, "rom", ModuleFeatureGroup.ROMS)
        body = _emit_rom_module(module)
    elif module.memories:
        _account_emission_plan(module, "memory", ModuleFeatureGroup.MEMORIES)
        body = _emit_memory(module)
    elif module.fifos:
        _account_emission_plan(
            module,
            "fifo",
            ModuleFeatureGroup.FIFOS,
            ModuleFeatureGroup.PROTOCOL_PORTS,
        )
        body = _emit_fifo(module)
    elif module.arbiters:
        _account_emission_plan(
            module,
            "packet_arbiter",
            ModuleFeatureGroup.ARBITERS,
            ModuleFeatureGroup.PROTOCOL_PORTS,
        )
        body = _emit_packet_arbiter(module)
    elif any(
        port.protocol is InterfaceProtocol.VC_CREDIT for port in module.ports
    ):
        _account_emission_plan(
            module,
            "vc_credit",
            ModuleFeatureGroup.PROTOCOL_PORTS,
            ModuleFeatureGroup.VC_CREDIT_PORTS,
        )
        body = _emit_vc_credit(module)
    elif any(port.protocol is InterfaceProtocol.CREDIT for port in module.ports):
        _account_emission_plan(
            module,
            "credit",
            ModuleFeatureGroup.PROTOCOL_PORTS,
            ModuleFeatureGroup.CREDIT_PORTS,
        )
        body = _emit_credit(module)
    elif any(
        port.protocol is InterfaceProtocol.READY_VALID for port in module.ports
    ):
        _account_emission_plan(
            module,
            "ready_valid",
            ModuleFeatureGroup.PROTOCOL_PORTS,
            ModuleFeatureGroup.HIERARCHICAL_PROTOCOL_ENDPOINTS,
            ModuleFeatureGroup.AGGREGATE_PROTOCOL_ENDPOINTS,
            ModuleFeatureGroup.CONNECTIONS,
            ModuleFeatureGroup.AGGREGATE_PROTOCOL_CONNECTIONS,
        )
        body = _emit_ready_valid(module)
    elif module.rules:
        _account_emission_plan(
            module,
            "rules",
            *_STATE_GROUPS,
        )
        body = _emit_rules(module)
    elif module.is_sequential:
        _account_emission_plan(
            module,
            "sequential",
            ModuleFeatureGroup.REGISTERS,
            ModuleFeatureGroup.NEXT_ASSIGNMENTS,
        )
        has_staging = any(
            isinstance(value, (expr.Delay, expr.Pipeline))
            for assignment in module.assignments
            for value in _walk_expression(assignment.expression)
        )
        body = _emit_pipeline(module) if has_staging else _emit_combinational(module)
    else:
        _account_emission_plan(
            module,
            "combinational",
            ModuleFeatureGroup.CONNECTIONS,
        )
        body = _emit_combinational(module)
    return "`default_nettype none\n" + body + "`default_nettype wire\n"


def _component_name(
    module: Module,
    specialization: str | None,
    *,
    top: bool = False,
    naming_plan: ComponentNamePlan | None = None,
) -> str:
    return _composed_component_name(
        module, specialization,
        replace(_composed_rendering(), component_names=naming_plan), top=top
    )


def _validated_hierarchy(
    module: Module,
    *,
    cache: HierarchyTraversalCache | None = None,
) -> HierarchyIndex:
    try:
        return build_hierarchy_index(module, cache=cache)
    except HierarchyError as error:
        raise SystemVerilogEmissionError(str(error)) from error


def _emit_composed_design(
    module: Module,
    external_mapping_index: dict[str, ExternalPhysicalMapping],
    *,
    formal_buffer_counts: tuple[FormalBufferCountProjection, ...] = (),
    hierarchy_cache: HierarchyTraversalCache | None = None,
) -> str:
    return _emit_composed_subsystem(
        module,
        _composed_rendering(
            external_mapping_index,
            formal_buffer_counts=formal_buffer_counts,
            hierarchy_cache=hierarchy_cache,
        ),
    )


def _validate_recursive_hierarchy(
    module: Module,
    recursive_design: object,
) -> tuple[HierarchyIndex, dict[str, object]]:
    """Bind recursive metadata only to the exact validated typed hierarchy."""

    hierarchy = _validated_hierarchy(module)
    instances = tuple(recursive_design.instances)
    nodes = {item.instance_identity: item for item in instances}
    if len(nodes) != len(instances):
        raise SystemVerilogEmissionError(
            "recursive formal design contains duplicate instance identities"
        )
    root = nodes.get(recursive_design.root_instance_identity)
    if root is None:
        raise SystemVerilogEmissionError(
            "recursive formal design root instance is absent"
        )
    if tuple(root.physical_instance_path) != (module.name,):
        raise SystemVerilogEmissionError(
            "recursive formal root path does not match typed top module"
        )

    node_paths: set[tuple[str, ...]] = set()
    for node in instances:
        path = tuple(node.physical_instance_path)
        if path in node_paths:
            raise SystemVerilogEmissionError(
                "recursive formal design contains duplicate physical path: "
                + ".".join(path)
            )
        node_paths.add(path)
        try:
            entry = hierarchy.at(path)
        except HierarchyError as error:
            raise SystemVerilogEmissionError(str(error)) from error
        if entry.module.name != node.module_name:
            raise SystemVerilogEmissionError(
                f"recursive formal path '{'.'.join(path)}' names module "
                f"'{node.module_name}', not typed module '{entry.module.name}'"
            )
        if entry.elaborated is not None:
            if node.source_instance_name != path[-1]:
                raise SystemVerilogEmissionError(
                    f"recursive formal path '{'.'.join(path)}' has source instance "
                    f"'{node.source_instance_name}'"
                )
            if node.specialization_identity != entry.specialization_identity:
                raise SystemVerilogEmissionError(
                    f"recursive formal path '{'.'.join(path)}' specialization "
                    "does not match typed elaboration"
                )
    hierarchy_paths = {item.physical_path for item in hierarchy.entries}
    if node_paths != hierarchy_paths:
        missing = sorted(".".join(path) for path in hierarchy_paths - node_paths)
        extra = sorted(".".join(path) for path in node_paths - hierarchy_paths)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        raise SystemVerilogEmissionError(
            "recursive formal instance tree does not match typed hierarchy: "
            + "; ".join(details)
        )

    for binding in recursive_design.bindings:
        node = nodes.get(binding.ref.instance_identity)
        if node is None:
            raise SystemVerilogEmissionError(
                f"recursive binding '{binding.semantic_binding_id}' references "
                "an unknown instance identity"
            )
        if tuple(binding.physical_instance_path) != tuple(node.physical_instance_path):
            raise SystemVerilogEmissionError(
                f"recursive binding '{binding.semantic_binding_id}' physical path "
                "does not match its instance"
            )
        if binding.specialization_identity != node.specialization_identity:
            raise SystemVerilogEmissionError(
                f"recursive binding '{binding.semantic_binding_id}' specialization "
                "does not match its instance"
            )
    return hierarchy, nodes


def _physical_port_declarations(module: Module) -> list[str]:
    ports: list[str] = _clock_reset_port_declarations(module)
    for port in module.ports:
        name = _identifier(port.name)
        if port.protocol is InterfaceProtocol.WIRE:
            ports.append(_logic_port("input" if port.direction is PortDirection.INPUT else "output", name, port.type))
        elif port.protocol is InterfaceProtocol.READY_VALID:
            if port.direction is PortDirection.INPUT:
                ports.extend((_logic_port("input", f"{name}_payload", port.type),
                              f"input wire logic {name}_valid", f"output logic {name}_ready"))
            else:
                ports.extend((_logic_port("output", f"{name}_payload", port.type),
                              f"output logic {name}_valid", f"input wire logic {name}_ready"))
        else:
            raise SystemVerilogEmissionError(
                f"composed direct SystemVerilog does not support {port.protocol.value} port '{port.name}'"
            )
    for interface in module.request_responses:
        name = _identifier(interface.name)
        requester = interface.role is not None and interface.role.value == "requester"
        request_out = requester
        response_out = not requester
        ports.extend((
            _logic_port("output" if request_out else "input", f"{name}_request_payload", interface.request_type),
            f"{'output' if request_out else 'input'} logic {name}_request_valid",
            f"{'input' if request_out else 'output'} logic {name}_request_ready",
            _logic_port("output" if response_out else "input", f"{name}_response_payload", interface.response_type),
            f"{'output' if response_out else 'input'} logic {name}_response_valid",
            f"{'input' if response_out else 'output'} logic {name}_response_ready",
        ))
    return ports


def _clock_reset_port_declarations(module: Module) -> list[str]:
    """Render every explicit physical domain exactly once.

    ``module.clock``/``module.reset`` are the legacy single-domain aliases.
    Multi-domain modules intentionally leave those aliases unset, so backend
    ports must be derived from the shared ``TopPhysicalABI`` rather than from
    the aliases or an unused clock/reset would silently disappear.
    """

    return [
        f"input wire logic {_identifier(leaf.external_name)}"
        for leaf in module.top_physical_abi.leaves
        if leaf.signal_kind in {"clock", "reset"}
    ]


def _public_leaf_port_declaration(leaf: object) -> str:
    """Render one public top leaf, preserving vectors as native SV arrays."""

    direction = (
        "input" if leaf.direction is PortDirection.INPUT else "output"
    )
    name = _identifier(leaf.external_name)
    if not leaf.array_dimensions:
        return _logic_port(direction, name, leaf.canonical_type)
    element = leaf.element_type
    signed = " signed" if isinstance(element, (SIntType, FixedType)) else ""
    net = " wire" if direction == "input" else ""
    dimensions = " ".join(
        f"[0:{length - 1}]" for length in leaf.array_dimensions
    )
    return (
        f"{direction}{net} logic{signed} {_range(_width(element))}{name} "
        f"{dimensions}"
    )


def _public_leaf_elements(leaf: object) -> tuple[tuple[object, str], ...]:
    """Return exact packed slices and their public scalar/array expressions."""

    name = _identifier(leaf.external_name)
    return tuple(
        (
            item,
            name + "".join(f"[{index}]" for index in item.indices),
        )
        for item in leaf.packed_element_slices
    )


def _top_core_signal(module: Module, leaf: object) -> str:
    """Map one typed physical-ABI root to the existing private component port."""

    if not leaf.packed_root_external_name:
        raise SystemVerilogEmissionError(
            f"public top ABI leaf '{leaf.leaf_semantic_id}' has no private root"
        )
    if leaf.category in {"clock", "reset"}:
        return _identifier(leaf.packed_root_external_name)
    if leaf.category == "port":
        base = _identifier(leaf.member_path[0])
        return (
            base if leaf.signal_kind == "wire"
            else f"{base}_{leaf.signal_kind}"
        )
    if leaf.category == "request_response":
        interface, channel, signal, *_ = leaf.member_path
        return f"{_identifier(interface)}_{channel}_{signal}"
    if leaf.category == "aggregate":
        endpoint, member, *_ = leaf.member_path
        base = _identifier(f"{endpoint}__{member}")
        return (
            base if leaf.signal_kind == "wire"
            else f"{base}_{leaf.signal_kind}"
        )
    return _identifier(leaf.packed_root_external_name)


def _needs_public_leaf_boundary(module: Module, leaves: tuple[object, ...]) -> bool:
    """Return whether the compact implementation ABI differs from the public one."""

    return any(
        leaf.array_dimensions
        or _identifier(leaf.external_name)
        != _top_core_signal(module, leaf)
        for leaf in leaves
        if leaf.signal_kind not in {"clock", "reset"}
    )


def _validate_public_leaf_identifiers(leaves: tuple[object, ...]) -> None:
    """Reject public names which collide after physical HDL mangling.

    ``TopPhysicalABI`` validates logical external names.  The direct-SV
    reserved-word policy is a second, backend-specific namespace projection:
    for example ``module`` and ``zlang_module`` are distinct ZLang names but
    both become ``zlang_module`` in RTL.  Validate that projection before any
    artifact can be published.
    """

    collision = first_rtl_leaf_identifier_collision(leaves)
    if collision is not None:
        raise SystemVerilogEmissionError(
            "direct-SystemVerilog public top identifier collision after "
            f"mangling: '{collision.first_external_name}' "
            f"({collision.first_semantic_id}) and "
            f"'{collision.second_external_name}' "
            f"({collision.second_semantic_id}) both map to "
            f"'{collision.physical_name}'"
        )


def _allocate_public_wrapper_identifier(
    preferred: str,
    semantic_identity: str,
    used: set[str],
) -> str:
    """Allocate one deterministic wrapper-private HDL identifier."""

    return allocate_private_rtl_identifier(
        preferred, semantic_identity=semantic_identity, used=used,
    )


def _public_core_instance_name(module: Module, leaves: tuple[object, ...]) -> str:
    """Return the exact private core instance name used by the public wrapper."""

    public_names = {_identifier(leaf.external_name) for leaf in leaves}
    return _allocate_public_wrapper_identifier(
        "zlang_top_core", f"instance:{module.name}", public_names
    )


def _public_core_module_name(module: Module) -> str:
    """Name the packed implementation below a public leaf wrapper."""

    return f"{_identifier(module.name)}_zlang_core"


def physical_state_root_path(module: Module) -> tuple[str, ...]:
    """Return the typed VPI root which owns selected-top architectural state.

    This is a backend-published physical locator, not a reconstruction from
    generated text.  The public leaf wrapper, when present, owns no ZLang
    state; the private packed core below it does.
    """

    leaves = tuple(build_top_physical_abi(module).leaves)
    top_name = _identifier(module.name)
    if not _needs_public_leaf_boundary(module, leaves):
        return ("TOP", top_name)
    return ("TOP", top_name, _public_core_instance_name(module, leaves))


def _emit_public_leaf_boundary(
    module: Module,
    packed_text: str,
    *,
    leaves: tuple[object, ...] | None = None,
) -> str:
    """Replace a packed selected top by its deterministic public leaf wrapper."""

    if leaves is None:
        leaves = tuple(build_top_physical_abi(module).leaves)
        _validate_public_leaf_identifiers(leaves)
    if not _needs_public_leaf_boundary(module, leaves):
        return packed_text

    top_name = _identifier(module.name)
    core_name = _public_core_module_name(module)
    definition = f"module {top_name} ("
    if packed_text.count(definition) != 1:
        raise SystemVerilogEmissionError(
            f"cannot identify unique selected top module '{top_name}' for its public ABI"
        )
    packed_text = packed_text.replace(
        definition, f"module {core_name} (", 1
    )

    public_names: set[str] = set()
    public_ports: list[str] = []
    for leaf in leaves:
        name = _identifier(leaf.external_name)
        public_names.add(name)
        public_ports.append(_public_leaf_port_declaration(leaf))
    wrapper_identifiers = set(public_names)
    instance_name = _public_core_instance_name(module, leaves)

    roots: dict[str, list[object]] = {}
    root_order: list[str] = []
    for leaf in leaves:
        root = leaf.packed_root_semantic_id
        if root not in roots:
            roots[root] = []
            root_order.append(root)
        roots[root].append(leaf)

    declarations: list[str] = []
    connections: list[str] = []
    assignments: list[str] = []
    for root in root_order:
        group = roots[root]
        first = group[0]
        core_signal = _top_core_signal(module, first)
        ordered = sorted(
            group,
            key=lambda item: max(
                element.msb for element in item.packed_element_slices
            ),
            reverse=True,
        )
        if first.direction is PortDirection.INPUT:
            elements = [
                element
                for item in group
                for element in _public_leaf_elements(item)
            ]
            parts = [
                value
                for _slice_, value in sorted(
                    elements, key=lambda pair: pair[0].msb, reverse=True
                )
            ]
            value = parts[0] if len(parts) == 1 else "{" + ", ".join(parts) + "}"
            connections.append(f".{core_signal}({value})")
            continue

        root_width = _width(first.packed_root_type)
        temporary = _allocate_public_wrapper_identifier(
            f"zlang_top_core_{core_signal}", root, wrapper_identifiers
        )
        signed = " signed" if isinstance(
            first.packed_root_type, (SIntType, FixedType)
        ) else ""
        declarations.append(
            f"  logic{signed} {_range(root_width)}{temporary};"
        )
        connections.append(f".{core_signal}({temporary})")

        def root_slice(msb: int, lsb: int) -> str:
            if root_width == 1 and msb == 0 and lsb == 0:
                return temporary
            return _slice(temporary, msb, lsb)

        for leaf in ordered:
            name = _identifier(leaf.external_name)
            elements = _public_leaf_elements(leaf)
            if not leaf.array_dimensions:
                item, _ = elements[0]
                assignments.append(
                    f"  assign {name} = "
                    f"{root_slice(item.msb, item.lsb)};"
                )
                continue
            for item, target in elements:
                assignments.append(
                    f"  assign {target} = {root_slice(item.msb, item.lsb)};"
                )

    instance = (
        f"  {core_name} {instance_name} (\n    "
        + ",\n    ".join(connections)
        + "\n  );"
    )
    wrapper = _named_module(
        top_name,
        public_ports,
        [*declarations, instance, *assignments],
    )
    marker = "`default_nettype wire\n"
    position = packed_text.rfind(marker)
    if position < 0:
        raise SystemVerilogEmissionError(
            "generated direct-SystemVerilog artifact is missing its nettype boundary"
        )
    return packed_text[:position] + wrapper + packed_text[position:]


def _request_response_tracker_name(descriptor: object, module: Module) -> str:
    return _composed_rr_tracker_name(descriptor, _composed_rendering(), module)


def _instance_expression(
    module: Module, expression: expr.Expression,
    names: ModuleRtlNames | None = None,
) -> expr.Expression:
    instance_names = {item.instance.name for item in module.elaborated_instances}
    if not instance_names:
        return expression
    local_names = names or module_rtl_names(module)

    def walk(value):
        if isinstance(value, expr.InstanceOutputRef):
            return expr.InputRef(
                local_names.child_signal(value.instance, value.port),
                value.type, origin=value.origin,
            )
        if (
            isinstance(value, expr.FieldAccess)
            and isinstance(value.expression, expr.InputRef)
            and value.expression.name in instance_names
        ):
            return expr.InputRef(
                local_names.child_signal(value.expression.name, value.field), value.type,
                origin=value.origin,
            )
        if isinstance(value, tuple):
            return tuple(walk(item) for item in value)
        if is_dataclass(value):
            updates = {}
            for field in fields(value):
                if field.name == "origin" or not field.init:
                    continue
                updates[field.name] = walk(getattr(value, field.name))
            try:
                return replace(value, **updates)
            except (TypeError, ValueError):
                return value
        return value

    return walk(expression)


def _module_expression_roots(
    module: Module, names: ModuleRtlNames | None = None,
) -> tuple[expr.Expression, ...]:
    names = names or module_rtl_names(module)
    return _common_module_expression_roots(
        module,
        normalize=lambda value: _instance_expression(module, value, names),
    )


def _materialization_plan(
    module: Module, names: ModuleRtlNames | None = None,
) -> tuple[_MaterializedExpression, ...]:
    names = names or module_rtl_names(module)
    roots = _module_expression_roots(module, names)
    preferred = {
        _instance_expression(module, local.expression, names): _identifier(local.name)
        for local in module.locals
        if not local.compile_time
    }
    reserved_names = {
        _identifier(item.name) for item in (*module.ports, *module.registers, *module.locals)
    }
    reserved_names.update(item.physical_name for item in names.entries)
    return plan_materialization(
        roots,
        preferred_names=preferred,
        reserved_names=reserved_names,
    )


def _materialized_emission(module: Module):
    names = module_rtl_names(module)
    materialized = _materialization_plan(module, names)
    aliases = {item.expression: item.name for item in materialized}

    def render(value: expr.Expression, *, keep: expr.Expression | None = None) -> str:
        physical = _instance_expression(module, value, names)
        return _expression(_replace_materialized(physical, aliases, keep=keep))

    declarations = []
    assignments = []
    for item in materialized:
        signed = " signed" if isinstance(item.expression.type, (SIntType, FixedType)) else ""
        declarations.append(
            f"  logic{signed} {_range(_width(item.expression.type))}{item.name};"
        )
    for item in materialized:
        assignments.append(
            f"  assign {item.name} = {render(item.expression, keep=item.expression)};"
        )
    return declarations, assignments, render


def _composed_rendering(
    external_mapping_index: dict[str, ExternalPhysicalMapping] | None = None,
    *,
    formal_buffer_counts: tuple[FormalBufferCountProjection, ...] = (),
    hierarchy_cache: HierarchyTraversalCache | None = None,
) -> ComposedRendering:
    selected_hierarchy_cache = hierarchy_cache or HierarchyTraversalCache()
    return ComposedRendering(
        physical=SVPhysicalSyntax(
            error=SystemVerilogEmissionError,
            identifier=_identifier,
            instance_identifier=_instance_identifier,
            packed_width=_width,
            packed_range=_range,
            logic_declaration=_logic_declaration,
            physical_ports=_physical_port_declarations,
            validate_hierarchy=lambda module: _validated_hierarchy(
                module, cache=selected_hierarchy_cache
            ),
        ),
        services=ComposedLeafServices(
            materialized_emission=_materialized_emission,
            staging_emission=_embedded_staging_emission,
            rom_logic=_emit_rom_logic,
            append_unified_state=_append_unified_state,
            append_rule_state=lambda module, declarations, logic, render: (
                _append_rule_state(
                    module,
                    declarations,
                    logic,
                    render,
                    compact_reset=True,
                )
            ),
            emit_fifo=_emit_fifo,
            emit_memory=_emit_memory,
            emit_rom=_emit_rom_module,
            emit_unified_state=_emit_unified_state_module,
            emit_rules=_emit_rules,
            emit_elastic_pipeline=_emit_elastic_pipeline,
            emit_csr_child=_emit_composed_csr_child,
            named_module=_emit_composed_named_module,
            requires_unified_state=_requires_unified_state,
            ordered_rules=_ordered_rules,
            assignment_name=_assignment_name,
            emit_external=lambda module, name: _emit_external_wrapper(
                module,
                name,
                (external_mapping_index or {})[
                    module.external_contract.semantic_identity
                ],
            ),
        ),
        formal_buffer_counts=formal_buffer_counts,
    )


def _emit_composed_csr_child(module: Module) -> str:
    return _emit_csr(module, expose_internal_abi=True)


def _emit_composed_named_module(
    name: str,
    ports: list[str],
    lines: list[str],
    module: Module,
) -> str:
    return _named_module(name, ports, lines, typed_module=module)


def _rv_fifo_helper(
    name: str,
    width: int,
    depth: int,
    module: Module,
    *,
    expose_count: bool = False,
    allow_full_replace: bool = True,
) -> str:
    return _composed_rv_fifo_helper(
        name, width, depth, module, expose_count=expose_count,
        allow_full_replace=allow_full_replace,
    )
def _cdc_rendering() -> CDCRendering:
    return CDCRendering(
        SystemVerilogEmissionError,
        _identifier,
        _logic_port,
        _width,
        _range,
        _module,
    )


def _named_module(
    name: str,
    ports: list[str],
    lines: list[str],
    *,
    typed_module: Module | None = None,
) -> str:
    declarations = ",\n".join(f"  {port}" for port in ports)
    helpers = _function_definitions(typed_module) if typed_module is not None else []
    conditioner = (
        _reset_conditioner_lines(typed_module, _identifier)
        if typed_module is not None
        else ()
    )
    body = "\n".join(
        line for line in (*helpers, *conditioner, *lines) if line
    )
    return f"module {name} (\n{declarations}\n);\n  // Generated from backend-independent typed ZLang IR.\n{body}\nendmodule\n"


def _memory_byte_mask_concatenation(
    signal: str,
    *,
    element_width: int,
    lane_count: int,
) -> str:
    """Render an exact-width byte mask, including one partial MSB lane."""

    expected_lanes = (element_width + 7) // 8
    if lane_count != expected_lanes:
        raise SystemVerilogEmissionError(
            "memory write-mask width does not match its element width"
        )
    high_lane_width = element_width - (lane_count - 1) * 8
    return ", ".join(
        f"{{{high_lane_width if lane == lane_count - 1 else 8}"
        f"{{{signal}[{lane}]}}}}"
        for lane in reversed(range(lane_count))
    )


def _emit_memory(module: Module, *, ram_style: str | None = None) -> str:
    if len(module.memories) != 1 or module.clock is None or module.reset is None:
        raise SystemVerilogEmissionError(
            "direct SystemVerilog memory emission requires one memory and clock/reset"
        )
    memory = module.memories[0]
    if memory.read_latency not in (0, 1):
        raise SystemVerilogEmissionError(
            "direct SystemVerilog memory emission requires read_latency 0 or 1"
        )
    width = _width(memory.element_type)
    name = _identifier(memory.name)
    cells_name = rtl_memory_cells_identifier(memory.name)
    read_data_name = rtl_memory_read_data_identifier(memory.name)
    style = f'(* ram_style = "{ram_style}" *) ' if ram_style else ""
    declarations = [
        f"  {style}logic [{width - 1}:0] {cells_name} [0:{memory.depth - 1}];",
        f"  logic [{width - 1}:0] {read_data_name};",
    ]
    combinational: list[str] = []
    if memory.write_mask is not None:
        lanes = memory.write_mask_width
        assert lanes is not None
        expanded = _memory_byte_mask_concatenation(
            f"{name}_write_mask",
            element_width=width,
            lane_count=lanes,
        )
        declarations.extend((
            f"  logic [{lanes - 1}:0] {name}_write_mask;",
            f"  logic [{width - 1}:0] {name}_write_mask_expanded;",
            f"  logic [{width - 1}:0] {name}_write_merged;",
        ))
        combinational.extend((
            f"  assign {name}_write_mask = {_expression(memory.write_mask)};",
            f"  assign {name}_write_mask_expanded = {{{expanded}}};",
            f"  assign {name}_write_merged = "
            f"({name}_cells[{_expression(memory.write_address)}] & ~{name}_write_mask_expanded) | "
            f"({_expression(memory.write_data)} & {name}_write_mask_expanded);",
        ))
    declarations.append("  integer zlang_memory_reset_index;")
    if memory.read_latency == 0:
        read_value = f"{name}_cells[{_expression(memory.read_address)}]"
        if memory.collision is MemoryCollision.WRITE_FIRST:
            write_value = (
                name + "_write_merged"
                if memory.write_mask is not None
                else _expression(memory.write_data)
            )
            read_value = (
                f"({_reset_deasserted(module, _identifier)} && "
                f"{_expression(memory.write_enable)} && "
                f"({_expression(memory.read_address)} == "
                f"{_expression(memory.write_address)})) ? "
                f"{write_value} : ({read_value})"
            )
        if memory.read_data_reset is MemoryResetPolicy.CLEAR:
            read_value = (
                f"{_reset_asserted(module, _identifier)} ? '0 : ({read_value})"
            )
        combinational.append(f"  assign {name}_read_data = {read_value};")

    initialization: list[str] = []
    if (
        memory.contents_reset is MemoryResetPolicy.PRESERVE
        or (
            memory.read_latency == 1
            and memory.read_data_reset is MemoryResetPolicy.PRESERVE
        )
    ):
        initialization.append("  initial begin")
        if (
            memory.read_latency == 1
            and memory.read_data_reset is MemoryResetPolicy.PRESERVE
        ):
            initialization.append(f"    {name}_read_data = '0;")
        if memory.contents_reset is MemoryResetPolicy.PRESERVE:
            initialization.extend((
                f"    for (zlang_memory_reset_index = 0; "
                f"zlang_memory_reset_index < {memory.depth}; "
                "zlang_memory_reset_index = zlang_memory_reset_index + 1)",
                f"      {name}_cells[zlang_memory_reset_index] = '0;",
            ))
        initialization.append("  end")

    lines = [
        *declarations,
        *combinational,
        *initialization,
        f"  {'always' if initialization else 'always_ff'} "
        f"@({_clock_event(module, _identifier)}) begin",
        f"    if ({_reset_asserted(module, _identifier)}) begin",
    ]
    if memory.read_latency == 1 and memory.read_data_reset is MemoryResetPolicy.CLEAR:
        lines.append(f"      {name}_read_data <= '0;")
    if memory.contents_reset is MemoryResetPolicy.CLEAR:
        lines.append(
            f"      for (zlang_memory_reset_index = 0; "
            f"zlang_memory_reset_index < {memory.depth}; "
            "zlang_memory_reset_index = zlang_memory_reset_index + 1) "
        )
        lines.append(f"        {name}_cells[zlang_memory_reset_index] <= '0;")
    if (
        memory.contents_reset is MemoryResetPolicy.PRESERVE
        and (
            memory.read_latency == 0
            or memory.read_data_reset is MemoryResetPolicy.PRESERVE
        )
    ):
        lines.append("      // Memory contents and read result hold across reset.")
    lines.extend((
        "    end",
        "    else begin",
        f"      if ({_expression(memory.write_enable)}) {name}_cells[{_expression(memory.write_address)}] <= "
        f"{name + '_write_merged' if memory.write_mask is not None else _expression(memory.write_data)};",
    ))
    if memory.read_latency == 1 and memory.collision is MemoryCollision.WRITE_FIRST:
        lines.append(
            f"      if ({_expression(memory.write_enable)} && "
            f"({_expression(memory.read_address)} == {_expression(memory.write_address)})) "
            f"{name}_read_data <= "
            f"{name + '_write_merged' if memory.write_mask is not None else _expression(memory.write_data)};"
        )
        lines.append(
            f"      else {name}_read_data <= {name}_cells[{_expression(memory.read_address)}];"
        )
    elif memory.read_latency == 1:
        lines.append(
            f"      {name}_read_data <= {name}_cells[{_expression(memory.read_address)}];"
        )
    lines.extend(("    end", "  end"))
    for assignment in module.assignments:
        lines.append(
            f"  assign {_identifier(_assignment_name(assignment))} = {_expression(assignment.expression)};"
        )
    return _named_module(
        module.name,
        _physical_port_declarations(module),
        lines,
        typed_module=module,
    )


def _emit_rom_logic(module: Module, render) -> tuple[list[str], list[str]]:
    """Emit immutable ROM arrays and their single registered read boundary."""

    if module.roms and (module.clock is None or module.reset is None):
        raise SystemVerilogEmissionError(
            "initialized ROM emission requires one module clock and reset"
        )
    declarations: list[str] = []
    logic: list[str] = []
    for rom in module.roms:
        companion = companion_for_rom(rom)
        name = _identifier(rom.name)
        width = _width(rom.element_type)
        declarations.extend((
            f"  logic [{width - 1}:0] {name}_cells [0:{rom.depth - 1}];",
            f"  logic [{width - 1}:0] {name}_read_data;",
        ))
        logic.extend((
            "  initial begin",
            f'    $readmemb("{companion.logical_path}", {name}_cells);',
            "  end",
            f"  always_ff @({_clock_event(module, _identifier)}) begin",
            f"    if ({_reset_asserted(module, _identifier)}) {name}_read_data <= '0;",
            f"    else {name}_read_data <= {name}_cells[{render(rom.read_address)}];",
            "  end",
        ))
    return declarations, logic


def _emit_rom_module(module: Module) -> str:
    if module.memories or module.fifos:
        raise SystemVerilogEmissionError(
            "initialized ROM cannot share the legacy memory/FIFO emitter"
        )
    declarations, logic = _emit_rom_logic(module, _expression)
    for assignment in module.assignments:
        logic.append(
            f"  assign {_identifier(_assignment_name(assignment))} = "
            f"{_expression(assignment.expression)};"
        )
    return _named_module(
        module.name,
        _physical_port_declarations(module),
        declarations + logic,
        typed_module=module,
    )


def _referenced_fifo_signals(module: Module, fifo: object) -> set[FifoSignal]:
    """Return the exact optional observations emitted by the legacy FIFO path."""

    referenced = {
        value.signal
        for root in (
            fifo.data,
            fifo.push,
            fifo.pop,
            *(assignment.expression for assignment in module.assignments),
        )
        for value in _walk_expression(root)
        if isinstance(value, expr.FifoRef) and value.fifo == fifo.name
    }
    if FifoSignal.OVERFLOW in referenced:
        referenced.add(FifoSignal.FULL)
    if FifoSignal.UNDERFLOW in referenced:
        referenced.add(FifoSignal.EMPTY)
    return referenced


def _emit_fifo(module: Module) -> str:
    if len(module.fifos) != 1 or module.clock is None or module.reset is None:
        raise SystemVerilogEmissionError("direct FIFO emission requires one clock/reset FIFO")
    fifo = module.fifos[0]
    sources = tuple(p for p in module.inputs if p.protocol is InterfaceProtocol.READY_VALID)
    sinks = tuple(p for p in module.outputs if p.protocol is InterfaceProtocol.READY_VALID)
    scalar_wire = all(
        port.protocol is InterfaceProtocol.WIRE for port in module.ports
    )
    if not scalar_wire and (len(sources) != 1 or len(sinks) != 1):
        raise SystemVerilogEmissionError(
            "direct FIFO emission requires scalar wire ports or one "
            "ready/valid input and output"
        )
    width, count_width = _width(fifo.element_type), fifo.count_width
    ptr_width = max(1, (fifo.depth - 1).bit_length())
    if scalar_wire:
        ports = _physical_port_declarations(module)
    else:
        source, sink = sources[0], sinks[0]
        source_name = _identifier(source.name)
        sink_name = _identifier(sink.name)
        ports = [f"input logic {_identifier(module.clock)}",
                 f"input logic {_identifier(module.reset)}",
                 _logic_port("input", f"{source_name}_payload", source.type),
                 f"input logic {source_name}_valid", f"output logic {source_name}_ready",
                 _logic_port("output", f"{sink_name}_payload", sink.type),
                 f"output logic {sink_name}_valid", f"input logic {sink_name}_ready"]
    name = fifo.name
    if fifo.data is None or fifo.push is None or fifo.pop is None:
        raise SystemVerilogEmissionError(
            "legacy direct FIFO emission requires explicit data/push/pop controls"
        )
    referenced_fifo_signals = _referenced_fifo_signals(module, fifo)
    # The FIFO state is a typed storage resource, not an implicit ready/valid
    # pass-through.  Publish its semantic read-side signals once, then render
    # every source assignment from typed IR.  This matters when the output is
    # a projection, permutation, or other width-changing expression over
    # ``fifo.front`` rather than the stored element itself.
    observation_declarations: list[str] = []
    observation_assignments: list[str] = []
    if FifoSignal.FRONT in referenced_fifo_signals:
        observation_declarations.append(
            f"  logic {_range(width)}{name}_front;"
        )
        observation_assignments.append(
            f"  assign {name}_front = {name}_storage[{name}_rd];"
        )
    if FifoSignal.VALID in referenced_fifo_signals:
        observation_declarations.append(f"  logic {name}_valid;")
        observation_assignments.append(
            f"  assign {name}_valid = !({_reset_asserted(module, _identifier)}) "
            f"&& ({name}_count != '0);"
        )
    if FifoSignal.READY in referenced_fifo_signals:
        observation_declarations.append(f"  logic {name}_ready;")
        observation_assignments.append(
            f"  assign {name}_ready = !({_reset_asserted(module, _identifier)}) && "
            f"(({name}_count < {count_width}'d{fifo.depth}) || {name}_pop);"
        )
    if FifoSignal.FULL in referenced_fifo_signals:
        observation_declarations.append(f"  logic {name}_full;")
        observation_assignments.append(
            f"  assign {name}_full = "
            f"({name}_count == {count_width}'d{fifo.depth});"
        )
    if FifoSignal.EMPTY in referenced_fifo_signals:
        observation_declarations.append(f"  logic {name}_empty;")
        observation_assignments.append(
            f"  assign {name}_empty = ({name}_count == '0);"
        )
    request_declarations = [
        f"  logic {name}_push_request, {name}_pop_request;",
        f"  logic {name}_push, {name}_pop;",
    ]
    request_assignments = [
        f"  assign {name}_push_request = {_expression(fifo.push)};",
        f"  assign {name}_pop_request = {_expression(fifo.pop)};",
        f"  assign {name}_pop = !({_reset_asserted(module, _identifier)}) && "
        f"{name}_pop_request && ({name}_count != '0);",
        f"  assign {name}_push = !({_reset_asserted(module, _identifier)}) && "
        f"{name}_push_request && "
        f"(({name}_count < {count_width}'d{fifo.depth}) || {name}_pop);",
    ]
    if FifoSignal.OVERFLOW in referenced_fifo_signals:
        request_declarations.append(f"  logic {name}_overflow;")
        request_assignments.append(
            f"  assign {name}_overflow = !({_reset_asserted(module, _identifier)}) && "
            f"{name}_push_request && {name}_full && !{name}_pop;"
        )
    if FifoSignal.UNDERFLOW in referenced_fifo_signals:
        request_declarations.append(f"  logic {name}_underflow;")
        request_assignments.append(
            f"  assign {name}_underflow = !({_reset_asserted(module, _identifier)}) && "
            f"{name}_pop_request && {name}_empty;"
        )
    lines = [f"  logic {_range(width)}{name}_storage [0:{fifo.depth - 1}];",
             f"  logic [{count_width - 1}:0] {name}_count;",
             f"  logic [{ptr_width - 1}:0] {name}_rd, {name}_wr;",
             *observation_declarations,
             *request_declarations,
             *observation_assignments,
             *request_assignments,
             *(
                 f"  assign {_identifier(_assignment_name(assignment))} = "
                 f"{_expression(assignment.expression)};"
                 for assignment in module.assignments
             ),
             f"  always_ff @({_clock_event(module, _identifier)}) begin",
             f"    if ({_reset_asserted(module, _identifier)}) begin {name}_count <= '0; {name}_rd <= '0; {name}_wr <= '0; end",
             "    else begin",
             f"      if ({name}_push) begin {name}_storage[{name}_wr] <= {_expression(fifo.data)}; {name}_wr <= ({name}_wr == {ptr_width}'d{fifo.depth - 1}) ? '0 : {name}_wr + 1'b1; end",
             f"      if ({name}_pop) {name}_rd <= ({name}_rd == {ptr_width}'d{fifo.depth - 1}) ? '0 : {name}_rd + 1'b1;",
             f"      case ({{{name}_push, {name}_pop}})", f"        2'b10: {name}_count <= {name}_count + 1'b1;",
             f"        2'b01: {name}_count <= {name}_count - 1'b1;", f"        default: {name}_count <= {name}_count;",
             "      endcase", "    end", "  end"]
    return _module(module, ports, lines)


def _append_unified_state(
    module: Module,
    declarations: list[str],
    logic: list[str],
    render,
    *,
    excluded_assignment_names: frozenset[str] = frozenset(),
) -> None:
    """Append one frozen register/rule/storage transition to a component.

    This is deliberately shared by standalone and hierarchical modules.  A
    parent that owns scheduled state must not lose that state merely because it
    also instantiates a child component.
    """
    if module.clock is None or module.reset is None or module.resolved_transition is None:
        raise SystemVerilogEmissionError("unified state emission requires clock/reset transition IR")
    if any(not fifo.scheduled for fifo in module.fifos):
        raise SystemVerilogEmissionError("mixed legacy and scheduled FIFO resources are not implemented")
    transition = module.resolved_transition
    local_names = module_rtl_names(module)
    resources = {item.semantic_id: item for item in transition.resources}
    groups = ordered_state_groups(transition)
    group_by_name = {item.rule_name: item for item in groups}

    conditional = conditional_actions(transition)
    activation_predicates = conditional_activation_predicates(transition)
    activation_names = tuple(
        f"zlang_condition_{index}_active"
        for index in range(len(activation_predicates))
    )

    def action_enable(group, action) -> str:
        fire = local_names.rule(group.rule_name, "fire")
        if action.activation is None:
            return fire
        return (
            f"({fire} && "
            f"{activation_names[action_activation_predicate_index(transition, action)]})"
        )

    for register in module.registers:
        declarations.append(
            _logic_declaration(
                rtl_register_state_identifier(register.name), register.type
            )
        )
    for fifo in module.fifos:
        width = _width(fifo.element_type)
        ptr_width = max(1, (fifo.depth - 1).bit_length())
        name = _identifier(fifo.name)
        declarations.extend((
            f"  logic {_range(width)}{name}_storage [0:{fifo.depth - 1}];",
            f"  logic [{fifo.count_width - 1}:0] {name}_count;",
            f"  logic [{ptr_width - 1}:0] {name}_rd, {name}_wr;",
            f"  logic {name}_push, {name}_pop;",
            f"  logic {_range(width)}{name}_push_data;",
            f"  logic {_range(width)}{name}_front;",
            f"  logic {name}_empty, {name}_full;",
            f"  logic {name}_valid, {name}_ready;",
            f"  logic {name}_overflow, {name}_underflow;",
        ))
        logic.extend((
            f"  assign {name}_front = {name}_storage[{name}_rd];",
            f"  assign {name}_empty = ({name}_count == '0);",
            f"  assign {name}_full = ({name}_count == {fifo.count_width}'d{fifo.depth});",
            f"  assign {name}_valid = {_reset_deasserted(module, _identifier)} && !{name}_empty;",
            f"  assign {name}_ready = {_reset_deasserted(module, _identifier)} && "
            f"({name}_count < {fifo.count_width}'d{fifo.depth});",
            f"  assign {name}_overflow = 1'b0;",
            f"  assign {name}_underflow = 1'b0;",
        ))
    for memory in module.memories:
        if not memory.scheduled:
            raise SystemVerilogEmissionError(
                "mixed legacy and scheduled memory resources are not implemented"
            )
        if memory.read_latency != 1:
            raise SystemVerilogEmissionError(
                "scheduled memory emission requires read_latency 1"
            )
        width = _width(memory.element_type)
        address_width = memory.address_width
        name = _identifier(memory.name)
        cells_name = rtl_memory_cells_identifier(memory.name)
        read_data_name = rtl_memory_read_data_identifier(memory.name)
        declarations.extend((
            f"  logic {_range(width)}{cells_name} [0:{memory.depth - 1}];",
            f"  logic {_range(width)}{read_data_name};",
            f"  logic {name}_read_fire, {name}_write_fire;",
            f"  logic [{address_width - 1}:0] {name}_read_address, {name}_write_address;",
            f"  logic {_range(width)}{name}_write_data;",
            f"  integer {name}_reset_index;",
        ))
        if memory.write_mask_width is not None:
            declarations.extend((
                f"  logic [{memory.write_mask_width - 1}:0] {name}_write_mask;",
                f"  logic {_range(width)}{name}_write_mask_expanded;",
                f"  logic {_range(width)}{name}_write_merged;",
            ))

    for group in groups:
        declarations.extend((
            f"  logic {local_names.rule(group.rule_name, 'guard')};",
            f"  logic {local_names.rule(group.rule_name, 'fire')};",
        ))
        logic.append(
            f"  assign {local_names.rule(group.rule_name, 'guard')} = {render(group.guard)};"
        )
    for index, activation in enumerate(activation_predicates):
        name = activation_names[index]
        declarations.append(f"  logic {name};")
        logic.append(f"  assign {name} = {render(activation)};")

    fifo_names = [fifo.name for fifo in module.fifos]
    guard_names = [group.rule_name for group in groups]
    for group in groups:
        clauses: list[str] = []
        for region in selection_regions(transition, group.rule_name):
            count_values = region[:len(fifo_names)]
            guard_values = region[
                len(fifo_names):len(fifo_names) + len(guard_names)
            ]
            activation_values = region[
                len(fifo_names) + len(guard_names):
            ]
            terms: list[str] = []
            for name, fifo, value in zip(
                fifo_names, module.fifos, count_values, strict=True
            ):
                if value is FifoOccupancy.EMPTY:
                    terms.append(f"{_identifier(name)}_count == '0")
                elif value is FifoOccupancy.FULL:
                    terms.append(
                        f"{_identifier(name)}_count == "
                        f"{fifo.count_width}'d{fifo.depth}"
                    )
                elif value is FifoOccupancy.MIDDLE:
                    terms.append(
                        f"({_identifier(name)}_count > '0 && "
                        f"{_identifier(name)}_count < "
                        f"{fifo.count_width}'d{fifo.depth})"
                    )
            terms.extend(
                f"{local_names.rule(name, 'guard')} == 1'b{1 if value else 0}"
                for name, value in zip(
                    guard_names, guard_values, strict=True
                )
                if value is not None
            )
            terms.extend(
                f"{activation_names[index]} == 1'b{1 if value else 0}"
                for index, value in enumerate(
                    activation_values
                )
                if value is not None
            )
            clauses.append("(" + " && ".join(terms) + ")")
        condition = " || ".join(clauses) if clauses else "1'b0"
        logic.append(
            f"  assign {local_names.rule(group.rule_name, 'fire')} = "
            f"{_reset_deasserted(module, _identifier)} && ({condition});"
        )

    for fifo in module.fifos:
        resource_id = next(
            item.semantic_id for item in transition.resources
            if item.kind.value == "fifo" and item.name == fifo.name
        )
        push_actions = [
            (group, action)
            for group in groups
            for action in group.actions
            if action.resource_id == resource_id
            and action.kind is StateActionKind.FIFO_PUSH
        ]
        pop_actions = [
            (group, action)
            for group in groups
            for action in group.actions
            if action.resource_id == resource_id
            and action.kind is StateActionKind.FIFO_POP
        ]
        name = _identifier(fifo.name)
        push_terms = [action_enable(group, action) for group, action in push_actions]
        pop_terms = [action_enable(group, action) for group, action in pop_actions]
        logic.append(f"  assign {name}_push = " + (" || ".join(push_terms) if push_terms else "1'b0") + ";")
        logic.append(f"  assign {name}_pop = " + (" || ".join(pop_terms) if pop_terms else "1'b0") + ";")
        data = f"{_width(fifo.element_type)}'d0"
        for group, action in reversed(push_actions):
            data = (
                f"{action_enable(group, action)} ? "
                f"{render(action.operands[0])} : ({data})"
            )
        logic.append(f"  assign {name}_push_data = {data};")

    for memory in module.memories:
        name = _identifier(memory.name)
        resource_id = memory.semantic_id
        read_actions = [
            (group, action)
            for group in groups
            for action in group.actions
            if action.resource_id == resource_id
            and action.kind is StateActionKind.MEMORY_READ_REQUEST
        ]
        write_actions = [
            (group, action)
            for group in groups
            for action in group.actions
            if action.resource_id == resource_id
            and action.kind is StateActionKind.MEMORY_WRITE
        ]
        read_terms = [action_enable(group, action) for group, action in read_actions]
        write_terms = [action_enable(group, action) for group, action in write_actions]
        logic.append(
            f"  assign {name}_read_fire = "
            + (" || ".join(read_terms) if read_terms else "1'b0") + ";"
        )
        logic.append(
            f"  assign {name}_write_fire = "
            + (" || ".join(write_terms) if write_terms else "1'b0") + ";"
        )
        read_address = f"{memory.address_width}'d0"
        for group, action in reversed(read_actions):
            read_address = (
                f"{action_enable(group, action)} ? "
                f"{render(action.operands[0])} : ({read_address})"
            )
        write_address = f"{memory.address_width}'d0"
        write_data = f"{_width(memory.element_type)}'d0"
        write_mask = (
            f"{memory.write_mask_width}'d0"
            if memory.write_mask_width is not None else None
        )
        for group, action in reversed(write_actions):
            fire = action_enable(group, action)
            write_address = f"{fire} ? {render(action.operands[0])} : ({write_address})"
            write_data = f"{fire} ? {render(action.operands[1])} : ({write_data})"
            if write_mask is not None:
                write_mask = f"{fire} ? {render(action.operands[2])} : ({write_mask})"
        logic.append(f"  assign {name}_read_address = {read_address};")
        logic.append(f"  assign {name}_write_address = {write_address};")
        logic.append(f"  assign {name}_write_data = {write_data};")
        if write_mask is not None:
            logic.append(f"  assign {name}_write_mask = {write_mask};")
            expanded = _memory_byte_mask_concatenation(
                f"{name}_write_mask",
                element_width=_width(memory.element_type),
                lane_count=memory.write_mask_width,
            )
            logic.append(
                f"  assign {name}_write_mask_expanded = {{{expanded}}};"
            )
            logic.append(
                f"  assign {name}_write_merged = "
                f"({name}_cells[{name}_write_address] & ~{name}_write_mask_expanded) | "
                f"({name}_write_data & {name}_write_mask_expanded);"
            )

    scheduled_memory_initialization = False
    for memory in module.memories:
        initialize_contents = (
            memory.contents_reset is MemoryResetPolicy.PRESERVE
        )
        initialize_read_data = (
            memory.read_data_reset is MemoryResetPolicy.PRESERVE
        )
        if not (initialize_contents or initialize_read_data):
            continue
        scheduled_memory_initialization = True
        name = _identifier(memory.name)
        logic.append("  initial begin")
        if initialize_read_data:
            logic.append(f"    {name}_read_data = '0;")
        if initialize_contents:
            logic.extend((
                f"    for ({name}_reset_index = 0; {name}_reset_index < "
                f"{memory.depth}; {name}_reset_index = {name}_reset_index + 1)",
                f"      {name}_cells[{name}_reset_index] = '0;",
            ))
        logic.append("  end")

    logic.append(
        f"  {'always' if scheduled_memory_initialization else 'always_ff'} "
        f"@({_clock_event(module, _identifier)}) begin"
    )
    logic.append(f"    if ({_reset_asserted(module, _identifier)}) begin")
    for register in module.registers:
        logic.append(f"      {_identifier(register.name)} <= {render(register.initial)};")
    for fifo in module.fifos:
        name = _identifier(fifo.name)
        logic.append(f"      {name}_count <= '0; {name}_rd <= '0; {name}_wr <= '0;")
    for memory in module.memories:
        name = _identifier(memory.name)
        if memory.read_data_reset is MemoryResetPolicy.CLEAR:
            logic.append(f"      {name}_read_data <= '0;")
        if memory.contents_reset is MemoryResetPolicy.CLEAR:
            logic.append(
                f"      for ({name}_reset_index = 0; {name}_reset_index < "
                f"{memory.depth}; {name}_reset_index = {name}_reset_index + 1) "
                f"{name}_cells[{name}_reset_index] <= '0;"
            )
        if (
            memory.read_data_reset is MemoryResetPolicy.PRESERVE
            and memory.contents_reset is MemoryResetPolicy.PRESERVE
        ):
            logic.append(
                f"      // {name} contents and read result hold across reset."
            )
    logic.append("    end else begin")
    for register in module.registers:
        writers = []
        resource_id = next(
            item.semantic_id for item in transition.resources
            if item.kind.value == "register" and item.name == register.name
        )
        for group in groups:
            for action in group.actions:
                if (
                    action.resource_id != resource_id
                    or action.kind is not StateActionKind.REGISTER_WRITE
                ):
                    continue
                writers.append((group, action))
        for index, (group, action) in enumerate(writers):
            keyword = "if" if index == 0 else "else if"
            logic.append(
                f"      {keyword} ({action_enable(group, action)}) "
                f"{_identifier(register.name)} <= {render(action.operands[0])};"
            )
        default = next((item for item in module.next_assignments if item.target.name == register.name), None)
        if default is not None:
            logic.append(
                f"      {'else ' if writers else ''}{_identifier(register.name)} <= {render(default.expression)};"
            )
    for fifo in module.fifos:
        name = _identifier(fifo.name)
        ptr_width = max(1, (fifo.depth - 1).bit_length())
        logic.extend((
            f"      if ({name}_push) begin {name}_storage[{name}_wr] <= {name}_push_data; "
            f"{name}_wr <= ({name}_wr == {ptr_width}'d{fifo.depth - 1}) ? '0 : {name}_wr + 1'b1; end",
            f"      if ({name}_pop) {name}_rd <= ({name}_rd == {ptr_width}'d{fifo.depth - 1}) ? '0 : {name}_rd + 1'b1;",
            f"      case ({{{name}_push, {name}_pop}})",
            f"        2'b10: {name}_count <= {name}_count + 1'b1;",
            f"        2'b01: {name}_count <= {name}_count - 1'b1;",
            f"        default: {name}_count <= {name}_count;",
            "      endcase",
        ))
    for memory in module.memories:
        name = _identifier(memory.name)
        logic.append(
            f"      if ({name}_write_fire) "
            f"{name}_cells[{name}_write_address] <= "
            f"{name + '_write_merged' if memory.write_mask_width is not None else name + '_write_data'};"
        )
        if memory.collision is MemoryCollision.WRITE_FIRST:
            logic.extend((
                f"      if ({name}_read_fire) begin",
                f"        if ({name}_write_fire && ({name}_read_address == {name}_write_address))",
                f"          {name}_read_data <= "
                f"{name + '_write_merged' if memory.write_mask_width is not None else name + '_write_data'};",
                f"        else {name}_read_data <= {name}_cells[{name}_read_address];",
                "      end",
            ))
        else:
            logic.append(
                f"      if ({name}_read_fire) "
                f"{name}_read_data <= {name}_cells[{name}_read_address];"
            )
    logic.extend(("    end", "  end"))
    # Scalar rule outputs share the already-resolved rule-fire schedule with
    # register writes.  A direct assignment, when present, is the ordinary
    # combinational fallback; otherwise the reset/idle value is the exact
    # zero of the declared output type.  Protocol-member assignments retain
    # their existing independent lowering below.
    scalar_assignments = {
        assignment.target.name: assignment
        for assignment in module.assignments
        if (
            isinstance(assignment.target, Port)
            and assignment.target.protocol is InterfaceProtocol.WIRE
        )
    }
    emitted_scalar_outputs: set[str] = set()
    for output in module.outputs:
        if output.protocol is not InterfaceProtocol.WIRE:
            continue
        resource = next(
            (
                item for item in transition.resources
                if item.kind.value == "output" and item.name == output.name
            ),
            None,
        )
        writers = (
            [
                (group, action)
                for group in groups
                for action in group.actions
                if action.resource_id == resource.semantic_id
                and action.kind is StateActionKind.OUTPUT_WRITE
            ]
            if resource is not None else []
        )
        assignment = scalar_assignments.get(output.name)
        if assignment is None and not writers:
            continue
        value = (
            render(assignment.expression)
            if assignment is not None
            else f"{_width(output.type)}'d0"
        )
        for group, action in reversed(writers):
            value = (
                f"{action_enable(group, action)} ? "
                f"{render(action.operands[0])} : ({value})"
            )
        logic.append(f"  assign {_identifier(output.name)} = {value};")
        emitted_scalar_outputs.add(output.name)
    for assignment in module.assignments:
        name = _assignment_name(assignment)
        if name in emitted_scalar_outputs or name in excluded_assignment_names:
            continue
        logic.append(
            f"  assign {_identifier(name)} = {render(assignment.expression)};"
        )


def _emit_unified_state_module(module: Module) -> str:
    """Emit the frozen register/rule/FIFO transition as one clocked component."""
    ports = _physical_port_declarations(module)
    declarations: list[str] = []
    logic: list[str] = []
    # Unified state used to bypass the backend-wide materialization policy and
    # render transition expressions directly.  Keep one typed renderer for
    # guards, state actions, defaults, and outputs so aggregate runtime reads
    # and expensive fixed-point conversions are named once and then reused.
    stage_declarations, stage_logic, stage_render = (
        _embedded_staging_emission(module)
    )
    if stage_declarations:
        # Delay/Pipeline roots may live in an action activation rather than a
        # conventional assignment.  The shared root traversal includes those
        # predicates; retain their physical stage before scheduling effects.
        declarations.extend(stage_declarations)
        logic.extend(stage_logic)
        render = stage_render
    else:
        materialized_declarations, materialized_assignments, render = (
            _materialized_emission(module)
        )
        declarations.extend(materialized_declarations)
        logic.extend(materialized_assignments)
    rom_declarations, rom_logic = _emit_rom_logic(module, render)
    declarations.extend(rom_declarations)
    logic.extend(rom_logic)
    _append_unified_state(module, declarations, logic, render)
    return _module(module, ports, declarations + logic)


def emit_artifact(module: Module, *, selected_ir_identity: str | None = None,
                  recursive_design: object | None = None,
                  external_mappings: tuple[ExternalPhysicalMapping, ...] = ()) -> BackendArtifact:
    """Emit direct SV and publish its explicit artifact-bound manifest."""
    recursive_hierarchy = (
        _validate_recursive_hierarchy(module, recursive_design)
        if recursive_design is not None else None
    )
    text = emit(module, external_mappings=external_mappings)
    identity = selected_ir_identity or default_selected_ir_identity(module)
    names = _top_physical_rtl_names(module)
    if recursive_design is not None:
        assert recursive_hierarchy is not None
        hierarchy, _ = recursive_hierarchy
        # Reserve the exact helper spellings used by emission. Semantic call
        # names carry provenance, while physical helper names intentionally do
        # not; a collision must resolve identically in RTL and its locators.
        naming_hierarchy = _validated_hierarchy(_physicalize_generic_callables(module))
        component_names = build_component_name_plan(naming_hierarchy)
        local_plans = {
            entry.physical_path: module_rtl_names(entry.module)
            for entry in naming_hierarchy.entries
        }
        # The public leaf wrapper owns no packed signals or architectural
        # state.  Production locators use the same state root as VPI, while
        # retaining paths relative to the artifact's public top.  Formal-only
        # observation ports have their own top-level publication route.
        state_root = physical_state_root_path(naming_hierarchy.root.module)
        core_path = state_root[2:]
        root_rtl_module = (
            _public_core_module_name(naming_hierarchy.root.module)
            if core_path else _identifier(module.name)
        )
        digest = hashlib.sha256(text.encode()).hexdigest()
        bound = []
        for item in recursive_design.bindings:
            hierarchy_entry = hierarchy.at(tuple(item.physical_instance_path))
            observed_module = naming_hierarchy.at(tuple(item.physical_instance_path)).module
            token = _recursive_signal_token(
                observed_module, item.ref.local_semantic_id
            )
            locator = None
            if token is not None:
                rtl_module = (
                    root_rtl_module
                    if item.ref.instance_identity == recursive_design.root_instance_identity
                    else _component_name(
                        hierarchy_entry.module,
                        hierarchy_entry.specialization_identity,
                        naming_plan=component_names,
                    )
                )
                locator = BackendPhysicalLocator(
                    "direct_systemverilog", digest, rtl_module,
                    core_path + rtl_hierarchy_instance_path(
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


def _top_physical_rtl_names(module: Module) -> dict[str, str]:
    """Return the one authoritative direct-SV public-name projection.

    Both production and formal-only artifacts describe the same public
    ``TopPhysicalABI``.  Derive every physical token through the emitter's
    existing identifier policy so a formal harness can never reconstruct an
    unmangled ZLang spelling or maintain a second reserved-word list.
    """

    names = {
        f"port:{port.name}": _identifier(port.name) for port in module.ports
    }
    for port in module.ports:
        base = _identifier(port.name)
        if port.protocol is InterfaceProtocol.READY_VALID:
            names.update({f"port:{port.name}.payload": f"{base}_payload",
                          f"port:{port.name}.valid": f"{base}_valid",
                          f"port:{port.name}.ready": f"{base}_ready"})
        elif port.protocol is InterfaceProtocol.CREDIT:
            names.update({f"port:{port.name}.payload": f"{base}_payload",
                          f"port:{port.name}.send": f"{base}_send",
                          f"port:{port.name}.return": f"{base}_return"})
        elif port.protocol is InterfaceProtocol.VC_CREDIT:
            names.update({
                f"port:{port.name}.payload": f"{base}_payload",
                f"port:{port.name}.vc": f"{base}_vc",
                f"port:{port.name}.send": f"{base}_send",
                f"port:{port.name}.return": f"{base}_return",
                f"port:{port.name}.return_vc": f"{base}_return_vc",
            })
    # The complete public TopPhysicalABI is authoritative.  Legacy protocol
    # spellings above describe the private packed core and may differ when a
    # reserved root such as ``input`` is flattened into a public leaf such as
    # ``input_payload``.
    names.update({
        leaf.leaf_semantic_id: _identifier(leaf.external_name)
        for leaf in module.top_physical_abi.leaves
    })
    if module.clock:
        names["clock"] = _identifier(module.clock)
    if module.reset:
        names["reset"] = _identifier(module.reset)
    return names


def emit_artifact_with_source_map(
    module: Module,
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


def _formal_buffer_count_projection(
    module: Module,
    semantic_id: str,
) -> FormalBufferCountProjection | None:
    """Resolve one typed buffered RR occupancy to its formal-only FIFO ABI."""

    for descriptor in module.request_response_connections:
        for channel, signal, edge, depth in (
            (
                RequestResponseChannel.REQUEST,
                RequestResponseObservationSignal.REQUEST_OCCUPANCY,
                descriptor.request,
                descriptor.request.request_buffer_depth,
            ),
            (
                RequestResponseChannel.RESPONSE,
                RequestResponseObservationSignal.RESPONSE_OCCUPANCY,
                descriptor.response,
                descriptor.response.response_buffer_depth,
            ),
        ):
            if depth <= 0 or semantic_id != request_response_observation_id(
                descriptor.semantic_id, signal
            ):
                continue
            # Validate the semantic channel relation before allocating a
            # physical signal.  The renderer consumes this exact descriptor;
            # no generated instance/token spelling participates in lookup.
            if edge.source.channel is not channel:
                raise SystemVerilogEmissionError(
                    "request/response directional buffer channel metadata is "
                    "inconsistent"
                )
            token = hashlib.sha256(
                repr((descriptor.semantic_id, channel.value)).encode()
            ).hexdigest()[:16]
            return FormalBufferCountProjection(
                descriptor.semantic_id,
                channel,
                f"zlang_formal_rr_buffer_count_{token}",
                max(1, depth.bit_length()),
                depth,
            )
    return None


def _formal_adapter_count_projection(
    module: Module,
    semantic_id: str,
) -> FormalAdapterCountProjection | None:
    """Resolve receiver-credit occupancy to its typed adapter FIFO count.

    This is deliberately limited to the existing closed
    ``credit_to_rv`` adapter.  A receiver credit endpoint by itself does not
    imply implementation state, so no observation is fabricated unless the
    exact typed connection owns the FIFO that implements that occupancy.
    """

    for connection in module.connections:
        if connection.adapter is not ConnectionAdapter.CREDIT_TO_READY_VALID:
            continue
        source = connection.source
        destination = connection.destination
        observation_id = port_observation_id(source.name, "occupancy")
        if semantic_id != observation_id:
            continue
        if (
            source.protocol is not InterfaceProtocol.CREDIT
            or source.direction is not PortDirection.INPUT
            or destination.protocol is not InterfaceProtocol.READY_VALID
            or destination.direction is not PortDirection.OUTPUT
            or source.capacity is None
            or connection.buffer_depth != source.capacity
            or source.type != destination.type
            or source.domain != destination.domain
        ):
            raise SystemVerilogEmissionError(
                "receiver-credit formal occupancy requires one exact typed "
                "credit_to_rv adapter FIFO"
            )
        depth = connection.buffer_depth
        token = hashlib.sha256(
            repr((
                observation_id,
                source.name,
                destination.name,
                connection.adapter.value,
                depth,
                str(source.type),
                source.domain,
            )).encode()
        ).hexdigest()[:16]
        return FormalAdapterCountProjection(
            observation_id,
            source.name,
            destination.name,
            connection.adapter,
            f"zlang_formal_adapter_count_{token}",
            max(1, depth.bit_length()),
            depth,
        )
    return None


def _formal_rule_fire_reset(
    module: Module, accepted: expr.Expression,
) -> expr.Expression:
    """Gate the accepted schedule with the reset consumed by emitted state.

    ``module`` is the final backend-local component: synchronized-release roots
    consume their conditioner, while closed children consume the already
    conditioned native-release reset supplied through their component ABI.
    """

    if module.reset is None:
        return accepted
    domain = _module_domain(module)
    bit = BitType()
    origin = getattr(accepted, "origin", None)
    deasserted = expr.Binary(
        expr.BinaryOperator.EQUAL,
        expr.InputRef(_effective_reset_signal(module, _identifier), bit, origin=origin),
        expr.Constant(
            0 if domain.reset_polarity is ResetPolarity.ACTIVE_HIGH else 1,
            bit, origin=origin,
        ),
        bit, bit, origin=origin,
    )
    return expr.Binary(
        expr.BinaryOperator.BIT_AND, deasserted, accepted,
        bit, bit, origin=origin,
    )


def _formal_local_expression(
    module: Module, semantic_id: str, *, defer_rule_reset: bool = False,
) -> expr.Expression | None:
    """Return the typed expression for one already-frozen M35 observation."""

    if module.resolved_transition is not None:
        group = next((
            item for item in module.resolved_transition.action_groups
            if rule_fire_observation_id(item.rule_name) == semantic_id
        ), None)
        if group is not None:
            # The legacy direct rule emitter represents accepted firing as an
            # ``if/else if`` control relation rather than a named RTL wire.
            # Project the exact minimized selection regions computed by the
            # authoritative ResolvedTransition scheduler; never substitute a
            # raw guard or infer the relation from emitted identifiers.
            bit = BitType()

            def binary(
                operator: expr.BinaryOperator,
                left: expr.Expression,
                right: expr.Expression,
                operand_type: HardwareType,
            ) -> expr.Binary:
                return expr.Binary(
                    operator, left, right, operand_type, bit,
                    origin=group.source_origin,
                )

            def conjunction(items: list[expr.Expression]) -> expr.Expression:
                result = items[0] if items else expr.Constant(1, bit)
                for item in items[1:]:
                    result = binary(expr.BinaryOperator.BIT_AND, result, item, bit)
                return result

            def disjunction(items: list[expr.Expression]) -> expr.Expression:
                result = items[0] if items else expr.Constant(0, bit)
                for item in items[1:]:
                    result = binary(expr.BinaryOperator.BIT_OR, result, item, bit)
                return result

            fifos = tuple(
                item for item in module.resolved_transition.resources
                if item.kind.value == "fifo"
            )
            groups = ordered_state_groups(module.resolved_transition)
            activation_predicates = conditional_activation_predicates(
                module.resolved_transition
            )
            fifo_by_name = {item.name: item for item in module.fifos}
            regions: list[expr.Expression] = []
            for region in selection_regions(
                module.resolved_transition, group.rule_name
            ):
                terms: list[expr.Expression] = []
                for resource, occupancy in zip(
                    fifos, region[:len(fifos)], strict=True
                ):
                    if occupancy is None:
                        continue
                    fifo = fifo_by_name[resource.name]
                    count_type = UIntType(fifo.count_width)
                    count = expr.FifoRef(
                        fifo.name, FifoSignal.COUNT, count_type,
                        origin=group.source_origin,
                    )
                    zero = expr.Constant(0, count_type)
                    depth = expr.Constant(fifo.depth, count_type)
                    if occupancy is FifoOccupancy.EMPTY:
                        terms.append(binary(
                            expr.BinaryOperator.EQUAL, count, zero, count_type
                        ))
                    elif occupancy is FifoOccupancy.FULL:
                        terms.append(binary(
                            expr.BinaryOperator.EQUAL, count, depth, count_type
                        ))
                    else:
                        terms.extend((
                            binary(expr.BinaryOperator.GREATER, count, zero, count_type),
                            binary(expr.BinaryOperator.LESS, count, depth, count_type),
                        ))
                guard_values = region[
                    len(fifos):len(fifos) + len(groups)
                ]
                activation_values = region[len(fifos) + len(groups):]
                for candidate, enabled in zip(
                    groups, guard_values, strict=True
                ):
                    if enabled is None:
                        continue
                    terms.append(
                        candidate.guard
                        if enabled else binary(
                            expr.BinaryOperator.EQUAL,
                            candidate.guard,
                            expr.Constant(0, bit),
                            bit,
                        )
                    )
                for activation, enabled in zip(
                    activation_predicates, activation_values, strict=True
                ):
                    if enabled is None:
                        continue
                    terms.append(
                        activation
                        if enabled else binary(
                            expr.BinaryOperator.EQUAL,
                            activation,
                            expr.Constant(0, bit),
                            bit,
                        )
                    )
                regions.append(conjunction(terms))
            accepted = disjunction(regions)
            return (
                accepted if defer_rule_reset
                else _formal_rule_fire_reset(module, accepted)
            )
    if semantic_id.startswith("register:"):
        name = semantic_id.split(":", 1)[1]
        register = next((item for item in module.registers if item.name == name), None)
        return None if register is None else expr.RegisterRef(name, register.type)
    if semantic_id.startswith("fifo:"):
        value = semantic_id.split(":", 1)[1]
        if "." not in value:
            return None
        name, signal_text = value.split(".", 1)
        fifo = next((item for item in module.fifos if item.name == name), None)
        if fifo is None:
            return None
        try:
            signal = FifoSignal(signal_text)
        except ValueError:
            return None
        signal_type: HardwareType
        if signal is FifoSignal.COUNT:
            signal_type = UIntType(fifo.count_width)
        elif signal in {FifoSignal.PUSH, FifoSignal.POP, FifoSignal.EMPTY,
                        FifoSignal.FULL, FifoSignal.VALID, FifoSignal.READY}:
            signal_type = BitType()
        elif signal in {FifoSignal.FRONT, FifoSignal.DATA}:
            signal_type = fifo.element_type
        else:
            return None
        return expr.FifoRef(name, signal, signal_type)
    if semantic_id.startswith("port:"):
        value = semantic_id.split(":", 1)[1]
        name, separator, signal_text = value.partition(".")
        port = next((item for item in module.ports if item.name == name), None)
        if port is None:
            return None
        if not separator:
            # A protocol endpoint is an ownership/aggregate identity, not one
            # physical packed RTL signal.  Only ordinary wire ports have a
            # base-value observation; protocol leaves are projected below.
            return (
                expr.InputRef(name, port.type)
                if port.protocol is InterfaceProtocol.WIRE
                else None
            )
        if port.protocol is InterfaceProtocol.READY_VALID:
            try:
                signal = ReadyValidSignal(signal_text)
            except ValueError:
                return None
            signal_type = port.type if signal is ReadyValidSignal.PAYLOAD else BitType()
            return expr.ReadyValidRef(name, signal, signal_type)
        if port.protocol is InterfaceProtocol.CREDIT:
            if signal_text == "occupancy":
                projection = _formal_adapter_count_projection(module, semantic_id)
                if projection is None:
                    return None
                return expr.InputRef(
                    projection.signal,
                    UIntType(projection.width),
                )
            try:
                signal = CreditSignal(signal_text)
            except ValueError:
                return None
            if signal is CreditSignal.PAYLOAD:
                signal_type = port.type
            elif signal is CreditSignal.CREDITS:
                signal_type = UIntType(max(1, (port.capacity or 1).bit_length()))
            else:
                signal_type = BitType()
            return expr.CreditRef(name, signal, signal_type)
        return None
    if semantic_id.startswith("csr-field:"):
        token = _csr_observation_token(module, semantic_id)
        if token is None:
            return None
        port = next((item for item in module.ports if item.name == token), None)
        return None if port is None else expr.InputRef(port.name, port.type)
    if semantic_id.startswith("rr:"):
        count_projection = _formal_buffer_count_projection(module, semantic_id)
        if count_projection is not None:
            descriptor = next(
                item for item in module.request_response_connections
                if item.semantic_id
                == count_projection.request_response_semantic_id
            )
            return expr.InputRef(
                count_projection.signal,
                UIntType(count_projection.width),
                origin=descriptor.source_origin,
            )
        for descriptor in module.request_response_connections:
            tracker = _request_response_tracker_name(descriptor, module)
            width = max(1, descriptor.max_outstanding.bit_length())
            observations = {
                request_response_observation_id(
                    descriptor.semantic_id,
                    RequestResponseObservationSignal.OUTSTANDING,
                ): expr.InputRef(tracker, UIntType(width)),
                request_response_observation_id(
                    descriptor.semantic_id,
                    RequestResponseObservationSignal.REQUEST_ACCEPT,
                ): expr.InputRef(f"{tracker}_request_transfer", BitType()),
                request_response_observation_id(
                    descriptor.semantic_id,
                    RequestResponseObservationSignal.RESPONSE_CONSUME,
                ): expr.InputRef(f"{tracker}_response_transfer", BitType()),
            }
            # An absent directional buffer has exactly zero occupancy by the
            # typed request/response connection contract.  Publish that fact as
            # a formal-only constant projection.  Buffered occupancy remains
            # unavailable until the FIFO component exposes its count through an
            # explicit typed formal ABI; never recover it from generated names.
            if not descriptor.request.request_buffer_depth:
                observations[request_response_observation_id(
                    descriptor.semantic_id,
                    RequestResponseObservationSignal.REQUEST_OCCUPANCY,
                )] = expr.Constant(
                    0, UIntType(width), origin=descriptor.source_origin
                )
            if not descriptor.response.response_buffer_depth:
                observations[request_response_observation_id(
                    descriptor.semantic_id,
                    RequestResponseObservationSignal.RESPONSE_OCCUPANCY,
                )] = expr.Constant(
                    0, UIntType(width), origin=descriptor.source_origin
                )
            if semantic_id in observations:
                return observations[semantic_id]
    # Directional-buffer occupancy remains explicitly unavailable until its
    # emitting IR entity exposes a typed value. Never recover observations by
    # parsing generated RTL identifiers.
    return None


def _formal_projection_name(relative_path: tuple[str, ...], semantic_id: str) -> str:
    payload = repr((relative_path, semantic_id)).encode()
    return "zlang_formal_local_" + hashlib.sha256(payload).hexdigest()[:16]


def _instrument_direct_formal_module(
    module: Module,
    recursive_design: object,
    top_tokens: dict[str, str],
) -> tuple[
    Module,
    set[str],
    tuple[FormalBufferCountProjection, ...],
    tuple[FormalAdapterCountProjection, ...],
]:
    """Project typed observations through explicit component output ports."""

    by_path: dict[tuple[str, ...], list[object]] = {}
    for item in recursive_design.bindings:
        by_path.setdefault(tuple(item.physical_instance_path), []).append(item)
    available: set[str] = set()
    formal_buffer_counts: set[FormalBufferCountProjection] = set()
    formal_adapter_counts: set[FormalAdapterCountProjection] = set()
    namespace_hierarchy = _validated_hierarchy(_physicalize_generic_callables(module))
    namespace_plans = {
        entry.physical_path: module_rtl_names(entry.module)
        for entry in namespace_hierarchy.entries
    }

    def walk(current: Module, path: tuple[str, ...], *, top: bool) -> tuple[Module, dict[str, str]]:
        transformed_children: list[Module] = []
        child_outputs: dict[str, dict[str, str]] = {}
        for child_module, elaborated in zip(
            current.children, current.elaborated_instances, strict=True
        ):
            array = elaborated.instance.array_length or 1
            if array != 1:
                transformed_children.append(child_module)
                continue
            child_path = (*path, elaborated.instance.name)
            child, outputs = walk(child_module, child_path, top=False)
            transformed_children.append(child)
            child_outputs[elaborated.instance.name] = outputs
        if not current.elaborated_instances:
            transformed_children = list(current.children)

        ports = list(current.ports)
        assignments = list(current.assignments)
        output_map: dict[str, str] = {}
        namespace = namespace_plans[path]
        used_names = set(namespace.reserved) | {
            item.physical_name for item in namespace.entries
        }
        deferred_rule_outputs: set[str] = set()
        deferred_rr_outputs: dict[str, str] = {}
        local_rule_ids = {
            rule_fire_observation_id(group.rule_name)
            for group in (
                current.resolved_transition.action_groups
                if current.resolved_transition is not None else ()
            )
        }

        def publish(binding: object, expression: expr.Expression,
                    relative: tuple[str, ...]) -> None:
            semantic_binding_id = binding.semantic_binding_id
            preferred_name = (
                top_tokens[semantic_binding_id]
                if top else _formal_projection_name(relative, binding.ref.local_semantic_id)
            )
            name = output_map.get(semantic_binding_id)
            if name is None:
                name = allocate_private_rtl_identifier(
                    preferred_name,
                    semantic_identity=f"formal-observation:{relative}:{binding.ref.local_semantic_id}",
                    used=used_names,
                )
                if top:
                    top_tokens[semantic_binding_id] = name
                # Formal observations have one stable packed physical token,
                # even when the observed semantic value is an aggregate.  The
                # production top ABI still exposes aggregate leaves; only this
                # formal-only projection is representation-bit packed.  Using
                # the semantic aggregate type here would make the normal top
                # ABI flattener publish ``token_field`` ports while the formal
                # manifest and harness correctly referred to ``token``.
                physical_type = _hardware_type_for_width(
                    binding.width, binding.signedness
                )
                physical_expression = expression
                if expression.type != physical_type:
                    physical_expression = expr.Bitcast(
                        expression, physical_type,
                        origin=getattr(expression, "origin", None),
                    )
                output = Port(
                    PortDirection.OUTPUT, name, physical_type,
                    domain=current.clock,
                )
                ports.append(output)
                assignments.append(Assignment(output, physical_expression))
            output_map[semantic_binding_id] = name
            available.add(semantic_binding_id)

        for binding in sorted(
            by_path.get(path, ()), key=lambda item: item.semantic_binding_id
        ):
            expression = _formal_local_expression(
                current, binding.ref.local_semantic_id, defer_rule_reset=True,
            )
            if expression is not None:
                projection = _formal_buffer_count_projection(
                    current, binding.ref.local_semantic_id
                )
                if projection is not None:
                    formal_buffer_counts.add(projection)
                adapter_projection = _formal_adapter_count_projection(
                    current, binding.ref.local_semantic_id
                )
                if adapter_projection is not None:
                    formal_adapter_counts.add(adapter_projection)
                publish(binding, expression, ())
                if binding.ref.local_semantic_id in local_rule_ids:
                    deferred_rule_outputs.add(output_map[binding.semantic_binding_id])
                if binding.ref.local_semantic_id.startswith("rr:"):
                    deferred_rr_outputs[output_map[binding.semantic_binding_id]] = (
                        binding.ref.local_semantic_id
                    )

        bindings_by_id = {
            item.semantic_binding_id: item
            for items in by_path.values() for item in items
        }
        for child_name, outputs in sorted(child_outputs.items()):
            for semantic_binding_id, child_port in sorted(outputs.items()):
                binding = bindings_by_id[semantic_binding_id]
                expression = expr.InstanceOutputRef(
                    child_name, child_port,
                    _hardware_type_for_width(binding.width, binding.signedness),
                )
                relative = tuple(binding.physical_instance_path[len(path):])
                publish(binding, expression, relative)

        emitted_name = f"{current.name}__formal" if top else current.name
        transformed_connections = current.hierarchical_connections
        transformed_request_responses = current.request_response_connections
        transformed_protocol_endpoints = current.protocol_endpoints
        if emitted_name != current.name:
            # The composed emitter recognizes a public endpoint by comparing
            # its typed owner with ``module.name``.  The formal-only top name
            # is physical ABI decoration; retain the exact connection graph
            # while moving only references owned by that renamed top.  Child
            # owners and physical instance identities remain untouched.
            def renamed_endpoint(endpoint):
                return (
                    replace(endpoint, owner=emitted_name)
                    if endpoint.owner == current.name
                    else endpoint
                )

            mapped_connections = {
                id(connection): replace(
                    connection,
                    source=renamed_endpoint(connection.source),
                    destination=renamed_endpoint(connection.destination),
                )
                for connection in current.hierarchical_connections
            }
            transformed_connections = tuple(
                mapped_connections[id(connection)]
                for connection in current.hierarchical_connections
            )
            transformed_request_responses = tuple(
                replace(
                    descriptor,
                    request=mapped_connections[id(descriptor.request)],
                    response=mapped_connections[id(descriptor.response)],
                    requester=(
                        emitted_name
                        if descriptor.requester == current.name
                        else descriptor.requester
                    ),
                    responder=(
                        emitted_name
                        if descriptor.responder == current.name
                        else descriptor.responder
                    ),
                )
                for descriptor in current.request_response_connections
            )
            transformed_protocol_endpoints = tuple(
                renamed_endpoint(endpoint)
                for endpoint in current.protocol_endpoints
            )
        transformed_elaboration = tuple(
            replace(
                item,
                semantic_path=(emitted_name, item.instance.name),
            )
            for item in current.elaborated_instances
        )
        transformed = replace(
            current,
            name=emitted_name,
            ports=tuple(ports), assignments=tuple(assignments),
            children=tuple(transformed_children),
            elaborated_instances=transformed_elaboration,
            protocol_endpoints=transformed_protocol_endpoints,
            hierarchical_connections=transformed_connections,
            request_response_connections=transformed_request_responses,
        )
        if deferred_rule_outputs:
            # Resolve reset only after formal ports and the physical top name
            # have been allocated.  The same reset helper and final component
            # scope are used by production state emission; no raw-port or
            # generated-name convention is substituted for that contract.
            reset_component = transformed if top else _native_release_module(transformed)
            transformed = replace(
                transformed,
                assignments=tuple(
                    replace(
                        assignment,
                        expression=_formal_rule_fire_reset(reset_component, assignment.expression),
                    )
                    if assignment.target.name in deferred_rule_outputs else assignment
                    for assignment in transformed.assignments
                ),
            )
        if deferred_rr_outputs:
            # Ledger spellings depend on the allocated physical owner. The
            # formal top decoration and generic-helper physicalization can
            # change a collision suffix, so resolve these typed projections
            # only in the final emitted namespace, never the source namespace.
            physical_component = _physicalize_generic_callables(transformed)
            projected_assignments = []
            for assignment in transformed.assignments:
                semantic_id = deferred_rr_outputs.get(assignment.target.name)
                if semantic_id is None:
                    projected_assignments.append(assignment)
                    continue
                projection = _formal_local_expression(physical_component, semantic_id)
                if projection is None:
                    raise SystemVerilogEmissionError(
                        "formal request/response projection lost its typed observation"
                    )
                if projection.type != assignment.target.type:
                    projection = expr.Bitcast(
                        projection, assignment.target.type,
                        origin=getattr(projection, "origin", None),
                    )
                projected_assignments.append(replace(assignment, expression=projection))
            transformed = replace(transformed, assignments=tuple(projected_assignments))
        return transformed, output_map

    formal, _ = walk(module, (module.name,), top=True)
    return (
        formal,
        available,
        tuple(sorted(
            formal_buffer_counts,
            key=lambda item: (
                item.request_response_semantic_id,
                item.channel.value,
                item.signal,
            ),
        )),
        tuple(sorted(
            formal_adapter_counts,
            key=lambda item: (
                item.observation_semantic_id,
                item.source_port,
                item.destination_port,
                item.signal,
            ),
        )),
    )


def _hardware_type_for_width(width: int, signedness: str) -> HardwareType:
    if width == 1 and signedness == "bit":
        return BitType()
    return SIntType(width) if signedness == "signed" else UIntType(width)


def emit_formal_artifact(module: Module, recursive_design: object, *,
                         selected_ir_identity: str | None = None) -> BackendArtifact:
    """Emit a formal-only closed component ABI with explicit observations."""
    _validate_recursive_hierarchy(module, recursive_design)
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
    ) = _instrument_direct_formal_module(module, recursive_design, top_tokens)
    text = emit(
        formal_module,
        _formal_buffer_counts=formal_buffer_counts,
        _formal_adapter_counts=formal_adapter_counts,
    )
    wrapper_name = _identifier(formal_module.name)
    digest = hashlib.sha256(text.encode()).hexdigest()
    bound = []
    for item in recursive_design.bindings:
        locator = None
        if item.semantic_binding_id in available:
            locator = BackendPhysicalLocator(
                "direct_systemverilog", digest, wrapper_name,
                (), item.ref.local_semantic_id,
                top_tokens[item.semantic_binding_id],
            )
        bound.append(replace(item, locator=locator))
    formal_design = replace(recursive_design, bindings=tuple(bound))
    identity = selected_ir_identity or default_selected_ir_identity(module)
    artifact = publish_artifact(
        module, text, backend="direct_systemverilog",
        selected_ir_identity=identity,
        rtl_names=_top_physical_rtl_names(module),
        recursive_design=formal_design,
        formal_artifact_hash=digest,
        companions=collect_rom_companions(module),
    )
    # ``publish_artifact`` also carries production-only internal equivalence
    # locators (for example the parent RR ledger).  Those signals may appear in
    # the formal implementation text, but they are not public wrapper ports and
    # must never be instantiated as such by the M35 harness.  Formal-only
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
    # wrapper above.  Keep the formal-only physical ABI truthful even when a
    # property needs only clock/reset (and therefore has no observation port
    # from which the connector could otherwise identify the wrapper).
    artifact = replace(
        artifact,
        module=wrapper_name,
        bindings=tuple(
            replace(item, rtl_module=wrapper_name)
            for item in artifact.bindings
        ),
        physical_domains=tuple(
            replace(item, rtl_module=wrapper_name)
            for item in artifact.physical_domains
        ),
    )
    # Exercise the complete manifest/link codec before formal metadata can be
    # cached or published in an immutable verification bundle.
    BackendArtifact.from_json(artifact.to_json())
    return artifact


def _module_at_path(module: Module, path: tuple[str, ...]) -> Module:
    """Return one exact typed module or fail; never return an ancestor."""

    return _validated_hierarchy(module).at(tuple(path)).module


def _csr_observation_token(module: Module, semantic_id: str) -> str | None:
    for block in module.csr_blocks:
        for binding in block.state_bindings:
            base = ir_csr.csr_state_port_name(binding)
            if semantic_id == binding.semantic_state_id:
                return base
            if semantic_id == binding.write_hit_id:
                return ir_csr.csr_write_hit_port_name(binding)
            if semantic_id == binding.write_value_id:
                return ir_csr.csr_write_value_port_name(binding)
    return None


def _recursive_signal_token(module: Module, semantic_id: str) -> str | None:
    """Map a semantic object to a signal emitted by the generic SV ABI.

    This mapping is produced beside the emitter from typed object kinds; it is
    never reconstructed later from a synthesized RTL name.
    """
    if semantic_id == "clock":
        return _identifier(module.clock) if module.clock is not None else None
    if semantic_id == "reset":
        return _identifier(module.reset) if module.reset is not None else None
    if semantic_id.startswith("register:"):
        return _identifier(semantic_id.split(":", 1)[1])
    if semantic_id.startswith("fifo:"):
        return _identifier(semantic_id.split(":", 1)[1].replace(".", "_"))
    if semantic_id.startswith("port:"):
        value = semantic_id.split(":", 1)[1]
        parts = value.split(".")
        if len(parts) == 1:
            port = next((item for item in module.ports if item.name == parts[0]), None)
            if port is None or port.protocol is not InterfaceProtocol.WIRE:
                # Aggregate protocols have no physical base wire. Only their
                # typed leaves may receive locators.
                return None
            return _identifier(parts[0])
        return _identifier(parts[0]) + "_" + "_".join(_identifier(part) for part in parts[1:])
    csr_token = _csr_observation_token(module, semantic_id)
    if csr_token is not None:
        return csr_token
    if semantic_id.startswith("rr:"):
        for descriptor in module.request_response_connections:
            tracker = _request_response_tracker_name(descriptor, module)
            tokens = {
                request_response_observation_id(
                    descriptor.semantic_id,
                    RequestResponseObservationSignal.OUTSTANDING,
                ): tracker,
                request_response_observation_id(
                    descriptor.semantic_id,
                    RequestResponseObservationSignal.REQUEST_ACCEPT,
                ): f"{tracker}_request_transfer",
                request_response_observation_id(
                    descriptor.semantic_id,
                    RequestResponseObservationSignal.RESPONSE_CONSUME,
                ): f"{tracker}_response_transfer",
            }
            if semantic_id in tokens:
                return tokens[semantic_id]
    return None


def _emit_combinational(module: Module) -> str:
    output_names = {port.name for port in module.outputs}
    assigned_names = {
        assignment.target.name
        for assignment in module.assignments
        if assignment.signal is None and assignment.channel is None
    }
    if (
        len(module.assignments) != len(module.outputs)
        or assigned_names != output_names
    ):
        raise SystemVerilogEmissionError(
            "direct combinational emission requires one complete scalar "
            "assignment per output"
        )
    ports = [
        *_clock_reset_port_declarations(module),
        *(_port_declaration(port) for port in module.inputs),
        *(_port_declaration(port) for port in module.outputs),
    ]
    declarations, materialized, render = _materialized_emission(module)
    assignments = [
        line
        for assignment in module.assignments
        for line in (
            f"  // ZLang IR output: {assignment.target.name}",
            f"  assign {_identifier(assignment.target.name)} = "
            f"{render(assignment.expression)};",
        )
    ]
    return _module(
        module,
        ports,
        [
            *declarations,
            *materialized,
            *assignments,
        ],
    )


def _emit_pipeline(module: Module) -> str:
    assigned_names = {
        assignment.target.name
        for assignment in module.assignments
        if assignment.signal is None and assignment.channel is None
    }
    if (
        module.clock is None
        or module.reset is None
        or len(module.assignments) != len(module.outputs)
        or assigned_names != {port.name for port in module.outputs}
    ):
        raise SystemVerilogEmissionError(
            "direct pipeline emission requires every output to have one assignment"
        )
    staged_assignments: list[Assignment] = []
    for candidate in module.assignments:
        candidate_staging: dict[int, expr.Delay | expr.Pipeline] = {}
        _collect_staged_expressions(
            _instance_expression(module, candidate.expression), candidate_staging
        )
        if candidate_staging:
            staged_assignments.append(candidate)
    if len(staged_assignments) != 1:
        raise SystemVerilogEmissionError(
            "direct pipeline emission requires exactly one staged output assignment"
        )
    assignment = staged_assignments[0]
    root = _instance_expression(module, assignment.expression)
    staged: dict[int, expr.Delay | expr.Pipeline] = {}
    _collect_staged_expressions(root, staged)
    if not staged:
        raise SystemVerilogEmissionError(
            "direct sequential datapath emission requires a typed pipeline or delay"
        )
    aliases = {
        node: _staged_signal_name(node, _staged_count(node))
        for node in staged.values()
    }
    physical_roots = tuple(
        _replace_staged_expressions(node.expression, aliases)
        for node in staged.values()
    )
    stage_materialized = plan_materialization(
        physical_roots,
        reserved_names=(
            *(_identifier(port.name) for port in module.ports),
            *aliases.values(),
        ),
        generated_prefix="zlang_stage_expr_",
        # Pipeline-stage roots are already explicit DAG fragments.  A shared
        # multiply has only three expression nodes but must still remain one
        # physical combinational value rather than being copied into siblings.
        minimum_shared_size=2,
    )
    materialized_aliases = {
        item.expression: item.name for item in stage_materialized
    }

    def render(value: expr.Expression) -> str:
        physical = _replace_staged_expressions(value, aliases)
        return _expression(_replace_materialized(physical, materialized_aliases))

    registers: list[str] = []
    resets: list[str] = []
    updates: list[str] = []
    for node in staged.values():
        stages = _staged_count(node)
        if stages <= 0:
            raise SystemVerilogEmissionError("pipeline/delay depth must be positive")
        signed = " signed" if isinstance(node.type, (SIntType, FixedType)) else ""
        for index in range(1, stages + 1):
            name = _staged_signal_name(node, index)
            registers.append(
                f"  logic{signed} {_range(_width(node.type))}{name};"
            )
            resets.append(f"      {name} <= '0;")
        updates.append(
            f"      {_staged_signal_name(node, 1)} <= {render(node.expression)};"
        )
        updates.extend(
            f"      {_staged_signal_name(node, index)} <= "
            f"{_staged_signal_name(node, index - 1)};"
            for index in range(2, stages + 1)
        )
    ports = [
        "input wire logic " + module.clock,
        "input wire logic " + module.reset,
        *(_port_declaration(port) for port in module.inputs),
        *(_port_declaration(port) for port in module.outputs),
    ]
    materialized_declarations = [
        "  logic"
        + (" signed" if isinstance(item.expression.type, (SIntType, FixedType)) else "")
        + f" {_range(_width(item.expression.type))}{item.name};"
        for item in stage_materialized
    ]
    materialized_assignments = [
        f"  assign {item.name} = "
        f"{_expression(_replace_materialized(item.expression, materialized_aliases, keep=item.expression))};"
        for item in dependency_ordered_materialization(stage_materialized)
    ]
    lines = [
        *materialized_declarations,
        *materialized_assignments,
        *registers,
        f"  always_ff @({_clock_event(module, _identifier)}) begin",
        f"    if ({_reset_asserted(module, _identifier)}) begin",
        *resets,
        "    end else begin",
        *updates,
        "    end",
        "  end",
        f"  assign {_identifier(assignment.target.name)} = {render(root)};",
        *(
            f"  assign {_identifier(item.target.name)} = "
            f"{render(_instance_expression(module, item.expression))};"
            for item in module.assignments
            if item is not assignment
        ),
    ]
    return _module(module, ports, lines)


def _emit_elastic_pipeline(module: Module) -> str:
    """Emit the frozen single-region global-clock-enable elastic kernel."""

    if (
        module.clock is None
        or module.reset is None
        or len(module.elastic_pipeline_regions) != 1
    ):
        raise SystemVerilogEmissionError(
            "elastic pipeline emission requires one region and clock/reset"
        )
    region = module.elastic_pipeline_regions[0]
    source = next(port for port in module.ports if port.name == region.source_endpoint)
    destination = next(
        port for port in module.ports if port.name == region.destination_endpoint
    )
    if (
        source.direction is not PortDirection.INPUT
        or destination.direction is not PortDirection.OUTPUT
        or source.protocol is not InterfaceProtocol.READY_VALID
        or destination.protocol is not InterfaceProtocol.READY_VALID
    ):
        raise SystemVerilogEmissionError(
            "elastic pipeline endpoints do not match their typed ready/valid ABI"
        )
    # Production elastic modules have no ordinary assignments.  The formal
    # artifact instrumentation may add explicit output-only observation
    # projections, and the specialized emitter must preserve those instead of
    # silently dropping them.  Keep this narrowly fail-closed so this does not
    # become a second compositional assignment engine.
    if any(
        assignment.target.direction is not PortDirection.OUTPUT
        or assignment.target.protocol is not InterfaceProtocol.WIRE
        or assignment.signal is not None
        or assignment.channel is not None
        for assignment in module.assignments
    ):
        raise SystemVerilogEmissionError(
            "elastic pipeline accepts only formal output observation projections"
        )
    root = region.selected_candidate.expression
    staged: dict[int, expr.Delay | expr.Pipeline] = {}
    _collect_staged_expressions(root, staged)
    if any(isinstance(node, expr.Delay) for node in staged.values()):
        raise SystemVerilogEmissionError(
            "elastic pipeline does not accept fixed Delay nodes"
        )
    physical = tuple(sorted((_stage.instance, _stage.stages) for _stage in staged.values()))
    if physical != region.plan.data_stage_instances:
        raise SystemVerilogEmissionError(
            "elastic pipeline typed data stages do not match the frozen plan"
        )
    aliases = {
        node: _staged_signal_name(node, _staged_count(node))
        for node in staged.values()
    }

    def render(value: expr.Expression) -> str:
        return _expression(_replace_staged_expressions(value, aliases))

    registers: list[str] = []
    resets: list[str] = []
    updates: list[str] = []
    for node in staged.values():
        signed = " signed" if isinstance(node.type, (SIntType, FixedType)) else ""
        for index in range(1, node.stages + 1):
            name = _staged_signal_name(node, index)
            registers.append(f"  logic{signed} {_range(_width(node.type))}{name};")
            resets.append(f"      {name} <= '0;")
        updates.append(
            f"      {_staged_signal_name(node, 1)} <= {render(node.expression)};"
        )
        updates.extend(
            f"      {_staged_signal_name(node, index)} <= "
            f"{_staged_signal_name(node, index - 1)};"
            for index in range(2, node.stages + 1)
        )

    latency = region.timing.minimum_unstalled_latency
    valid_names = [f"zlang_elastic_valid_{index}" for index in range(latency)]
    source_name = _identifier(source.name)
    destination_name = _identifier(destination.name)
    advance = "zlang_elastic_advance"
    lines = [
        *registers,
        *(f"  logic {name};" for name in valid_names),
        f"  logic {advance};",
        f"  assign {advance} = {_reset_deasserted(module, _identifier)} && "
        f"(!{valid_names[-1]} || {destination_name}_ready);",
        f"  assign {source_name}_ready = {advance};",
        f"  assign {destination_name}_payload = {render(root)};",
        f"  assign {destination_name}_valid = "
        f"{_reset_deasserted(module, _identifier)} && {valid_names[-1]};",
        *(
            f"  assign {_identifier(assignment.target.name)} = "
            f"{_expression(assignment.expression)};"
            for assignment in module.assignments
        ),
        f"  always_ff @({_clock_event(module, _identifier)}) begin",
        f"    if ({_reset_asserted(module, _identifier)}) begin",
        *resets,
        *(f"      {name} <= 1'b0;" for name in valid_names),
        f"    end else if ({advance}) begin",
        *updates,
        f"      {valid_names[0]} <= {source_name}_valid;",
        *(
            f"      {valid_names[index]} <= {valid_names[index - 1]};"
            for index in range(1, latency)
        ),
        "    end",
        "  end",
    ]
    return _module(module, _physical_port_declarations(module), lines)


def _staged_count(value: expr.Delay | expr.Pipeline) -> int:
    return value.cycles if isinstance(value, expr.Delay) else value.stages


def _staged_signal_name(value: expr.Delay | expr.Pipeline, stage: int) -> str:
    prefix = "delay" if isinstance(value, expr.Delay) else "pipeline"
    # Trivial fixed pipelines retain one enclosing N-cycle node for the public
    # IR shape.  Expose each physical register with the historical per-stage
    # instance spelling so hierarchy-local naming and emitted RTL remain
    # compatible (pipeline_0_s1, pipeline_1_s1, ...).
    if (
        isinstance(value, expr.Pipeline)
        and value.pipeline_plan is None
        and value.stages > 1
    ):
        return f"{prefix}_{value.instance + stage - 1}_s1"
    return f"{prefix}_{value.instance}_s{stage}"


def _collect_staged_expressions(
    value: expr.Expression,
    found: dict[int, expr.Delay | expr.Pipeline],
) -> None:
    for child in _expression_children(value):
        _collect_staged_expressions(child, found)
    if isinstance(value, (expr.Delay, expr.Pipeline)):
        previous = found.get(value.instance)
        if previous is not None and previous != value:
            raise SystemVerilogEmissionError(
                f"pipeline instance {value.instance} has conflicting typed definitions"
            )
        found.setdefault(value.instance, value)


def _replace_staged_expressions(
    value: expr.Expression,
    aliases: dict[expr.Delay | expr.Pipeline, str],
) -> expr.Expression:
    return _replace_materialized(value, aliases)


def _emit_ready_valid(module: Module) -> str:
    if module.registers or module.rules or module.fifos or module.memories:
        raise SystemVerilogEmissionError(
            "the direct ready/valid experiment supports combinational endpoints"
        )
    # A stateless protocol component may still declare a clock/reset domain so
    # it composes with clocked children and publishes a stable aggregate ABI.
    # Those timing ports do not make its data path sequential.
    ports: list[str] = [
        *(
            f"input logic {_identifier(name)}"
            for name in (module.clock,)
            if name is not None
        ),
        *(
            f"input logic {_identifier(name)}"
            for name in (module.reset,)
            if name is not None
        ),
    ]
    for port in module.ports:
        name = _identifier(port.name)
        if port.protocol is not InterfaceProtocol.READY_VALID:
            ports.append(_port_declaration(port))
        elif port.direction is PortDirection.INPUT:
            ports.extend(
                (
                    _logic_port("input", f"{name}_payload", port.type),
                    f"input logic {name}_valid",
                    f"output logic {name}_ready",
                )
            )
        else:
            ports.extend(
                (
                    _logic_port("output", f"{name}_payload", port.type),
                    f"output logic {name}_valid",
                    f"input logic {name}_ready",
                )
            )
    lines = [
        f"  assign {_assignment_name(assignment)} = "
        f"{_expression(assignment.expression)};"
        for assignment in module.assignments
    ]
    return _module(module, ports, lines)


def _emit_credit(module: Module) -> str:
    if module.clock is None or module.reset is None:
        raise SystemVerilogEmissionError(
            "direct credit emission requires one clock and reset"
        )
    credit_ports = tuple(
        port for port in module.ports
        if port.protocol is InterfaceProtocol.CREDIT
    )
    if len(credit_ports) != 1 or credit_ports[0].direction is not PortDirection.OUTPUT:
        raise SystemVerilogEmissionError(
            "the direct credit experiment supports one sender endpoint"
        )
    endpoint = credit_ports[0]
    assert endpoint.capacity is not None
    payload_assignment = _find_signal_assignment(
        module, endpoint.name, CreditSignal.PAYLOAD
    )
    send_assignment = _find_signal_assignment(
        module, endpoint.name, CreditSignal.SEND
    )
    count_width = max(1, endpoint.capacity.bit_length())
    ports = [
        f"input logic {_identifier(module.clock)}",
        f"input logic {_identifier(module.reset)}",
        *(
            _port_declaration(port)
            for port in module.ports
            if port.protocol is InterfaceProtocol.WIRE
        ),
        f"input logic {endpoint.name}_return",
        _logic_port("output", f"{endpoint.name}_payload", endpoint.type),
        f"output logic {endpoint.name}_send",
    ]
    name = endpoint.name
    lines = [
        f"  logic [{count_width - 1}:0] {name}_credits;",
        f"  assign {name}_payload = {_expression(payload_assignment.expression)};",
        f"  assign {name}_send = {_reset_deasserted(module, _identifier)} && ({name}_credits != '0) "
        f"&& {_expression(send_assignment.expression)};",
        *(
            f"  assign {_assignment_name(assignment)} = "
            f"{_expression(assignment.expression)};"
            for assignment in module.assignments
            if isinstance(assignment.target, Port)
            and assignment.target.protocol is InterfaceProtocol.WIRE
            and assignment.target.direction is PortDirection.OUTPUT
        ),
        f"  always_ff @({_clock_event(module, _identifier)}) begin",
        f"    if ({_reset_asserted(module, _identifier)}) begin",
        f"      {name}_credits <= {count_width}'d{endpoint.capacity};",
        "    end else begin",
        f"      case ({{{name}_send, {name}_return}})",
        f"        2'b10: {name}_credits <= {name}_credits - 1'b1;",
        f"        2'b01: if ({name}_credits < {count_width}'d{endpoint.capacity}) "
        f"{name}_credits <= {name}_credits + 1'b1;",
        f"        default: {name}_credits <= {name}_credits;",
        "      endcase",
        "    end",
        "  end",
    ]
    return _module(module, ports, lines)


def _emit_vc_credit(module: Module) -> str:
    if module.clock is None or module.reset is None:
        raise SystemVerilogEmissionError(
            "direct VC-credit emission requires one clock and reset"
        )
    endpoints = tuple(
        port for port in module.ports
        if port.protocol is InterfaceProtocol.VC_CREDIT
    )
    if len(endpoints) != 1 or endpoints[0].direction is not PortDirection.OUTPUT:
        raise SystemVerilogEmissionError(
            "direct VC-credit emission supports one sender endpoint"
        )
    endpoint = endpoints[0]
    if endpoint.capacity is None or endpoint.virtual_channels is None:
        raise SystemVerilogEmissionError("VC-credit endpoint has no typed bounds")
    payload = _find_signal_assignment(
        module, endpoint.name, VirtualChannelCreditSignal.PAYLOAD
    )
    channel = _find_signal_assignment(
        module, endpoint.name, VirtualChannelCreditSignal.VC
    )
    request = _find_signal_assignment(
        module, endpoint.name, VirtualChannelCreditSignal.SEND
    )
    name = _identifier(endpoint.name)
    clock = _identifier(module.clock)
    reset = _identifier(module.reset)
    capacity = endpoint.capacity
    virtual_channels = endpoint.virtual_channels
    count_width = max(1, capacity.bit_length())
    vc_width = max(1, (virtual_channels - 1).bit_length())
    ports = [
        f"input logic {clock}",
        f"input logic {reset}",
        *(
            _port_declaration(port)
            for port in module.ports
            if port.protocol is InterfaceProtocol.WIRE
        ),
        f"input logic {name}_return",
        f"input logic {_range(vc_width)}{name}_return_vc",
        _logic_port("output", f"{name}_payload", endpoint.type),
        f"output logic {_range(vc_width)}{name}_vc",
        f"output logic {name}_send",
    ]
    lines = [
        *(
            f"  logic [{count_width - 1}:0] {name}_credits_{index};"
            for index in range(virtual_channels)
        ),
        f"  logic {name}_can_send;",
        f"  assign {name}_payload = {_expression(payload.expression)};",
        f"  assign {name}_vc = {_expression(channel.expression)};",
        f"  always_comb begin",
        f"    {name}_can_send = 1'b0;",
        f"    case ({name}_vc)",
        *(
            f"      {vc_width}'d{index}: {name}_can_send = "
            f"({name}_credits_{index} != '0);"
            for index in range(virtual_channels)
        ),
        "      default: begin end",
        "    endcase",
        "  end",
        f"  assign {name}_send = {_reset_deasserted(module, _identifier)} && "
        f"({_expression(request.expression)}) && {name}_can_send;",
        f"  always_ff @({_clock_event(module, _identifier)}) begin",
        f"    if ({_reset_asserted(module, _identifier)}) begin",
        *(
            f"      {name}_credits_{index} <= {count_width}'d{capacity};"
            for index in range(virtual_channels)
        ),
        "    end else begin",
    ]
    for index in range(virtual_channels):
        sent = f"({name}_send && {name}_vc == {vc_width}'d{index})"
        returned = (
            f"({name}_return && {name}_return_vc == {vc_width}'d{index})"
        )
        lines.extend((
            f"      case ({{{sent}, {returned}}})",
            f"        2'b10: if ({name}_credits_{index} != '0) "
            f"{name}_credits_{index} <= {name}_credits_{index} - 1'b1;",
            f"        2'b01: if ({name}_credits_{index} < "
            f"{count_width}'d{capacity}) {name}_credits_{index} <= "
            f"{name}_credits_{index} + 1'b1;",
            f"        default: {name}_credits_{index} <= {name}_credits_{index};",
            "      endcase",
        ))
    lines.extend(("    end", "  end"))
    return _module(module, ports, lines)


def _validate_formal_adapter_count_projection(
    module: Module,
    projection: FormalAdapterCountProjection,
) -> tuple[Assignment, ...]:
    """Seal one formal projection against its typed adapter and output port."""

    if len(module.connections) != 1:
        raise SystemVerilogEmissionError(
            "formal adapter count projection requires exactly one typed connection"
        )
    connection = module.connections[0]
    source = connection.source
    destination = connection.destination
    expected_width = max(1, connection.buffer_depth.bit_length())
    if (
        projection.observation_semantic_id
        != port_observation_id(source.name, "occupancy")
        or projection.source_port != source.name
        or projection.destination_port != destination.name
        or projection.adapter is not ConnectionAdapter.CREDIT_TO_READY_VALID
        or connection.adapter is not projection.adapter
        or source.protocol is not InterfaceProtocol.CREDIT
        or source.direction is not PortDirection.INPUT
        or destination.protocol is not InterfaceProtocol.READY_VALID
        or destination.direction is not PortDirection.OUTPUT
        or source.capacity is None
        or projection.depth != source.capacity
        or connection.buffer_depth != projection.depth
        or projection.width != expected_width
        or source.type != destination.type
        or source.domain != destination.domain
    ):
        raise SystemVerilogEmissionError(
            "formal adapter count projection disagrees with the typed "
            "credit_to_rv connection"
        )
    if _identifier(projection.signal) != projection.signal:
        raise SystemVerilogEmissionError(
            "formal adapter count projection has an invalid physical signal"
        )
    count_matches = 0
    seen_targets: set[str] = set()
    validated: list[Assignment] = []
    for assignment in module.assignments:
        target = assignment.target
        value = assignment.expression
        if (
            assignment.signal is not None
            or assignment.channel is not None
            or not isinstance(target, Port)
            or target.direction is not PortDirection.OUTPUT
            or target.protocol is not InterfaceProtocol.WIRE
            or target.type != value.type
            or target.name in seen_targets
        ):
            raise SystemVerilogEmissionError(
                "closed protocol adapter accepts only exact formal observation "
                "projection assignments"
            )
        is_count = (
            isinstance(value, expr.InputRef)
            and value.name == projection.signal
            and value.type == UIntType(projection.width)
        )
        is_source_leaf = (
            isinstance(value, expr.CreditRef)
            and value.interface == source.name
            and value.signal in {
                CreditSignal.PAYLOAD,
                CreditSignal.SEND,
                CreditSignal.RETURN,
            }
        )
        is_destination_leaf = (
            isinstance(value, expr.ReadyValidRef)
            and value.interface == destination.name
            and value.signal in {
                ReadyValidSignal.PAYLOAD,
                ReadyValidSignal.VALID,
                ReadyValidSignal.READY,
            }
        )
        if not (is_count or is_source_leaf or is_destination_leaf):
            raise SystemVerilogEmissionError(
                "closed protocol adapter formal ABI contains an observation "
                "outside its typed endpoints or FIFO count"
            )
        count_matches += int(is_count)
        seen_targets.add(target.name)
        validated.append(assignment)
    if count_matches != 1:
        raise SystemVerilogEmissionError(
            "closed protocol adapter formal ABI requires exactly one typed "
            "FIFO count projection"
        )
    return tuple(validated)


def _emit_connection_adapter(
    module: Module,
    *,
    formal_adapter_counts: tuple[FormalAdapterCountProjection, ...] = (),
) -> str:
    if (
        module.clock is None
        or module.reset is None
        or len(module.connections) != 1
        or module.registers
        or module.rules
        or module.fifos
        or module.memories
        or module.csr_blocks
        or module.elaborated_instances
    ):
        raise SystemVerilogEmissionError(
            "direct protocol adapter requires one closed clocked connection"
        )
    if len(formal_adapter_counts) > 1:
        raise SystemVerilogEmissionError(
            "duplicate formal count projections for one protocol adapter"
        )
    formal_assignments = (
        _validate_formal_adapter_count_projection(
            module, formal_adapter_counts[0]
        )
        if formal_adapter_counts else None
    )
    if formal_assignments is None and module.assignments:
        raise SystemVerilogEmissionError(
            "direct protocol adapter requires one closed clocked connection"
        )
    connection = module.connections[0]
    source, destination = connection.source, connection.destination
    if source.type != destination.type or source.domain != destination.domain:
        raise SystemVerilogEmissionError(
            "direct protocol adapter requires identical payload type and domain"
        )

    ports = [
        f"input logic {_identifier(module.clock)}",
        f"input logic {_identifier(module.reset)}",
    ]
    for port in module.ports:
        name = _identifier(port.name)
        if port.protocol is InterfaceProtocol.READY_VALID:
            if port.direction is PortDirection.INPUT:
                ports.extend((
                    _logic_port("input", f"{name}_payload", port.type),
                    f"input logic {name}_valid",
                    f"output logic {name}_ready",
                ))
            else:
                ports.extend((
                    _logic_port("output", f"{name}_payload", port.type),
                    f"output logic {name}_valid",
                    f"input logic {name}_ready",
                ))
        elif port.protocol is InterfaceProtocol.CREDIT:
            if port.direction is PortDirection.INPUT:
                ports.extend((
                    _logic_port("input", f"{name}_payload", port.type),
                    f"input logic {name}_send",
                    f"output logic {name}_return",
                ))
            else:
                ports.extend((
                    _logic_port("output", f"{name}_payload", port.type),
                    f"output logic {name}_send",
                    f"input logic {name}_return",
                ))
        elif (
            formal_assignments is not None
            and port in {item.target for item in formal_assignments}
        ):
            ports.append(_port_declaration(port))
        else:
            raise SystemVerilogEmissionError(
                "direct protocol adapter accepts only ready/valid and credit ports"
            )

    src = _identifier(source.name)
    dst = _identifier(destination.name)
    clock = _identifier(module.clock)
    reset = _effective_reset_signal(module, _identifier)
    reset_deasserted = _reset_deasserted(module, _identifier)
    if connection.adapter is ConnectionAdapter.READY_VALID_TO_CREDIT:
        if (
            source.protocol is not InterfaceProtocol.READY_VALID
            or destination.protocol is not InterfaceProtocol.CREDIT
            or destination.capacity is None
        ):
            raise SystemVerilogEmissionError("invalid typed rv_to_credit adapter")
        capacity = destination.capacity
        count_width = max(1, capacity.bit_length())
        lines = [
            f"  logic [{count_width - 1}:0] {dst}_credits;",
            f"  assign {src}_ready = {_reset_deasserted(module, _identifier)} && ({dst}_credits != '0);",
            f"  assign {dst}_payload = {src}_payload;",
            f"  assign {dst}_send = {src}_valid && {src}_ready;",
            f"  always_ff @({_clock_event(module, _identifier)}) begin",
            f"    if ({_reset_asserted(module, _identifier)}) {dst}_credits <= {count_width}'d{capacity};",
            "    else begin",
            f"      case ({{{dst}_send, {dst}_return}})",
            f"        2'b10: {dst}_credits <= {dst}_credits - 1'b1;",
            f"        2'b01: if ({dst}_credits < {count_width}'d{capacity}) "
            f"{dst}_credits <= {dst}_credits + 1'b1;",
            f"        default: {dst}_credits <= {dst}_credits;",
            "      endcase",
            "    end",
            "  end",
        ]
        return _module(module, ports, lines)

    if connection.adapter is ConnectionAdapter.CREDIT_TO_READY_VALID:
        if (
            source.protocol is not InterfaceProtocol.CREDIT
            or destination.protocol is not InterfaceProtocol.READY_VALID
            or source.capacity is None
            or connection.buffer_depth != source.capacity
        ):
            raise SystemVerilogEmissionError(
                "invalid typed credit_to_rv adapter buffer"
            )
        depth = connection.buffer_depth
        width = _width(source.type)
        projection = (
            formal_adapter_counts[0] if formal_adapter_counts else None
        )
        helper = (
            f"ZLangRvFifoFormal_{width}_{depth}"
            if projection is not None
            else f"ZLangRvFifo_{width}_{depth}"
        )
        lines = [
            f"  logic {src}_accepted;",
            f"  logic {_range(width)}{dst}_buffer_payload;",
            f"  logic {dst}_buffer_valid, {dst}_buffer_ready;",
            *(
                (f"  logic {_range(projection.width)}{projection.signal};",)
                if projection is not None else ()
            ),
            f"  {helper} zlang_adapter_fifo (",
            f"    .clk({clock}), .rst({reset}),",
            f"    .in_payload({src}_payload), "
            f".in_valid({src}_send && {reset_deasserted}),",
            f"    .in_ready({src}_accepted),",
            f"    .out_payload({dst}_buffer_payload),",
            f"    .out_valid({dst}_buffer_valid), .out_ready({dst}_buffer_ready)"
            + ("," if projection is not None else ");"),
            *(
                (f"    .formal_count({projection.signal}));",)
                if projection is not None else ()
            ),
            f"  assign {dst}_payload = {dst}_buffer_payload;",
            f"  assign {dst}_valid = {reset_deasserted} && "
            f"{dst}_buffer_valid;",
            f"  assign {dst}_buffer_ready = {reset_deasserted} && "
            f"{dst}_ready;",
            f"  assign {src}_return = {dst}_valid && {dst}_ready;",
            *(
                tuple(
                    f"  assign {_identifier(assignment.target.name)} = "
                    f"{_expression(assignment.expression)};"
                    for assignment in formal_assignments
                )
                if formal_assignments is not None else ()
            ),
        ]
        return _rv_fifo_helper(
            helper, width, depth, module,
            expose_count=projection is not None,
            allow_full_replace=True,
        ) + "\n" + _module(module, ports, lines)

    raise SystemVerilogEmissionError(
        f"unsupported direct protocol adapter '{connection.adapter}'"
    )


def _embedded_staging_emission(
    module: Module,
) -> tuple[list[str], list[str], object]:
    """Materialize nested Delay/Pipeline nodes without moving their boundary."""

    staged: list[expr.Delay | expr.Pipeline] = []
    local_names = module_rtl_names(module)
    seen: set[tuple[type[expr.Expression], int]] = set()

    def collect(value: expr.Expression) -> None:
        for child in _expression_children(value):
            collect(child)
        if isinstance(value, (expr.Delay, expr.Pipeline)):
            key = (type(value), value.instance)
            if key not in seen:
                seen.add(key)
                staged.append(value)

    for root in _module_expression_roots(module, local_names):
        collect(root)

    if staged and (module.clock is None or module.reset is None):
        raise SystemVerilogEmissionError(
            "staged hierarchical component requires clock and reset"
        )

    aliases: dict[expr.Expression, str] = {}
    declarations: list[str] = []
    resets: list[str] = []
    updates: list[str] = []
    for value in staged:
        count = value.cycles if isinstance(value, expr.Delay) else value.stages
        kind = "delay" if isinstance(value, expr.Delay) else "pipeline"
        signed = " signed" if isinstance(value.type, (SIntType, FixedType)) else ""
        for index in range(1, count + 1):
            name = local_names.stage(kind, value.instance, index)
            declarations.append(
                f"  logic{signed} {_range(_width(value.type))}{name};"
            )
            resets.append(f"      {name} <= '0;")
        physical_input = _replace_materialized(value.expression, aliases)
        updates.append(
            f"      {local_names.stage(kind, value.instance, 1)} <= {_expression(physical_input)};"
        )
        for index in range(2, count + 1):
            updates.append(
                f"      {local_names.stage(kind, value.instance, index)} <= "
                f"{local_names.stage(kind, value.instance, index - 1)};"
            )
        aliases[value] = local_names.stage(kind, value.instance, count)

    sequential: list[str] = []
    if staged:
        sequential.extend((
            f"  always_ff @({_clock_event(module, _identifier)}) begin",
            f"    if ({_reset_asserted(module, _identifier)}) begin",
            *resets,
            "    end else begin",
            *updates,
            "    end",
            "  end",
        ))

    def render(value: expr.Expression) -> str:
        physical = _instance_expression(module, value, local_names)
        return _expression(_replace_materialized(physical, aliases))

    return declarations, sequential, render


def _append_rule_state(
    module: Module,
    declarations: list[str],
    logic: list[str],
    render: Callable[[expr.Expression], str],
    *,
    compact_reset: bool = False,
) -> None:
    """Append classifier-approved scalar register/rule state to a module body."""

    if module.registers and (module.clock is None or module.reset is None):
        raise SystemVerilogEmissionError("direct rule emission requires clock/reset")
    ordered_rules = _ordered_rules(module)
    declarations.extend(
        _logic_declaration(
            rtl_register_state_identifier(register.name), register.type
        )
        for register in module.registers
    )
    for register in module.registers:
        writers = tuple(
            (rule, action)
            for rule in ordered_rules
            for action in rule.actions
            if action.target.name == register.name
        )
        default = next(
            (
                assignment.expression
                for assignment in module.next_assignments
                if assignment.target.name == register.name
            ),
            None,
        )
        logic.append(f"  always_ff @({_clock_event(module, _identifier)}) begin")
        if compact_reset:
            logic.extend((
                f"    if ({_reset_asserted(module, _identifier)}) "
                f"{_identifier(register.name)} <= {render(register.initial)};",
                "    else begin",
            ))
        else:
            logic.extend((
                f"    if ({_reset_asserted(module, _identifier)}) begin",
                f"      {_identifier(register.name)} <= {render(register.initial)};",
                "    end else begin",
            ))
        for index, (rule, action) in enumerate(writers):
            keyword = "if" if index == 0 else "else if"
            logic.append(
                f"      {keyword} ({render(rule.guard)}) "
                f"{_identifier(register.name)} <= {render(action.expression)};"
            )
        if default is not None:
            prefix = "      else " if writers else "      "
            logic.append(
                f"{prefix}{_identifier(register.name)} <= {render(default)};"
            )
        elif writers:
            logic.append(
                f"      else {_identifier(register.name)} <= "
                f"{_identifier(register.name)};"
            )
        logic.extend(("    end", "  end"))
    logic.extend(
        f"  assign {_assignment_name(assignment)} = "
        f"{render(assignment.expression)};"
        for assignment in module.assignments
    )


def _emit_rules(module: Module) -> str:
    if module.clock is None or module.reset is None:
        raise SystemVerilogEmissionError("direct rule emission requires clock/reset")
    ports = [
        f"input logic {module.clock}",
        f"input logic {module.reset}",
        *(_port_declaration(port) for port in module.inputs),
        *(_port_declaration(port) for port in module.outputs),
    ]
    declarations, logic, render = _embedded_staging_emission(module)
    _append_rule_state(module, declarations, logic, render)
    return _module(module, ports, [*declarations, *logic])


def _emit_packet_arbiter(module: Module) -> str:
    if module.clock is None or module.reset is None or len(module.arbiters) != 1:
        raise SystemVerilogEmissionError(
            "direct packet arbitration requires one clocked arbiter"
        )
    if (
        module.assignments or module.connections or module.registers
        or module.rules or module.fifos or module.memories or module.csr_blocks
        or module.request_responses
    ):
        raise SystemVerilogEmissionError(
            "direct packet arbiter currently requires only arbiter endpoints"
        )
    arbiter = module.arbiters[0]
    endpoints = {
        *(source.name for source in arbiter.sources),
        arbiter.destination.name,
    }
    if {port.name for port in module.ports} != endpoints:
        raise SystemVerilogEmissionError(
            "direct packet arbiter currently requires only arbiter endpoints"
        )

    sources = tuple(_identifier(source.name) for source in arbiter.sources)
    destination = _identifier(arbiter.destination.name)
    count = len(sources)
    owner_width = max(1, (count - 1).bit_length())
    ports = [
        f"input wire logic {_identifier(module.clock)}",
        f"input wire logic {_identifier(module.reset)}",
    ]
    for source, name in zip(arbiter.sources, sources, strict=True):
        ports.extend((
            _logic_port("input", f"{name}_payload", source.type),
            f"input wire logic {name}_valid",
            f"input wire logic {name}_last",
            f"output logic {name}_ready",
        ))
    ports.extend((
        _logic_port("output", f"{destination}_payload", arbiter.destination.type),
        f"output logic {destination}_valid",
        f"output logic {destination}_last",
        f"input wire logic {destination}_ready",
    ))

    lines = [
        f"  logic [{owner_width - 1}:0] zlang_candidate;",
        "  logic zlang_candidate_valid;",
        f"  logic [{owner_width - 1}:0] zlang_selected;",
        "  logic zlang_grant_active;",
        f"  logic [{owner_width - 1}:0] zlang_grant_owner;",
        "  logic zlang_transfer, zlang_grant_complete;",
    ]
    if arbiter.policy is ArbitrationPolicy.ROUND_ROBIN:
        lines.append(f"  logic [{owner_width - 1}:0] zlang_next_priority;")

    lines.extend((
        "  always_comb begin",
        "    zlang_candidate = '0;",
        "    zlang_candidate_valid = 1'b0;",
    ))
    if arbiter.policy is ArbitrationPolicy.FIXED_PRIORITY:
        for index, name in enumerate(sources):
            keyword = "if" if index == 0 else "else if"
            lines.extend((
                f"    {keyword} ({name}_valid) begin",
                f"      zlang_candidate = {owner_width}'d{index};",
                "      zlang_candidate_valid = 1'b1;",
                "    end",
            ))
    else:
        lines.append("    case (zlang_next_priority)")
        for start in range(count):
            lines.append(f"      {owner_width}'d{start}: begin")
            for offset in range(count):
                index = (start + offset) % count
                keyword = "if" if offset == 0 else "else if"
                lines.extend((
                    f"        {keyword} ({sources[index]}_valid) begin",
                    f"          zlang_candidate = {owner_width}'d{index};",
                    "          zlang_candidate_valid = 1'b1;",
                    "        end",
                ))
            lines.append("      end")
        lines.extend(("      default: begin end", "    endcase"))
    lines.extend((
        "  end",
        "  always_comb begin",
        "    zlang_selected = zlang_grant_active ? zlang_grant_owner : zlang_candidate;",
        f"    {destination}_payload = '0;",
        f"    {destination}_valid = 1'b0;",
        f"    {destination}_last = 1'b0;",
        *(f"    {name}_ready = 1'b0;" for name in sources),
        "    case (zlang_selected)",
    ))
    for index, name in enumerate(sources):
        lines.extend((
            f"      {owner_width}'d{index}: begin",
            f"        {destination}_payload = {name}_payload;",
            f"        {destination}_valid = {name}_valid;",
            f"        {destination}_last = {name}_last;",
            "      end",
        ))
    lines.extend((
        "      default: begin end",
        "    endcase",
        f"    if ({_reset_asserted(module, _identifier)}) {destination}_valid = 1'b0;",
        f"    if ({_reset_deasserted(module, _identifier)} && {destination}_valid && {destination}_ready) begin",
        "      case (zlang_selected)",
        *(f"        {owner_width}'d{index}: {name}_ready = 1'b1;"
          for index, name in enumerate(sources)),
        "        default: begin end",
        "      endcase",
        "    end",
        "  end",
        f"  assign zlang_transfer = {destination}_valid && {destination}_ready;",
        (
            "  assign zlang_grant_complete = zlang_transfer;"
            if arbiter.grant_scope is GrantScope.BEAT
            else f"  assign zlang_grant_complete = zlang_transfer && {destination}_last;"
        ),
        f"  always_ff @({_clock_event(module, _identifier)}) begin",
        f"    if ({_reset_asserted(module, _identifier)}) begin",
        "      zlang_grant_active <= 1'b0;",
        "      zlang_grant_owner <= '0;",
    ))
    if arbiter.policy is ArbitrationPolicy.ROUND_ROBIN:
        lines.append("      zlang_next_priority <= '0;")
    lines.extend((
        "    end else begin",
        "      if (!zlang_grant_active && zlang_candidate_valid) begin",
        "        zlang_grant_owner <= zlang_selected;",
        "        zlang_grant_active <= !zlang_grant_complete;",
        "      end else if (zlang_grant_active && zlang_grant_complete) begin",
        "        zlang_grant_active <= 1'b0;",
        "      end",
    ))
    if arbiter.policy is ArbitrationPolicy.ROUND_ROBIN:
        lines.extend((
            "      if (zlang_grant_complete) begin",
            f"        zlang_next_priority <= (zlang_selected == {owner_width}'d{count - 1}) "
            "? '0 : zlang_selected + 1'b1;",
            "      end",
        ))
    lines.extend(("    end", "  end"))
    return _module(module, ports, lines)


def _emit_csr(module: Module, *, expose_internal_abi: bool = False) -> str:
    if module.clock is None or module.reset is None or len(module.csr_blocks) != 1:
        raise SystemVerilogEmissionError(
            "direct CSR emission requires one block and one clock domain"
        )
    block = module.csr_blocks[0]
    access = module.csr_access
    if access is None:
        raise SystemVerilogEmissionError("typed CSR access interface is missing")
    if any(port.protocol is not InterfaceProtocol.WIRE for port in module.ports):
        raise SystemVerilogEmissionError(
            "CSR composition currently supports ordinary scalar wire ports only"
        )
    internal_names = ir_csr.csr_internal_port_names(access, module.csr_blocks)
    user_ports = tuple(
        port for port in module.ports if port.name not in internal_names
    )
    internal_ports = []
    if expose_internal_abi:
        internal_ports = [
            *(
                _logic_port("output", ir_csr.csr_state_port_name(binding),
                            binding.canonical_type)
                for binding in block.state_bindings
            ),
            *(
                _logic_port("output", ir_csr.csr_write_hit_port_name(binding),
                            BitType())
                for binding in block.state_bindings
            ),
            *(
                _logic_port("output", ir_csr.csr_write_value_port_name(binding),
                            binding.canonical_type)
                for binding in block.state_bindings
            ),
        ]
    ports = [
        f"input logic {module.clock}",
        f"input logic {module.reset}",
        *(_port_declaration(port) for port in user_ports),
        *(_logic_port("input", name, type_)
          for name, type_ in access.input_types),
        *(_logic_port("output", name, type_)
          for name, type_ in access.output_types),
        *internal_ports,
    ]
    stored = tuple(
        (register, field)
        for register in block.registers
        for field in register.fields
        if field.access
        in {
            ir_csr.CsrAccess.READ_WRITE,
            ir_csr.CsrAccess.WRITE_ONLY,
            ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR,
            ir_csr.CsrAccess.PULSE,
        }
    )
    lines = [
        *(
            f"  logic {_range(field.width)}{_csr_field_name(block, register, field)};"
            for register, field in stored
        ),
        f"  always_ff @({_clock_event(module, _identifier)}) begin",
        f"    if ({_reset_asserted(module, _identifier)}) begin",
        *(
            f"      {_csr_field_name(block, register, field)} <= "
            f"{field.width}'d{field.reset};"
            for register, field in stored
        ),
        "    end else begin",
    ]
    for register, field in stored:
        address = block.base_address + register.offset
        name = _csr_field_name(block, register, field)
        hit = (
            f"({access.write_port} && {access.address_port} == "
            f"32'h{address:08x})"
        )
        incoming = _slice(access.write_data_port, field.msb, field.lsb)
        binding = field.binding
        if (
            field.access is ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR
            and binding is not None
            and binding.kind is ir_csr.CsrBindingKind.STICKY
        ):
            hardware = _identifier(binding.signal)
            if binding.priority is ir_csr.CsrPriority.SOFTWARE:
                update = (
                    f"(({name} | {hardware}) & "
                    f"~({hit} ? {incoming} : '0))"
                )
            else:
                update = (
                    f"(({name} & ~({hit} ? {incoming} : '0)) | {hardware})"
                )
            lines.append(f"      {name} <= {update};")
        elif field.access is ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR:
            lines.append(
                f"      if ({hit}) {name} <= {name} & ~{incoming};"
            )
        elif field.access is ir_csr.CsrAccess.PULSE:
            lines.append(f"      {name} <= {hit} ? {incoming} : '0;")
        else:
            lines.append(f"      if ({hit}) {name} <= {incoming};")
    lines.extend((
        "    end", "  end", "  always_comb begin",
        f"    {access.read_data_port} = 32'b0;",
    ))
    lines.append(f"    if ({access.read_port}) begin")
    lines.append(f"      case ({access.address_port})")
    for register in block.registers:
        address = block.base_address + register.offset
        lines.append(f"        32'h{address:08x}: begin")
        for field in register.fields:
            if field.access not in {
                ir_csr.CsrAccess.READ_WRITE,
                ir_csr.CsrAccess.READ_ONLY,
                ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR,
            }:
                continue
            if (
                field.access is ir_csr.CsrAccess.READ_ONLY
                and field.binding is not None
                and field.binding.kind is ir_csr.CsrBindingKind.STATUS
            ):
                value = _identifier(field.binding.signal)
            elif field.access is ir_csr.CsrAccess.READ_ONLY:
                value = f"{field.width}'d{field.reset}"
            else:
                value = _csr_field_name(block, register, field)
            lines.append(
                f"          {access.read_data_port}[{field.msb}:{field.lsb}] = {value};"
            )
        lines.append("        end")
    lines.extend((
        f"        default: {access.read_data_port} = 32'b0;",
        "      endcase", "    end",
    ))
    addresses = " || ".join(
        f"addr == 32'h{block.base_address + register.offset:08x}"
        for register in block.registers
    )
    lines.extend(
        (
            f"    {access.ready_port} = ({access.read_port} || {access.write_port}) && ({addresses});",
            "  end",
        )
    )
    for register, field in stored:
        if (
            field.binding is not None
            and field.binding.kind is ir_csr.CsrBindingKind.COMMAND
        ):
            lines.append(
                f"  assign {_identifier(field.binding.signal)} = "
                f"{_csr_field_name(block, register, field)};"
            )
    fields_by_identity = {
        field.identity: (register, field)
        for register in block.registers
        for field in register.fields
    }
    for binding in block.state_bindings if expose_internal_abi else ():
        register, field = fields_by_identity[binding.csr_field_id]
        address = block.base_address + register.offset
        base = ir_csr.csr_state_port_name(binding)
        lines.append(
            f"  assign {base} = "
            f"{_csr_field_name(block, register, field)};"
        )
        lines.append(
            f"  assign {ir_csr.csr_write_hit_port_name(binding)} = {access.write_port} && "
            f"{access.address_port} == 32'h{address:08x};"
        )
        lines.append(
            f"  assign {ir_csr.csr_write_value_port_name(binding)} = "
            f"{_slice(access.write_data_port, field.msb, field.lsb)};"
        )
    has_user_transition = bool(
        module.registers or module.next_assignments or module.rules
    )
    has_user_assignments = bool(module.assignments)
    if has_user_transition or has_user_assignments:
        materialized_declarations, materialized_assignments, render = (
            _materialized_emission(module)
        )
        composed_logic = list(materialized_assignments)
        if has_user_transition:
            _append_unified_state(
                module, materialized_declarations, composed_logic, render
            )
        else:
            composed_logic.extend(
                f"  assign {_identifier(_assignment_name(assignment))} = "
                f"{render(assignment.expression)};"
                for assignment in module.assignments
            )
        # Declarations must precede both the CSR and resolved-transition logic;
        # the two state contributors own disjoint typed identities and share
        # only the module clock/reset boundary.
        lines = [*materialized_declarations, *lines, *composed_logic]
    return _module(module, ports, lines)


def _emit_request_response(module: Module) -> str:
    if (
        module.clock is None
        or module.reset is None
        or len(module.request_responses) != 1
    ):
        raise SystemVerilogEmissionError(
            "direct request/response emission requires one clocked interface"
        )
    interface = module.request_responses[0]
    if interface.ordering is RequestResponseOrdering.IN_ORDER:
        return _emit_in_order_request_response(module, interface)
    if module.registers or module.next_assignments or module.rules:
        raise SystemVerilogEmissionError(
            "standalone out-of-order request/response with ordinary user state "
            "is not implemented by the direct-SystemVerilog endpoint emitter",
            semantic_path=(module.name, interface.name),
            code="ZL-BACKEND-SYSTEMVERILOG-REQUEST-RESPONSE-STATE",
            notes=(
                "emission stopped before publishing an artifact so the typed "
                "state transition cannot be omitted",
            ),
        )
    if interface.role is RequestResponseRole.RESPONDER:
        raise SystemVerilogEmissionError(
            "standalone out-of-order responder emission is not implemented"
        )
    if interface.match_by is None or interface.id_type is None:
        raise SystemVerilogEmissionError("out-of-order interface lacks an ID field")
    request_payload = _find_channel_assignment(
        module, interface.name, RequestResponseChannel.REQUEST,
        ReadyValidSignal.PAYLOAD,
    )
    request_valid = _find_channel_assignment(
        module, interface.name, RequestResponseChannel.REQUEST,
        ReadyValidSignal.VALID,
    )
    response_ready = _find_channel_assignment(
        module, interface.name, RequestResponseChannel.RESPONSE,
        ReadyValidSignal.READY,
    )
    response_output = next(
        assignment for assignment in module.assignments
        if isinstance(assignment.target, Port)
        and isinstance(assignment.expression, expr.RequestResponseRef)
    )
    name = interface.name
    count_width = max(1, interface.max_outstanding.bit_length())
    id_width = _width(interface.id_type)
    request_payload_value = _expression(request_payload.expression)
    request_id = _struct_field_expression(
        request_payload_value, interface.request_type, interface.match_by
    )
    response_id = _struct_field_expression(
        f"{name}_response_payload", interface.response_type, interface.match_by
    )
    ports = [
        f"input logic {module.clock}",
        f"input logic {module.reset}",
        *(_port_declaration(port) for port in module.inputs),
        f"input logic {name}_request_ready",
        _logic_port("input", f"{name}_response_payload", interface.response_type),
        f"input logic {name}_response_valid",
        *(_port_declaration(port) for port in module.outputs),
        _logic_port("output", f"{name}_request_payload", interface.request_type),
        f"output logic {name}_request_valid",
        f"output logic {name}_response_ready",
    ]
    max_count = interface.max_outstanding
    lines = [
        f"  logic [{count_width - 1}:0] {name}_outstanding;",
        f"  logic [{id_width - 1}:0] {name}_ids [0:{max_count - 1}];",
        f"  logic [{id_width - 1}:0] {name}_ids_next [0:{max_count - 1}];",
        f"  logic [{max_count - 1}:0] {name}_ids_valid, {name}_ids_valid_next;",
        f"  logic {name}_duplicate, {name}_missing;",
        f"  logic {name}_request_id_present, {name}_response_id_present;",
        f"  logic {name}_request_transfer, {name}_response_transfer;",
        "  integer zlang_scan;",
        "  integer zlang_next;",
        "  integer zlang_state;",
        "  logic zlang_inserted;",
        f"  assign {name}_request_payload = {request_payload_value};",
        f"  assign {response_output.target.name} = {name}_response_payload;",
        "  always_comb begin",
        f"    {name}_request_id_present = 1'b0;",
        f"    {name}_response_id_present = 1'b0;",
        f"    for (zlang_scan = 0; zlang_scan < {max_count}; zlang_scan = zlang_scan + 1) begin",
        f"      if ({name}_ids_valid[zlang_scan] && {name}_ids[zlang_scan] == {request_id}) "
        f"{name}_request_id_present = 1'b1;",
        f"      if ({name}_ids_valid[zlang_scan] && {name}_ids[zlang_scan] == {response_id}) "
        f"{name}_response_id_present = 1'b1;",
        "    end",
        f"    {name}_duplicate = {_reset_deasserted(module, _identifier)} && "
        f"{_expression(request_valid.expression)} && {name}_request_ready && "
        f"({name}_outstanding < {count_width}'d{max_count}) && "
        f"{name}_request_id_present;",
        f"    {name}_missing = {_reset_deasserted(module, _identifier)} && "
        f"{_expression(response_ready.expression)} && {name}_response_valid && "
        f"({name}_outstanding != '0) && !{name}_response_id_present;",
        "  end",
        f"  assign {name}_request_valid = {_reset_deasserted(module, _identifier)} && "
        f"({name}_outstanding < {count_width}'d{max_count}) && !{name}_duplicate "
        f"&& {_expression(request_valid.expression)};",
        f"  assign {name}_response_ready = {_reset_deasserted(module, _identifier)} && "
        f"({name}_outstanding != '0) && !{name}_missing "
        f"&& {_expression(response_ready.expression)};",
        f"  assign {name}_request_transfer = {name}_request_valid && "
        f"{name}_request_ready;",
        f"  assign {name}_response_transfer = {name}_response_valid && "
        f"{name}_response_ready;",
        "  always_comb begin",
        f"    {name}_ids_valid_next = {name}_ids_valid;",
        f"    for (zlang_next = 0; zlang_next < {max_count}; zlang_next = zlang_next + 1) "
        f"{name}_ids_next[zlang_next] = {name}_ids[zlang_next];",
        f"    if ({name}_response_transfer) begin",
        f"      for (zlang_next = 0; zlang_next < {max_count}; zlang_next = zlang_next + 1) begin",
        f"        if ({name}_ids_valid_next[zlang_next] && "
        f"{name}_ids_next[zlang_next] == {response_id}) "
        f"{name}_ids_valid_next[zlang_next] = 1'b0;",
        "      end",
        "    end",
        "    zlang_inserted = 1'b0;",
        f"    if ({name}_request_transfer) begin",
        f"      for (zlang_next = 0; zlang_next < {max_count}; zlang_next = zlang_next + 1) begin",
        f"        if (!zlang_inserted && !{name}_ids_valid_next[zlang_next]) begin",
        f"          {name}_ids_next[zlang_next] = {request_id};",
        f"          {name}_ids_valid_next[zlang_next] = 1'b1;",
        "          zlang_inserted = 1'b1;",
        "        end",
        "      end",
        "    end",
        "  end",
        f"  always_ff @({_clock_event(module, _identifier)}) begin",
        f"    if ({_reset_asserted(module, _identifier)}) begin",
        f"      {name}_outstanding <= '0;",
        f"      {name}_ids_valid <= '0;",
        f"      for (zlang_state = 0; zlang_state < {max_count}; zlang_state = zlang_state + 1) "
        f"{name}_ids[zlang_state] <= '0;",
        "    end else begin",
        f"      case ({{{name}_request_transfer, {name}_response_transfer}})",
        f"        2'b10: if ({name}_outstanding < {count_width}'d{max_count}) "
        f"{name}_outstanding <= {name}_outstanding + 1'b1;",
        f"        2'b01: if ({name}_outstanding != '0) "
        f"{name}_outstanding <= {name}_outstanding - 1'b1;",
        f"        default: {name}_outstanding <= {name}_outstanding;",
        "      endcase",
        f"      {name}_ids_valid <= {name}_ids_valid_next;",
        f"      for (zlang_state = 0; zlang_state < {max_count}; zlang_state = zlang_state + 1) "
        f"{name}_ids[zlang_state] <= {name}_ids_next[zlang_state];",
        "    end",
        "  end",
    ]
    return _module(module, ports, lines)


def _emit_in_order_request_response(
    module: Module,
    interface: object,
) -> str:
    """Emit one role-qualified, in-order standalone endpoint.

    The endpoint's role is already frozen in semantic IR.  This emitter owns
    only the local unbuffered transaction ledger: a physical request transfer
    increments it and a physical response transfer decrements it.  A
    same-cycle response is legal when the request also transfers in that cycle,
    matching the existing hierarchical in-order accounting semantics.
    """

    if any(port.protocol is not InterfaceProtocol.WIRE for port in module.ports):
        raise SystemVerilogEmissionError(
            "mixing request/response with other protocols is not implemented"
        )

    name = _identifier(interface.name)
    requester = interface.role is RequestResponseRole.REQUESTER
    count_width = max(1, interface.max_outstanding.bit_length())
    maximum = interface.max_outstanding
    has_user_transition = bool(
        module.registers or module.next_assignments or module.rules
    )
    if has_user_transition:
        if module.resolved_transition is None:
            raise SystemVerilogEmissionError(
                "stateful request/response emission requires resolved transition IR"
            )
        stage_declarations, stage_logic, stage_render = (
            _embedded_staging_emission(module)
        )
        if stage_declarations:
            declarations = list(stage_declarations)
            materialized = list(stage_logic)
            render = stage_render
        else:
            materialized_declarations, materialized_assignments, render = (
                _materialized_emission(module)
            )
            declarations = list(materialized_declarations)
            materialized = list(materialized_assignments)
    else:
        materialized_declarations, materialized_assignments, render = (
            _materialized_emission(module)
        )
        declarations = list(materialized_declarations)
        materialized = list(materialized_assignments)

    rr_declarations = [
        f"  logic [{count_width - 1}:0] {name}_outstanding;",
        f"  logic {name}_request_transfer, {name}_response_transfer;",
    ]
    rr_logic: list[str] = []
    owned_assignment_names: set[str] = set()

    def owned(
        channel: RequestResponseChannel,
        signal: ReadyValidSignal,
    ) -> str:
        assignment = _find_channel_assignment(
            module, interface.name, channel, signal
        )
        owned_assignment_names.add(_assignment_name(assignment))
        return render(assignment.expression)

    if requester:
        request_payload = owned(
            RequestResponseChannel.REQUEST, ReadyValidSignal.PAYLOAD
        )
        request_valid = owned(
            RequestResponseChannel.REQUEST, ReadyValidSignal.VALID
        )
        response_ready = owned(
            RequestResponseChannel.RESPONSE, ReadyValidSignal.READY
        )
        rr_logic.extend((
            f"  assign {name}_request_payload = {request_payload};",
            f"  assign {name}_request_valid = "
            f"{_reset_deasserted(module, _identifier)} && "
            f"({name}_outstanding < {count_width}'d{maximum}) && "
            f"({request_valid});",
            f"  assign {name}_request_transfer = "
            f"{name}_request_valid && {name}_request_ready;",
            f"  assign {name}_response_ready = "
            f"{_reset_deasserted(module, _identifier)} && "
            f"({name}_outstanding != '0 || {name}_request_transfer) && "
            f"({response_ready});",
            f"  assign {name}_response_transfer = "
            f"{name}_response_valid && {name}_response_ready;",
        ))
    else:
        request_ready = owned(
            RequestResponseChannel.REQUEST, ReadyValidSignal.READY
        )
        response_payload = owned(
            RequestResponseChannel.RESPONSE, ReadyValidSignal.PAYLOAD
        )
        response_valid = owned(
            RequestResponseChannel.RESPONSE, ReadyValidSignal.VALID
        )
        rr_logic.extend((
            f"  assign {name}_request_ready = "
            f"{_reset_deasserted(module, _identifier)} && "
            f"({name}_outstanding < {count_width}'d{maximum}) && "
            f"({request_ready});",
            f"  assign {name}_request_transfer = "
            f"{name}_request_valid && {name}_request_ready;",
            f"  assign {name}_response_payload = {response_payload};",
            f"  assign {name}_response_valid = "
            f"{_reset_deasserted(module, _identifier)} && "
            f"({name}_outstanding != '0 || {name}_request_transfer) && "
            f"({response_valid});",
            f"  assign {name}_response_transfer = "
            f"{name}_response_valid && {name}_response_ready;",
        ))

    rr_logic.extend((
        f"  always_ff @({_clock_event(module, _identifier)}) begin",
        f"    if ({_reset_asserted(module, _identifier)}) "
        f"{name}_outstanding <= '0;",
        "    else begin",
        f"      case ({{{name}_request_transfer, {name}_response_transfer}})",
        f"        2'b10: if ({name}_outstanding < "
        f"{count_width}'d{maximum}) {name}_outstanding <= "
        f"{name}_outstanding + 1'b1;",
        f"        2'b01: if ({name}_outstanding != '0) "
        f"{name}_outstanding <= {name}_outstanding - 1'b1;",
        f"        default: {name}_outstanding <= {name}_outstanding;",
        "      endcase",
        "    end",
        "  end",
    ))

    if has_user_transition:
        state_logic = list(materialized)
        _append_unified_state(
            module,
            declarations,
            state_logic,
            render,
            excluded_assignment_names=frozenset(owned_assignment_names),
        )
        lines = [
            *declarations,
            *rr_declarations,
            *state_logic,
            *rr_logic,
        ]
    else:
        lines = [
            *declarations,
            *materialized,
            *rr_declarations,
            *rr_logic,
        ]
        for assignment in module.assignments:
            if not isinstance(assignment.target, Port):
                continue
            lines.append(
                f"  assign {_identifier(assignment.target.name)} = "
                f"{render(assignment.expression)};"
            )
    return _module(module, _physical_port_declarations(module), lines)


def _expression(expression: expr.Expression) -> str:
    if isinstance(expression, (expr.InputRef, expr.ParameterRef, expr.RegisterRef)):
        return _identifier(expression.name)
    if isinstance(expression, expr.InstanceOutputRef):
        raise SystemVerilogEmissionError(
            "instance output requires its containing module naming plan",
            code="ZL-BACKEND-SYSTEMVERILOG-NAMING-CONTEXT",
            semantic_path=(expression.instance, expression.port),
            primary=expression.origin,
        )
    if isinstance(expression, expr.ReadyValidRef):
        prefix = _identifier(expression.interface)
        if expression.signal is ReadyValidSignal.TRANSFER:
            return f"({prefix}_valid && {prefix}_ready)"
        return f"{prefix}_{expression.signal.value}"
    if isinstance(expression, expr.CreditRef):
        signal = (
            CreditSignal.SEND
            if expression.signal is CreditSignal.TRANSFER
            else expression.signal
        )
        return f"{_identifier(expression.interface)}_{signal.value}"
    if isinstance(expression, expr.RequestResponseRef):
        prefix = f"{_identifier(expression.interface)}_{expression.channel.value}"
        if expression.signal is ReadyValidSignal.TRANSFER:
            return f"({prefix}_valid && {prefix}_ready)"
        return (
            f"{prefix}_"
            f"{expression.signal.value}"
        )
    if isinstance(expression, expr.MemoryRef):
        return f"{_identifier(expression.memory)}_{expression.signal.value}"
    if isinstance(expression, expr.RomRef):
        if expression.signal is not RomSignal.READ_DATA:
            raise SystemVerilogEmissionError(
                "ROM read_address is a driven storage input, not a readable value"
            )
        return f"{_identifier(expression.rom)}_read_data"
    if isinstance(expression, expr.FifoRef):
        return f"{_identifier(expression.fifo)}_{expression.signal.value}"
    if isinstance(expression, expr.Constant):
        return _sized_decimal(
            _width(expression.type),
            expression.value,
            signed=isinstance(expression.type, (SIntType, FixedType)),
        )
    if isinstance(expression, expr.EnumEncode):
        width = expression.type.width
        return f"{width}'($unsigned({_expression(expression.expression)}))"
    if isinstance(expression, expr.EnumValid):
        value = _expression(expression.expression)
        width = expression.expression.type.width
        return "(" + " || ".join(
            f"(({value}) == {width}'d{code})"
            for code in expression.enum_type.codes
        ) + ")"
    if isinstance(expression, expr.EnumDecode):
        value = _expression(expression.expression)
        width = expression.type.width
        valid = " || ".join(
            f"(({value}) == {width}'d{code})"
            for code in expression.type.codes
        )
        fallback = _expression(expression.fallback)
        return f"(({valid}) ? {width}'($unsigned({value})) : ({fallback}))"
    if isinstance(expression, expr.UnionConstruct):
        union_type = expression.type
        tag = _sized_decimal(
            union_type.tag_width,
            union_type.tag(expression.variant),
            signed=False,
        )
        parts = [tag, *(_expression(value) for _, value in expression.fields)]
        variant = union_type.variant(expression.variant)
        assert variant is not None
        padding = union_type.payload_width - variant.payload_width
        if padding:
            parts.append(_sized_decimal(padding, 0, signed=False))
        return "{" + ", ".join(parts) + "}"
    if isinstance(expression, expr.UnionTag):
        union_type = expression.expression.type
        assert isinstance(union_type, TaggedUnionType)
        return _slice(
            _expression(expression.expression),
            union_type.width - 1,
            union_type.payload_width,
        )
    if isinstance(expression, expr.UnionField):
        union_type = expression.expression.type
        assert isinstance(union_type, TaggedUnionType)
        variant = union_type.variant(expression.variant)
        assert variant is not None
        offset = union_type.payload_width
        for field in variant.fields:
            msb, lsb = offset - 1, offset - field.type.width
            if field.name == expression.field:
                return _slice(_expression(expression.expression), msb, lsb)
            offset = lsb
        raise SystemVerilogEmissionError("invalid tagged-union field projection")
    if isinstance(expression, expr.Add):
        width = _width(expression.type)
        return (
            f"({_resize(expression.left, width)} + "
            f"{_resize(expression.right, width)})"
        )
    if isinstance(expression, expr.Binary):
        width = _width(expression.operand_type)
        left = _resize(expression.left, width)
        right = _resize(expression.right, width)
        if expression.operator is expr.BinaryOperator.SHIFT_RIGHT:
            return render_right_shift(
                left,
                right,
                signed=isinstance(expression.operand_type, SIntType),
            )
        operator = {
            expr.BinaryOperator.SUBTRACT: "-",
            expr.BinaryOperator.MULTIPLY: "*",
            expr.BinaryOperator.BIT_AND: "&",
            expr.BinaryOperator.BIT_OR: "|",
            expr.BinaryOperator.BIT_XOR: "^",
            expr.BinaryOperator.SHIFT_LEFT: "<<",
            expr.BinaryOperator.EQUAL: "==",
            expr.BinaryOperator.NOT_EQUAL: "!=",
            expr.BinaryOperator.LESS: "<",
            expr.BinaryOperator.LESS_EQUAL: "<=",
            expr.BinaryOperator.GREATER: ">",
            expr.BinaryOperator.GREATER_EQUAL: ">=",
        }[expression.operator]
        if expression.operator in {
            expr.BinaryOperator.LESS,
            expr.BinaryOperator.LESS_EQUAL,
            expr.BinaryOperator.GREATER,
            expr.BinaryOperator.GREATER_EQUAL,
        }:
            return render_ordered_comparison(
                left,
                operator,
                right,
                signed=isinstance(expression.operand_type, (SIntType, FixedType)),
            )
        return f"({left} {operator} {right})"
    if isinstance(expression, expr.Extend):
        return _resize(expression.expression, _width(expression.type))
    if isinstance(expression, expr.Truncate):
        width = _width(expression.type)
        return f"{width}'({_expression(expression.expression)})"
    if isinstance(expression, expr.FixedConvert):
        rendered = _expression(expression.expression)
        if expression.kind in {expr.FixedConversionKind.FROM_RAW, expr.FixedConversionKind.TO_RAW}:
            return rendered
        target_signed = isinstance(expression.type, FixedType)
        if expression.rational_denominator is not None:
            assert isinstance(expression.expression, expr.Constant)
            value = quantize_rational(
                expression.expression.value,
                expression.rational_denominator,
                fraction=expression.type.fraction,
                width=expression.type.width,
                signed=target_signed,
                rounding=expression.rounding,
                overflow=expression.overflow,
            )
            return _sized_decimal(
                expression.type.width,
                value,
                signed=target_signed,
            )
        source_fraction = getattr(expression.expression.type, "fraction", 0)
        delta = expression.type.fraction - source_fraction
        source_signed = isinstance(expression.expression.type, (SIntType, FixedType))
        source_width = _width(expression.expression.type)
        work_width = max(
            source_width + max(delta, 0) + 1,
            expression.type.width + 1,
        )
        value = (
            f"$signed({work_width}'($signed({rendered})))"
            if source_signed
            else f"$unsigned({work_width}'($unsigned({rendered})))"
        )
        if delta > 0:
            converted = f"({value} <<< {delta})"
        elif delta == 0:
            converted = value
        else:
            shift = -delta
            literal = lambda value: f"{work_width}'d{value}"
            magnitude = (f"(({value}) < 0 ? -({value}) : ({value}))"
                         if source_signed else f"({value})")
            quotient = f"({magnitude} >> {shift})"
            discarded = (
                f"(({magnitude} & {literal((1 << shift) - 1)}) "
                f"!= {literal(0)})"
            )
            discarded_value = f"{work_width}'({discarded})"
            if expression.rounding is expr.FixedRounding.NEAREST_EVEN:
                rounded = (
                    f"(({magnitude} + {literal((1 << (shift - 1)) - 1)} + "
                    f"(({magnitude} >> {shift}) & {literal(1)})) >> {shift})"
                )
                converted = (
                    f"(({value}) < 0 ? -({rounded}) : ({rounded}))"
                    if source_signed else rounded
                )
            elif expression.rounding is expr.FixedRounding.AWAY_ZERO:
                rounded = f"({quotient} + {discarded_value})"
                converted = (
                    f"(({value}) < 0 ? -({rounded}) : ({rounded}))"
                    if source_signed else rounded
                )
            elif expression.rounding is expr.FixedRounding.FLOOR and source_signed:
                converted = (
                    f"(({value}) < 0 ? "
                    f"-({quotient} + {discarded_value}) : {quotient})"
                )
            else:
                converted = (
                    f"(({value}) < 0 ? -({quotient}) : ({quotient}))"
                    if source_signed else quotient
                )
        if expression.overflow is expr.FixedOverflow.SATURATE:
            minimum = -(1 << (expression.type.width - 1)) if target_signed else 0
            maximum = ((1 << (expression.type.width - 1)) - 1 if target_signed
                       else (1 << expression.type.width) - 1)
            converted = (
                f"$signed({work_width}'({converted}))"
                if target_signed
                else f"$unsigned({work_width}'({converted}))"
            )
            minimum_literal = (
                f"-{work_width}'sd{-minimum}"
                if minimum < 0 else f"{work_width}'d{minimum}"
            )
            maximum_literal = (
                f"{work_width}'sd{maximum}"
                if target_signed else f"{work_width}'d{maximum}"
            )
            converted = (
                f"(({converted}) < {minimum_literal} ? {minimum_literal} : "
                f"(({converted}) > {maximum_literal} ? {maximum_literal} : "
                f"({converted})))"
            )
        sized = f"{expression.type.width}'({converted})"
        return f"$signed({sized})" if target_signed else sized
    if isinstance(expression, expr.Mux):
        return (
            f"({_expression(expression.condition)} ? "
            f"{_expression(expression.when_true)} : "
            f"{_expression(expression.when_false)})"
        )
    if isinstance(expression, expr.Switch):
        rendered = _expression(expression.default)
        selector = _expression(expression.selector)
        selector_width = _width(expression.selector.type)
        for case in reversed(expression.cases):
            rendered = (
                f"(({selector}) == {selector_width}'d{case.key} ? "
                f"{_expression(case.expression)} : {rendered})"
            )
        return rendered
    if isinstance(expression, expr.FieldAccess):
        if not isinstance(expression.expression.type, StructType):
            raise SystemVerilogEmissionError("field access requires a struct")
        return _struct_field_expression(
            _expression(expression.expression),
            expression.expression.type,
            expression.field,
        )
    if isinstance(expression, expr.StructConstruct):
        return "{" + ", ".join(_expression(value) for _, value in expression.fields) + "}"
    if isinstance(expression, expr.TupleConstruct):
        return "{" + ", ".join(
            f"{_width(value.type)}'({_expression(value)})"
            for value in expression.elements
        ) + "}"
    if isinstance(expression, expr.TupleProject):
        tuple_type = expression.expression.type
        if not isinstance(tuple_type, TupleType):
            raise SystemVerilogEmissionError(
                "tuple projection requires a structural tuple"
            )
        lsb = sum(
            _width(item) for item in tuple_type.elements[expression.index + 1:]
        )
        msb = lsb + _width(tuple_type.elements[expression.index]) - 1
        projected = _slice(_expression(expression.expression), msb, lsb)
        if isinstance(expression.type, (SIntType, FixedType)):
            return f"$signed({projected})"
        return projected
    if isinstance(expression, expr.VectorIndex):
        vector = expression.expression.type
        if not isinstance(vector, VecType):
            raise SystemVerilogEmissionError("vector index requires a vector")
        element_width = _width(vector.element_type)
        lsb = (vector.length - expression.index - 1) * element_width
        msb = lsb + element_width - 1
        return _slice(_expression(expression.expression), msb, lsb)
    if isinstance(expression, expr.RuntimeIndex):
        vector = expression.expression.type
        if not isinstance(vector, VecType):
            raise SystemVerilogEmissionError("runtime vector index requires a vector")
        element_width = _width(vector.element_type)
        rendered_vector = _expression(expression.expression)
        rendered_index = _expression(expression.index)
        base = (
            f"((32'd{vector.length - 1} - 32'({rendered_index})) * "
            f"32'd{element_width})"
        )
        return f"{rendered_vector}[{base} +: {element_width}]"
    if isinstance(expression, expr.VectorUpdate):
        vector = expression.expression.type
        if not isinstance(vector, VecType):
            raise SystemVerilogEmissionError("vector update requires a vector")
        total_width = _width(vector)
        element_width = _width(vector.element_type)
        rendered_vector = _expression(expression.expression)
        rendered_index = _expression(expression.index)
        rendered_value = _expression(expression.value)
        base = (
            f"((32'd{vector.length - 1} - 32'({rendered_index})) * "
            f"32'd{element_width})"
        )
        element_mask = f"{total_width}'h{((1 << element_width) - 1):x}"
        cleared = (
            f"({total_width}'($unsigned({rendered_vector})) & "
            f"~({element_mask} << ({base})))"
        )
        inserted = (
            f"(({total_width}'($unsigned({rendered_value})) & {element_mask}) "
            f"<< ({base}))"
        )
        return f"{total_width}'(({cleared}) | ({inserted}))"
    if isinstance(expression, expr.Slice):
        width = _width(expression.type)
        value = _expression(expression.expression)
        return f"{width}'(($unsigned({value})) >> {expression.lsb})"
    if isinstance(expression, expr.Concat):
        if len(expression.operands) < 2:
            raise SystemVerilogEmissionError(
                "typed concat requires at least two operands"
            )
        return "{" + ", ".join(
            f"{_width(operand.type)}'({_expression(operand)})"
            for operand in expression.operands
        ) + "}"
    if isinstance(expression, expr.VectorConcat):
        if len(expression.operands) < 2:
            raise SystemVerilogEmissionError(
                "typed vector concat requires at least two operands"
            )
        return "{" + ", ".join(
            f"{_width(operand.type)}'({_expression(operand)})"
            for operand in expression.operands
        ) + "}"
    if isinstance(expression, expr.Reshape):
        width = _width(expression.type)
        return f"{width}'($unsigned({_expression(expression.expression)}))"
    if isinstance(expression, expr.Bitcast):
        width = _width(expression.type)
        raw = f"{width}'($unsigned({_expression(expression.expression)}))"
        if isinstance(expression.type, (SIntType, FixedType)):
            return f"$signed({raw})"
        return raw
    if isinstance(expression, expr.Pack):
        width = _width(expression.type)
        return f"{width}'($unsigned({_expression(expression.expression)}))"
    if isinstance(expression, expr.Unpack):
        width = _width(expression.type)
        raw = f"{width}'($unsigned({_expression(expression.expression)}))"
        if isinstance(expression.type, (SIntType, FixedType)):
            return f"$signed({raw})"
        return raw
    if isinstance(expression, expr.Dot):
        return _balanced_expression(expression.products)
    if isinstance(expression, expr.Reduce):
        return _expression(lower_reduction(expression))
    if isinstance(expression, expr.FunctionalRegion):
        if not isinstance(expression.type, VecType):
            raise SystemVerilogEmissionError(
                "functional region result must be a vector"
            )
        return "{" + ", ".join(
            _expression(element)
            for element in materialize_functional_region(expression)
        ) + "}"
    if isinstance(expression, (expr.Generate, expr.Map)):
        if isinstance(expression.type, VecType):
            # Generate/map are semantically unrolled vectors by this stage;
            # preserve their packed element structure instead of treating the
            # vector as a reduction.  The element order matches the canonical
            # Vec layout used by VectorIndex and the Clash emitter.
            return "{" + ", ".join(
                _expression(element) for element in expression.elements
            ) + "}"
        return _balanced_expression(expression.elements)
    if isinstance(expression, expr.Call):
        arguments = ", ".join(_expression(item) for item in expression.arguments)
        return f"{_identifier(expression.function)}({arguments})"
    if isinstance(expression, expr.ImplementationChoice):
        return _expression(expression.selected_alternative.expression)
    if isinstance(expression, (expr.Delay, expr.Pipeline)):
        raise SystemVerilogEmissionError(
            "nested delay/pipeline expressions are outside the direct experiment"
        )
    raise SystemVerilogEmissionError(
        f"unsupported direct SystemVerilog expression {type(expression).__name__}"
    )


def _balanced_expression(elements: tuple[expr.Expression, ...]) -> str:
    if not elements:
        raise SystemVerilogEmissionError("direct reduction cannot be empty")
    if len(elements) == 1:
        return _expression(elements[0])
    middle = len(elements) // 2
    return f"({_balanced_expression(elements[:middle])} + {_balanced_expression(elements[middle:])})"


def _resize(expression: expr.Expression, width: int) -> str:
    return render_typed_resize(
        _expression(expression),
        source_width=_width(expression.type),
        target_width=width,
        signed=isinstance(expression.type, (SIntType, FixedType)),
    )


def _typed_functions(module: Module) -> tuple[object, ...]:
    """Return the local executable callable closure exactly once."""

    try:
        return reachable_module_callables(module)
    except CallableReachabilityError as error:
        raise SystemVerilogEmissionError(str(error)) from error


def _function_definitions(module: Module) -> list[str]:
    definitions: list[str] = []
    functions = _typed_functions(module)
    function_names = {_identifier(function.name) for function in functions}
    outer_names = {
        _identifier(item.name)
        for item in (*module.ports, *module.registers, *module.locals)
    }
    for function in functions:
        name = _identifier(function.name)
        return_signed = (
            " signed"
            if isinstance(function.return_type, (SIntType, FixedType))
            else ""
        )
        parameters = []
        # Preserve the source stem without repeating the enclosing helper.
        # A short private prefix excludes ALL bare SV keywords (including ones
        # not covered by the frozen public-port escaping policy).
        used = function_names | outer_names
        parameter_names = {}
        for parameter in sorted(function.parameters, key=lambda item: item.name):
            parameter_names[parameter.name] = allocate_private_rtl_identifier(
                f"arg_{parameter.name}",
                semantic_identity=f"{function.callee_identity}:parameter:{parameter.name}",
                used=used,
            )
        for parameter in function.parameters:
            signed = (
                " signed"
                if isinstance(parameter.type, (SIntType, FixedType))
                else ""
            )
            parameters.append(
                f"input logic{signed} {_range(_width(parameter.type))}"
                f"{parameter_names[parameter.name]}"
            )
        declaration = ",\n    ".join(parameters)
        call_identity = getattr(function, "callee_identity", "")
        identity_note = (
            f"  // ZLang callable identity: {call_identity}\n"
            if call_identity
            else ""
        )
        renamed_body = _rename_parameter_refs(function.body, parameter_names)
        materialized = plan_materialization(
            (renamed_body,),
            reserved_names=(*parameter_names.values(), name),
            generated_prefix="zlang_fn_expr_",
        )
        aliases = {item.expression: item.name for item in materialized}

        # Function statements execute procedurally, so exact typed dependencies
        # must be assigned before the expression that consumes them.  Global
        # first-discovery order is deterministic but is not topological when a
        # shared child was first seen through another root.  Module-level
        # materialization uses continuous assigns and does not need this step.
        function_locals: list[str] = []
        function_assignments: list[str] = []
        for item in dependency_ordered_materialization(materialized):
            signed = (
                " signed"
                if isinstance(item.expression.type, (SIntType, FixedType))
                else ""
            )
            function_locals.append(
                f"    logic{signed} {_range(_width(item.expression.type))}{item.name};"
            )
            rewritten = _replace_materialized(
                item.expression,
                aliases,
                keep=item.expression,
            )
            function_assignments.append(
                f"    {item.name} = {_expression(rewritten)};"
            )
        rewritten_body = _replace_materialized(renamed_body, aliases)
        local_block = "\n".join((*function_locals, *function_assignments))
        if local_block:
            local_block += "\n"
        definitions.append(
            f"  function automatic logic{return_signed} "
            f"{_range(_width(function.return_type))}{name}("
            f"{declaration});\n"
            f"{identity_note}"
            f"{local_block}"
            f"    {name} = {_expression(rewritten_body)};\n"
            "  endfunction"
        )
    return definitions


def _rename_parameter_refs(
    value: object,
    names: dict[str, str],
) -> object:
    """Give helper parameters deterministic backend-private identifiers.

    SystemVerilog functions live inside the generated module.  Reusing a
    source parameter such as ``value`` can therefore hide the top-level port
    with the same name and turns strict Verilator lint into a failure.  Rename
    only typed ``ParameterRef`` leaves; semantic identities and call-site ABI
    remain unchanged.
    """

    if isinstance(value, expr.ParameterRef):
        name = names.get(value.name)
        return replace(value, name=name) if name is not None else value
    if isinstance(value, tuple):
        return tuple(_rename_parameter_refs(item, names) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        updates = {
            item.name: _rename_parameter_refs(getattr(value, item.name), names)
            for item in fields(value)
            if item.init and item.name not in {"type", "origin"}
        }
        return replace(value, **updates) if updates else value
    return value


def _module(module: Module, ports: list[str], lines: list[str]) -> str:
    declarations = ",\n".join(f"  {port}" for port in ports)
    body = "\n".join(
        line
        for line in (
            *_function_definitions(module),
            *_reset_conditioner_lines(module, _identifier),
            *lines,
        )
        if line
    )
    return (
        f"module {_identifier(module.name)} (\n{declarations}\n);\n"
        f"  // Generated from backend-independent typed ZLang IR.\n"
        f"{body}\nendmodule\n"
    )


def _identifier(name: str) -> str:
    return rtl_identifier(name)


def _instance_identifier(name: str) -> str:
    """Compatibility wrapper around the shared physical-name projection."""

    return rtl_instance_identifier(name)


def _port_declaration(port: Port) -> str:
    direction = "input" if port.direction is PortDirection.INPUT else "output"
    return _logic_port(direction, _identifier(port.name), port.type)


def _logic_port(direction: str, name: str, type_: HardwareType) -> str:
    signed = " signed" if isinstance(type_, (SIntType, FixedType)) else ""
    net = " wire" if direction == "input" else ""
    return f"{direction}{net} logic{signed} {_range(_width(type_))}{name}"


def _logic_declaration(name: str, type_: HardwareType) -> str:
    """Declare one internal signal with its exact typed RTL signedness."""

    signed = " signed" if isinstance(type_, (SIntType, FixedType)) else ""
    return f"  logic{signed} {_range(_width(type_))}{name};"


def _range(width: int) -> str:
    return "" if width == 1 else f"[{width - 1}:0] "


def _width(type_: HardwareType) -> int:
    if isinstance(type_, (BitType, BitsType, UIntType, SIntType, FixedType, UFixedType, EnumType, TaggedUnionType)):
        return type_.width
    if isinstance(type_, VecType):
        return type_.length * _width(type_.element_type)
    if isinstance(type_, StructType):
        return sum(_width(field.type) for field in type_.fields)
    if isinstance(type_, TupleType):
        return sum(_width(element) for element in type_.elements)
    raise SystemVerilogEmissionError(f"no packed width for {type_}")


def _slice(value: str, msb: int, lsb: int) -> str:
    # A select may be applied directly to a named packed value, but constructs
    # such as a sized cast are not legal select bases in all supported
    # SystemVerilog front ends (for example ``7'($unsigned(x))[3]``).  Preserve
    # the same raw-bit semantics for a compound expression with an explicitly
    # sized logical shift instead of relying on parser-specific postfix-select
    # acceptance.
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        return f"{value}[{msb}]" if msb == lsb else f"{value}[{msb}:{lsb}]"
    width = msb - lsb + 1
    return f"{width}'(($unsigned({value})) >> {lsb})"


def _struct_field_expression(value: str, type_: HardwareType, field: str) -> str:
    if not isinstance(type_, StructType):
        raise SystemVerilogEmissionError("ID matching requires a struct payload")
    offset = _width(type_)
    for item in type_.fields:
        width = _width(item.type)
        msb = offset - 1
        lsb = offset - width
        if item.name == field:
            return _slice(value, msb, lsb)
        offset -= width
    raise SystemVerilogEmissionError(
        f"struct '{type_.name}' has no field '{field}'"
    )


def _assignment_name(assignment: object) -> str:
    target = assignment.target
    signal = assignment.signal
    base = _identifier(target.name)
    if assignment.channel is not None:
        return f"{base}_{assignment.channel.value}_{signal.value}"
    return base if signal is None else f"{base}_{signal.value}"


def _find_signal_assignment(
    module: Module, endpoint: str, signal: object
):
    matches = tuple(
        assignment for assignment in module.assignments
        if assignment.target.name == endpoint and assignment.signal is signal
    )
    if len(matches) != 1:
        raise SystemVerilogEmissionError(
            f"endpoint '{endpoint}' requires one '{signal.value}' assignment"
        )
    return matches[0]


def _find_channel_assignment(
    module: Module,
    interface: str,
    channel: RequestResponseChannel,
    signal: ReadyValidSignal,
):
    matches = tuple(
        assignment for assignment in module.assignments
        if assignment.target.name == interface
        and assignment.channel is channel
        and assignment.signal is signal
    )
    if len(matches) != 1:
        raise SystemVerilogEmissionError(
            f"interface '{interface}.{channel.value}' requires one "
            f"'{signal.value}' assignment"
        )
    return matches[0]


def _csr_field_name(
    block: ir_csr.CsrBlock,
    register: ir_csr.CsrRegister,
    field: ir_csr.CsrField,
) -> str:
    return f"csr_{block.name}_{register.name.lower()}_{field.name}"


def _ordered_rules(module: Module) -> tuple[Rule, ...]:
    remaining = list(module.rules)
    edges = {(priority.higher, priority.lower) for priority in module.rule_priorities}
    ordered: list[Rule] = []
    while remaining:
        ready = next(
            (
                rule for rule in remaining
                if not any(
                    lower == rule.name
                    and any(item.name == higher for item in remaining)
                    for higher, lower in edges
                )
            ),
            None,
        )
        if ready is None:
            raise SystemVerilogEmissionError("rule priority graph is cyclic")
        ordered.append(ready)
        remaining.remove(ready)
    return tuple(ordered)
