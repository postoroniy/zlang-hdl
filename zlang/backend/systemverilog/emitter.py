"""Fail-closed direct SystemVerilog emission for the production backend.

The backend consumes typed semantic IR, emits only validated shapes, and reports
unsupported regions before publishing a BackendArtifact.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
import hashlib
import re
from typing import Callable, Iterator

from zlang.common import stable_digest
from zlang.ir import csr as ir_csr
from zlang.ir import expressions as expr
from zlang.ir import packing as ir_packing
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
    parse_ready_valid_field_name,
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
    Rule,
    default_selected_ir_identity,
)
from zlang.ir.normalization import normalize_selected_values
from zlang.ir.storage import (
    FifoSignal,
    MemoryCollision,
    MemoryPortKind,
    MemoryResetPolicy,
    RomSignal,
)
from zlang.ir.state import (
    FifoOccupancy,
    StateActionKind,
    StateResourceKind,
    action_activation_predicate_index,
    conditional_activation_predicates,
    groups_conflict,
    ordered_groups as ordered_state_groups,
    selection_regions,
    transition_for_domain,
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
    ExpressionAliasMap,
    FUNCTIONAL_REGION_EMISSION_SCHEMA,
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
)
from zlang.ir.functional_regions import (
    CompileTimeBinderRef,
    CompileTimeExpr,
    CompileTimeOperator,
)
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.ir.recursive_formal import BackendPhysicalLocator
from zlang.memory_planning import (
    MemoryImplementationKind,
    plan_memory_implementation,
)
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
class TopBoundaryPlan:
    """One inline public boundary for the selected direct-SV top.

    Component modules retain their compact packed ABI.  Only the selected top
    uses this plan: public aggregate leaves remain real module ports while the
    existing body consumes deterministic private packed aliases in the same
    module.  No wrapper module or hierarchy instance is involved.
    """

    module_name: str
    public_ports: tuple[str, ...]
    base_aliases: tuple[tuple[str, str], ...]
    declarations: tuple[str, ...]
    input_bridges: tuple[str, ...]
    output_bridges: tuple[str, ...]

    @property
    def identity(self) -> str:
        """Stable physical identity of the validated inline boundary."""

        return stable_digest({
            "schema": "zlang-direct-sv-top-boundary-v2",
            "packing_layout_schema": ir_packing.PACKING_LAYOUT_SCHEMA,
            "module": self.module_name,
            "public_ports": list(self.public_ports),
            "base_aliases": [list(item) for item in self.base_aliases],
            "declarations": list(self.declarations),
            "input_bridges": list(self.input_bridges),
            "output_bridges": list(self.output_bridges),
        })

    @property
    def physical_module_name(self) -> str:
        return rtl_identifier(self.module_name)

    def alias(self, source_name: str) -> str | None:
        return next(
            (physical for source, physical in self.base_aliases
             if source == source_name),
            None,
        )


_CURRENT_TOP_BOUNDARY: ContextVar[TopBoundaryPlan | None] = ContextVar(
    "zlang_systemverilog_top_boundary", default=None,
)


@contextmanager
def _top_boundary_scope(
    plan: TopBoundaryPlan | None,
) -> Iterator[None]:
    token = _CURRENT_TOP_BOUNDARY.set(plan)
    try:
        yield
    finally:
        _CURRENT_TOP_BOUNDARY.reset(token)


@dataclass(frozen=True)
class FunctionalRegionEmissionPlan:
    """Stable statement-level lowering plan for one compact region."""

    identity: str
    result_name: str
    loop_variable: str
    element_width: int
    table_temporaries: tuple[tuple[expr.FunctionalTableLookup, str], ...]
    expression_temporaries: tuple[_MaterializedExpression, ...]
    reduction_temporaries: tuple["_FunctionalReductionEmissionPlan", ...] = ()
    scatter: "_FunctionalScatterEmissionPlan | None" = None
    schema: str = FUNCTIONAL_REGION_EMISSION_SCHEMA


@dataclass(frozen=True)
class FunctionalRegionCompositionNode:
    """One region and its exact lexical dependencies."""

    region: expr.FunctionalRegion
    identity: str
    dependencies: tuple[str, ...]
    free_binders: tuple[str, ...]


@dataclass(frozen=True)
class FunctionalRegionCompositionPlan:
    """Stable dependency-first plan for nested compact regions."""

    nodes: tuple[FunctionalRegionCompositionNode, ...]
    schema: str = FUNCTIONAL_REGION_EMISSION_SCHEMA


@dataclass(frozen=True)
class _FunctionalReductionEmissionPlan:
    """One exact associative bitwise reduction over a compact child region."""

    reduction: expr.Reduce
    accumulator: str
    loop_variable: str
    table_temporaries: tuple[tuple[expr.FunctionalTableLookup, str], ...]
    children: tuple["_FunctionalReductionEmissionPlan", ...]


@dataclass(frozen=True)
class _FunctionalScatterDimension:
    region: expr.FunctionalRegion
    loop_variable: str
    table_temporaries: tuple[tuple[expr.FunctionalTableLookup, str], ...]


@dataclass(frozen=True)
class _FunctionalScatterEntry:
    enable: expr.Expression
    address: expr.Expression
    value: expr.Expression


@dataclass(frozen=True)
class _FunctionalScatterEmissionPlan:
    """Invert an exact destination decode into candidate-addressed writes."""

    dimensions: tuple[_FunctionalScatterDimension, ...]
    entries: tuple[_FunctionalScatterEntry, ...]
    enable_temporary: str
    address_temporary: str
    value_temporary: str


@dataclass(frozen=True)
class _FunctionalExpressionContext:
    binders: tuple[tuple[str, str], ...]
    captures: tuple[tuple[str, expr.Expression], ...]
    table_temporaries: tuple[tuple[expr.FunctionalTableLookup, str], ...]

    def binder(self, identity: str) -> str | None:
        return next((name for key, name in self.binders if key == identity), None)

    def capture(self, identity: str) -> expr.Expression | None:
        return next((value for key, value in self.captures if key == identity), None)

    def table_temporary(self, lookup: expr.FunctionalTableLookup) -> str | None:
        return next(
            (name for candidate, name in self.table_temporaries if candidate == lookup),
            None,
        )


_CURRENT_FUNCTIONAL_EXPRESSION: ContextVar[_FunctionalExpressionContext | None] = (
    ContextVar("zlang_systemverilog_functional_expression", default=None)
)


@contextmanager
def _functional_expression_scope(
    context: _FunctionalExpressionContext,
) -> Iterator[None]:
    token = _CURRENT_FUNCTIONAL_EXPRESSION.set(context)
    try:
        yield
    finally:
        _CURRENT_FUNCTIONAL_EXPRESSION.reset(token)


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
    physical_module = _physicalize_generic_callables(
        normalize_selected_values(module, prune_callables=False)
    )
    hierarchy_cache = HierarchyTraversalCache()
    public_abi = build_top_physical_abi(physical_module)
    _validate_public_leaf_identifiers(tuple(public_abi.leaves))
    _validate_state_storage_rtl_namespace(
        physical_module, hierarchy_cache=hierarchy_cache
    )
    boundary = _build_top_boundary_plan(
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
    return packed


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
        if memory.ported:
            implementation = plan_memory_implementation(memory)
            if (
                implementation.implementation
                is MemoryImplementationKind.REPLICATED_1R1W
            ):
                for port in memory.ports:
                    if port.kind in {MemoryPortKind.READ, MemoryPortKind.READ_WRITE}:
                        claim(
                            f"{rtl_memory_cells_identifier(memory.name)}_"
                            f"{rtl_identifier(port.name)}",
                            f"memory '{memory.name}' port '{port.name}' cells",
                        )
            else:
                claim(
                    rtl_memory_cells_identifier(memory.name),
                    f"memory '{memory.name}' cells",
                )
            for port in memory.ports:
                if port.kind in {MemoryPortKind.READ, MemoryPortKind.READ_WRITE}:
                    claim(
                        f"{name}_{rtl_identifier(port.name)}_read_data",
                        f"memory '{memory.name}' port '{port.name}' read data",
                    )
        else:
            claim(
                rtl_memory_cells_identifier(memory.name),
                f"memory '{memory.name}' cells",
            )
            claim(
                rtl_memory_read_data_identifier(memory.name),
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

    rewritten_objects: dict[int, object] = {}

    def rewrite(value: object) -> object:
        cache_key = id(value)
        cached = rewritten_objects.get(cache_key)
        if cached is not None:
            return cached
        if isinstance(value, expr.Call):
            arguments = tuple(rewrite(item) for item in value.arguments)
            identity = value.callee_identity or ""
            replacement = physical.get(identity)
            if replacement is None:
                result = replace(value, arguments=arguments)
            else:
                result = replace(
                    value,
                    function=f"zlang_spec_{replacement}",
                    arguments=arguments,
                    callee_identity=replacement,
                )
            rewritten_objects[cache_key] = result
            return result
        if isinstance(value, Function):
            body = rewrite(value.body)
            identity = value.callee_identity
            replacement = physical.get(identity)
            if replacement is None:
                result = replace(value, body=body)
            else:
                metadata = value.metadata
                result = replace(
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
            rewritten_objects[cache_key] = result
            return result
        if isinstance(value, tuple):
            result = tuple(rewrite(item) for item in value)
            rewritten_objects[cache_key] = result
            return result
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
            result = replace(value, **updates) if updates else value
            rewritten_objects[cache_key] = result
            return result
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
    top_boundary: TopBoundaryPlan | None = None,
) -> str:
    """Emit one supported typed module as direct synthesizable SystemVerilog."""

    if len(module.clock_domains) == 1:
        try:
            _module_domain(module)
        except _PhysicalDomainError as error:
            raise SystemVerilogEmissionError(str(error)) from error
    else:
        try:
            for domain in module.clock_domains:
                _module_domain(module, domain.clock)
        except _PhysicalDomainError as error:
            raise SystemVerilogEmissionError(str(error)) from error

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
            *_STATE_GROUPS,
        )
        with _top_boundary_scope(top_boundary):
            body = _emit_cdc_subsystem(module, _cdc_rendering(module))
        return "`default_nettype none\n" + body + "`default_nettype wire\n"
    if any(connection.adapter is not None for connection in module.connections):
        _account_emission_plan(
            module,
            "connection_adapter",
            ModuleFeatureGroup.PROTOCOL_PORTS,
            ModuleFeatureGroup.CONNECTIONS,
            ModuleFeatureGroup.CREDIT_PORTS,
        )
        with _top_boundary_scope(top_boundary):
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
        with _top_boundary_scope(top_boundary):
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
        with _top_boundary_scope(top_boundary):
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
        with _top_boundary_scope(top_boundary):
            body = _emit_external_wrapper(
                module,
                rtl_identifier(module.name),
                mapping_index[module.external_contract.semantic_identity],
            )
        return (
            "`default_nettype none\n"
            + _external_sources(mapping_index)
            + body
            + "`default_nettype wire\n"
        )
    with _top_boundary_scope(top_boundary):
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
            body = (
                _emit_pipeline(module)
                if has_staging else _emit_combinational(module)
            )
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
    """Render one public top leaf, preserving vectors as packed SV arrays."""

    direction = (
        "input" if leaf.direction is PortDirection.INPUT else "output"
    )
    name = _identifier(leaf.external_name)
    if not leaf.array_dimensions:
        return _logic_port(direction, name, leaf.canonical_type)
    element = leaf.element_type
    signed = " signed" if isinstance(element, (SIntType, FixedType)) else ""
    net = " wire" if direction == "input" else ""
    dimensions = "".join(
        f"[{length - 1}:0]" for length in leaf.array_dimensions
    )
    return (
        f"{direction}{net} logic{signed} {dimensions}"
        f"{_range(_width(element))}{name}"
    )


def _public_leaf_elements(leaf: object) -> tuple[tuple[object, str], ...]:
    """Return exact packed slices and their public scalar/array expressions."""

    name = _identifier(leaf.external_name)
    return tuple(
        (
            item,
            name + "".join(
                f"[{index}]"
                for length, index in zip(
                    leaf.array_dimensions, item.indices, strict=True,
                )
            ),
        )
        for item in leaf.packed_element_slices
    )


def _top_boundary_source_base(leaf: object) -> str:
    """Return the source-owned base which names one compact packed root."""

    if leaf.category in {"clock", "reset"}:
        return str(leaf.external_name)
    if leaf.category == "port":
        return str(leaf.member_path[0])
    if leaf.category == "request_response":
        return str(leaf.member_path[0])
    if leaf.category == "aggregate":
        endpoint, member, *_ = leaf.member_path
        return f"{endpoint}__{member}"
    if leaf.packed_root_external_name:
        return str(leaf.packed_root_external_name)
    raise SystemVerilogEmissionError(
        f"public top ABI leaf '{leaf.leaf_semantic_id}' has no packed root base"
    )


def _top_boundary_signal(leaf: object) -> str:
    """Map one typed physical-ABI root to its containing-module signal."""

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


def _root_slices(group: list[object]) -> tuple[object, ...]:
    return tuple(
        sorted(
            (
                element
                for leaf in group
                for element in leaf.packed_element_slices
            ),
            key=lambda item: (item.lsb, item.msb),
        )
    )


def _validate_top_boundary_root(root: str, group: list[object]) -> None:
    """Prove that public leaves cover one compact root exactly once."""

    first = group[0]
    if first.packed_root_type is None:
        raise SystemVerilogEmissionError(
            f"public top ABI root '{root}' has no packed type"
        )
    if any(
        leaf.direction is not first.direction
        or leaf.packed_root_type != first.packed_root_type
        or _top_boundary_source_base(leaf)
        != _top_boundary_source_base(first)
        for leaf in group
    ):
        raise SystemVerilogEmissionError(
            f"public top ABI root '{root}' has inconsistent leaf metadata"
        )
    slices = _root_slices(group)
    width = _width(first.packed_root_type)
    if not slices or slices[0].lsb != 0 or slices[-1].msb != width - 1:
        raise SystemVerilogEmissionError(
            f"public top ABI root '{root}' does not cover its exact packed width"
        )
    cursor = 0
    for item in slices:
        if item.lsb != cursor:
            raise SystemVerilogEmissionError(
                f"public top ABI root '{root}' has overlapping or missing slices"
            )
        cursor = item.msb + 1
    if cursor != width:
        raise SystemVerilogEmissionError(
            f"public top ABI root '{root}' does not cover its exact packed width"
        )


def _leaf_is_contiguous(leaf: object) -> bool:
    slices = tuple(sorted(
        leaf.packed_element_slices, key=lambda item: item.msb, reverse=True,
    ))
    return bool(slices) and all(
        left.lsb == right.msb + 1
        for left, right in zip(slices, slices[1:])
    )


def _leaf_input_value(leaf: object) -> str:
    name = rtl_identifier(leaf.external_name)
    if not leaf.array_dimensions or _leaf_is_contiguous(leaf):
        return name
    return "{" + ", ".join(
        value
        for _item, value in sorted(
            _public_leaf_elements(leaf),
            key=lambda pair: pair[0].msb,
            reverse=True,
        )
    ) + "}"


def _build_top_boundary_plan(
    module: Module,
    *,
    leaves: tuple[object, ...] | None = None,
) -> TopBoundaryPlan:
    """Build the selected top's inline leaf/packed-array boundary."""

    selected_leaves = leaves or tuple(build_top_physical_abi(module).leaves)
    _validate_public_leaf_identifiers(selected_leaves)
    public_ports = tuple(
        _public_leaf_port_declaration(leaf) for leaf in selected_leaves
    )
    roots: dict[str, list[object]] = {}
    root_order: list[str] = []
    for leaf in selected_leaves:
        if leaf.signal_kind in {"clock", "reset"}:
            continue
        root = leaf.packed_root_semantic_id
        if root is None:
            raise SystemVerilogEmissionError(
                f"public top ABI leaf '{leaf.leaf_semantic_id}' has no packed root"
            )
        if root not in roots:
            roots[root] = []
            root_order.append(root)
        roots[root].append(leaf)
    for root, group in roots.items():
        _validate_top_boundary_root(root, group)

    bases_requiring_alias: set[str] = set()
    for root in root_order:
        group = roots[root]
        first = group[0]
        direct = (
            len(group) == 1
            and not first.array_dimensions
            and first.leaf_semantic_id == root
            and rtl_identifier(first.external_name)
            == _top_boundary_signal(first)
        )
        if not direct:
            bases_requiring_alias.add(_top_boundary_source_base(first))

    used = set(module_rtl_names(module).allocated_names)
    used.update(rtl_identifier(leaf.external_name) for leaf in selected_leaves)
    aliases: dict[str, str] = {}
    for base in sorted(bases_requiring_alias):
        aliases[base] = allocate_private_rtl_identifier(
            f"zlang_packed_{base}",
            semantic_identity=f"top-boundary:{module.name}:{base}",
            used=used,
        )

    partial = TopBoundaryPlan(
        module.name,
        public_ports,
        tuple(sorted(aliases.items())),
        (), (), (),
    )
    declarations: list[str] = []
    input_bridges: list[str] = []
    output_bridges: list[str] = []
    with _top_boundary_scope(partial):
        for root in root_order:
            group = roots[root]
            first = group[0]
            base = _top_boundary_source_base(first)
            if base not in aliases:
                continue
            signal = _top_boundary_signal(first)
            root_width = _width(first.packed_root_type)
            signed = " signed" if isinstance(
                first.packed_root_type, (SIntType, FixedType)
            ) else ""
            declarations.append(
                f"  logic{signed} {_range(root_width)}{signal};"
            )
            ordered = sorted(
                group,
                key=lambda leaf: max(
                    item.msb for item in leaf.packed_element_slices
                ),
                reverse=True,
            )
            if first.direction is PortDirection.INPUT:
                if all(_leaf_is_contiguous(leaf) for leaf in ordered):
                    parts = [_leaf_input_value(leaf) for leaf in ordered]
                    value = parts[0] if len(parts) == 1 else (
                        "{" + ", ".join(parts) + "}"
                    )
                    input_bridges.append(f"  assign {signal} = {value};")
                else:
                    for leaf in ordered:
                        for item, source in _public_leaf_elements(leaf):
                            input_bridges.append(
                                f"  assign {_slice(signal, item.msb, item.lsb)} "
                                f"= {source};"
                            )
                continue

            for leaf in ordered:
                name = rtl_identifier(leaf.external_name)
                elements = _public_leaf_elements(leaf)
                if _leaf_is_contiguous(leaf):
                    msb = max(item.msb for item, _target in elements)
                    lsb = min(item.lsb for item, _target in elements)
                    value = (
                        signal
                        if msb == root_width - 1 and lsb == 0
                        else _slice(signal, msb, lsb)
                    )
                    output_bridges.append(
                        f"  assign {name} = {value};"
                    )
                    continue
                for item, target in elements:
                    output_bridges.append(
                        f"  assign {target} = "
                        f"{_slice(signal, item.msb, item.lsb)};"
                    )
    return replace(
        partial,
        declarations=tuple(declarations),
        input_bridges=tuple(input_bridges),
        output_bridges=tuple(output_bridges),
    )


def physical_state_root_path(module: Module) -> tuple[str, ...]:
    """Return the selected top's direct architectural-state VPI root."""

    return ("TOP", rtl_identifier(module.name))


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
            protocol_field = parse_ready_valid_field_name(value.port)
            return expr.InputRef(
                (
                    local_names.child_signal(
                        value.instance,
                        protocol_field[0],
                        protocol_field[1].value,
                    )
                    if protocol_field is not None
                    else local_names.child_signal(value.instance, value.port)
                ),
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
    preferred = tuple(
        (_instance_expression(module, local.expression, names), _identifier(local.name))
        for local in module.locals
        if not local.compile_time
    )
    reserved_names = {
        _identifier(item.name) for item in (*module.ports, *module.registers, *module.locals)
    }
    reserved_names.update(item.physical_name for item in names.entries)
    planned = list(plan_materialization(
        roots,
        preferred_names=preferred,
        reserved_names=reserved_names,
    ))
    planned_regions = {
        item.expression for item in planned if isinstance(item.expression, expr.FunctionalRegion)
    }
    used_names = reserved_names | {item.name for item in planned}
    for root in roots:
        for region in _functional_region_objects(root):
            if region in planned_regions:
                continue
            identity = expression_semantic_identity(region)
            name = allocate_private_rtl_identifier(
                f"region_{identity[:10]}",
                semantic_identity=(
                    f"{FUNCTIONAL_REGION_EMISSION_SCHEMA}:module:{identity}"
                ),
                used=used_names,
            )
            planned.append(_MaterializedExpression(region, name))
            planned_regions.add(region)
    return tuple(planned)


def _materialized_emission(module: Module):
    names = module_rtl_names(module)
    materialized = _materialization_plan(module, names)
    aliases = ExpressionAliasMap(
        (item.expression, item.name) for item in materialized
    )

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
        if isinstance(item.expression, expr.FunctionalRegion):
            region = _replace_materialized(
                item.expression,
                aliases,
                keep=item.expression,
                rewrite_region_owned=True,
            )
            assert isinstance(region, expr.FunctionalRegion)
            replication = _functional_region_replication(region)
            if replication is not None:
                assignments.append(f"  assign {item.name} = {replication};")
                continue
            plan = _functional_region_plan(
                region,
                item.name,
                reserved_names=tuple(
                    entry.name for entry in materialized
                ),
            )
            region_declarations, region_statements = _functional_region_rendering(
                region,
                plan,
                declaration_indent="  ",
                statement_indent="    ",
            )
            declarations.extend(region_declarations)
            assignments.extend(
                ("  always_comb begin", *region_statements, "  end")
            )
        else:
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
            component_identifier=rtl_identifier,
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
            emit_cdc_child=_emit_composed_cdc_child,
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
            suspend_top_boundary=lambda: _top_boundary_scope(None),
        ),
        formal_buffer_counts=formal_buffer_counts,
    )


def _emit_composed_csr_child(module: Module) -> str:
    return _emit_csr(module, expose_internal_abi=True)


def _emit_composed_cdc_child(module: Module) -> str:
    return _emit_cdc_subsystem(module, _cdc_rendering(module))


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
def _cdc_rendering(
    module: Module | None = None, *, ram_style: str | None = None,
) -> CDCRendering:
    def emit_module(
        typed_module: Module,
        ports: list[str],
        lines: list[str],
    ) -> str:
        if module is None or not (
            typed_module.registers
            or typed_module.next_assignments
            or typed_module.rules
            or typed_module.assignments
        ):
            return _module(typed_module, ports, lines)

        crossing = next(
            item for item in typed_module.connections
            if item.crossing is not None
        )
        endpoint_names = {
            crossing.source.name,
            crossing.destination.name,
        }
        extra_ports = [
            _port_declaration(port)
            for port in typed_module.ports
            if port.name not in endpoint_names
            and port.protocol is InterfaceProtocol.WIRE
        ]
        declarations, _assignments, render = _materialized_emission(typed_module)
        state_lines: list[str] = []
        if typed_module.registers or typed_module.next_assignments or typed_module.rules:
            _append_unified_state(
                typed_module,
                declarations,
                state_lines,
                render,
            )
        else:
            state_lines.extend(
                f"  assign {_identifier(_assignment_name(assignment))} = "
                f"{render(assignment.expression)};"
                for assignment in typed_module.assignments
            )
        return _module(
            typed_module,
            [*ports, *extra_ports],
            [*declarations, *lines, *state_lines],
        )

    return CDCRendering(
        SystemVerilogEmissionError,
        _identifier,
        _logic_port,
        _width,
        _range,
        lambda typed_module, clock: _clock_event(
            typed_module, _identifier, clock
        ),
        lambda typed_module, clock: _reset_asserted(
            typed_module, _identifier, clock
        ),
        emit_module,
        lambda typed_module, memory: _ported_memory_fragments(
            typed_module, memory, _expression, ram_style=ram_style
        ),
    )


def _emit_target_async_fifo(module: Module) -> str:
    """Emit the selected typed FIFO decomposition with inferred block storage."""

    return (
        "`default_nettype none\n"
        + _emit_cdc_subsystem(module, _cdc_rendering(module, ram_style="block"))
        + "`default_nettype wire\n"
    )


def _named_module(
    name: str,
    ports: list[str],
    lines: list[str],
    *,
    typed_module: Module | None = None,
) -> str:
    boundary = _CURRENT_TOP_BOUNDARY.get()
    if boundary is not None and name == boundary.physical_module_name:
        if boundary.base_aliases:
            ports = list(boundary.public_ports)
        bridge_prefix = (
            ("  // Compiler-generated inline top boundary.",)
            if boundary.declarations
            or boundary.input_bridges
            or boundary.output_bridges
            else ()
        )
        lines = [*bridge_prefix, *boundary.declarations,
                 *boundary.input_bridges, *lines, *boundary.output_bridges]
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


def _ported_memory_fragments(
    module: Module,
    memory,
    render,
    *,
    ram_style: str | None = None,
) -> tuple[list[str], list[str]]:
    """Emit one normalized named-port memory without backend re-discovery."""

    if not memory.ports:
        raise SystemVerilogEmissionError("ported-memory emission requires named ports")
    if memory.async_memory and memory.collision is not MemoryCollision.READ_FIRST:
        raise SystemVerilogEmissionError(
            f"independent-clock memory collision mode '{memory.collision.value}' "
            "requires an exact target physical binding; generic synthesizable "
            "SystemVerilog publishes only the structural old-data model"
        )
    width = _width(memory.element_type)
    name = _identifier(memory.name)
    base_cells = rtl_memory_cells_identifier(memory.name)
    initial_word = (
        render(memory.initial_value) if memory.initial_value is not None else "'0"
    )
    style = f'(* ram_style = "{ram_style}" *) ' if ram_style else ""
    readable = tuple(
        port for port in memory.ports
        if port.kind in {MemoryPortKind.READ, MemoryPortKind.READ_WRITE}
    )
    writable = tuple(
        port for port in memory.ports
        if port.kind in {MemoryPortKind.WRITE, MemoryPortKind.READ_WRITE}
    )
    implementation = plan_memory_implementation(memory)
    replicated = (
        implementation.implementation
        is MemoryImplementationKind.REPLICATED_1R1W
    )
    cells_by_read_port = {
        port.name: (
            f"{base_cells}_{_identifier(port.name)}"
            if replicated else base_cells
        )
        for port in readable
    }
    cell_arrays = tuple(dict.fromkeys(cells_by_read_port.values())) or (base_cells,)
    declarations = [
        *(
            f"  {style}logic [{width - 1}:0] {cells} [0:{memory.depth - 1}];"
            for cells in cell_arrays
        ),
    ]
    logic: list[str] = [
        f"  // memory_plan={implementation.implementation.value} "
        f"identity={implementation.identity}"
    ]
    if (
        memory.initial_value is not None
        or memory.contents_reset is MemoryResetPolicy.PRESERVE
    ):
        logic.append("  initial begin")
        for cells in cell_arrays:
            logic.extend((
                f"    for (integer {name}_reset_index = 0; "
                f"{name}_reset_index < {memory.depth}; "
                f"{name}_reset_index = {name}_reset_index + 1)",
                f"      {cells}[{name}_reset_index] = {initial_word};",
            ))
        logic.append("  end")
    by_name = {port.name: port for port in writable}
    priority = tuple(
        by_name[item] for item in memory.write_priority
    ) if memory.write_priority else writable
    effective_enable: dict[str, str] = {}
    higher: list[object] = []
    for port in priority:
        assert port.write_enable is not None
        enable = render(port.write_enable)
        blockers = [
            f"({effective_enable[item.name]} && "
            f"({render(item.address)} == {render(port.address)}))"
            for item in higher
        ]
        effective_enable[port.name] = (
            enable if not blockers
            else f"({enable} && !({' || '.join(blockers)}))"
        )
        higher.append(port)
    for port in readable:
        if memory.read_latency > 1:
            declarations.extend(
                f"  logic [{width - 1}:0] {name}_{_identifier(port.name)}_read_stage_{index};"
                for index in range(memory.read_latency - 1)
            )
        declarations.append(
            f"  logic [{width - 1}:0] {name}_{_identifier(port.name)}_read_data;"
        )
    for port in writable:
        if port.write_mask is None:
            continue
        lanes = memory.write_mask_width
        assert lanes is not None
        prefix = f"{name}_{_identifier(port.name)}"
        declarations.extend((
            f"  logic [{lanes - 1}:0] {prefix}_write_mask;",
            f"  logic [{width - 1}:0] {prefix}_write_mask_expanded;",
            f"  logic [{width - 1}:0] {prefix}_write_merged;",
        ))
        expanded = _memory_byte_mask_concatenation(
            f"{prefix}_write_mask", element_width=width, lane_count=lanes
        )
        assert port.write_data is not None
        logic.extend((
            f"  assign {prefix}_write_mask = {render(port.write_mask)};",
            f"  assign {prefix}_write_mask_expanded = {{{expanded}}};",
            f"  assign {prefix}_write_merged = "
            f"({cell_arrays[0]}[{render(port.address)}] & ~{prefix}_write_mask_expanded) | "
            f"({render(port.write_data)} & {prefix}_write_mask_expanded);",
        ))

    def write_value(port) -> str:
        assert port.write_data is not None
        return (
            f"{name}_{_identifier(port.name)}_write_merged"
            if port.write_mask is not None else render(port.write_data)
        )

    writer_domains = {port.domain for port in writable}
    if len(writer_domains) > 1:
        raise SystemVerilogEmissionError(
            "ported-memory RTL supports writers in one domain"
        )
    writer_domain = next(iter(writer_domains), memory.domain)

    def append_cell_reset(indent: str) -> None:
        if memory.contents_reset is MemoryResetPolicy.CLEAR:
            for cells in cell_arrays:
                logic.extend((
                    f"{indent}for (integer {name}_reset_index = 0; "
                    f"{name}_reset_index < {memory.depth}; "
                    f"{name}_reset_index = {name}_reset_index + 1)",
                    f"{indent}  {cells}[{name}_reset_index] <= {initial_word};",
                ))
        else:
            logic.append(f"{indent}// Memory contents hold across writer reset.")

    def append_writes(indent: str) -> None:
        for port in priority:
            for cells in cell_arrays:
                logic.append(
                    f"{indent}if ({effective_enable[port.name]}) "
                    f"{cells}[{render(port.address)}] <= {write_value(port)};"
                )

    def append_read_reset(domain_ports, indent: str) -> None:
        for port in domain_ports:
            target = f"{name}_{_identifier(port.name)}_read_data"
            if memory.read_data_reset is MemoryResetPolicy.CLEAR:
                for index in range(memory.read_latency - 1):
                    logic.append(
                        f"{indent}{name}_{_identifier(port.name)}_read_stage_{index} <= '0;"
                    )
                logic.append(f"{indent}{target} <= '0;")

    def append_reads(domain_ports, indent: str) -> None:
        for port in domain_ports:
            assert port.read_enable is not None
            prefix = f"{name}_{_identifier(port.name)}"
            target = (
                f"{prefix}_read_stage_0"
                if memory.read_latency > 1 else f"{prefix}_read_data"
            )
            read_cells = cells_by_read_port[port.name]
            collisions = [
                (writer, f"({effective_enable[writer.name]} && "
                 f"({render(writer.address)} == {render(port.address)}))")
                for writer in priority
            ]
            any_collision = " || ".join(term for _, term in collisions) or "1'b0"
            if memory.collision is MemoryCollision.NO_CHANGE:
                logic.append(
                    f"{indent}if ({render(port.read_enable)} && !({any_collision})) "
                    f"{target} <= {read_cells}[{render(port.address)}];"
                )
            elif memory.collision is MemoryCollision.WRITE_FIRST and collisions:
                selected = f"{read_cells}[{render(port.address)}]"
                for writer, term in reversed(collisions):
                    selected = f"{term} ? {write_value(writer)} : ({selected})"
                logic.append(
                    f"{indent}if ({render(port.read_enable)}) {target} <= {selected};"
                )
            else:
                logic.append(
                    f"{indent}if ({render(port.read_enable)}) "
                    f"{target} <= {read_cells}[{render(port.address)}];"
                )

    def append_read_shifts(domain_ports, indent: str) -> None:
        if memory.read_latency <= 1:
            return
        for port in domain_ports:
            prefix = f"{name}_{_identifier(port.name)}"
            for index in range(1, memory.read_latency - 1):
                logic.append(
                    f"{indent}{prefix}_read_stage_{index} <= "
                    f"{prefix}_read_stage_{index - 1};"
                )
            logic.append(
                f"{indent}{prefix}_read_data <= "
                f"{prefix}_read_stage_{memory.read_latency - 2};"
            )

    read_domains = tuple(dict.fromkeys(port.domain for port in readable))
    common_registered = (
        not memory.async_memory
        and memory.read_latency >= 1
        and writer_domain is not None
        and read_domains == (writer_domain,)
    )
    native_true_dual = (
        ram_style == "block"
        and common_registered
        and memory.read_latency == 1
        and len(memory.ports) == 2
        and all(port.kind is MemoryPortKind.READ_WRITE for port in memory.ports)
    )
    if native_true_dual:
        if memory.contents_reset is not MemoryResetPolicy.PRESERVE:
            raise SystemVerilogEmissionError(
                "selected true-dual block-memory emission requires preserved "
                "contents; clearing every cell prevents exact native inference"
            )
        # A true-dual RAM has one physical clocked process per port.  Generic
        # ported memories deliberately retain their single deterministic
        # process; this shape is emitted only after exact target selection.
        for port in memory.ports:
            target = f"{name}_{_identifier(port.name)}_read_data"
            logic.extend((
                f"  always_ff @({_clock_event(module, _identifier, port.domain)}) begin",
                f"    if ({_reset_asserted(module, _identifier, port.domain)}) begin",
            ))
            if memory.read_data_reset is MemoryResetPolicy.CLEAR:
                logic.append(f"      {target} <= '0;")
            logic.append("    end else begin")
            logic.append(
                f"      if ({effective_enable[port.name]}) "
                f"{base_cells}[{render(port.address)}] <= {write_value(port)};"
            )
            append_reads((port,), "      ")
            logic.extend(("    end", "  end"))
        return declarations, logic
    if common_registered:
        logic.extend((
            f"  always_ff @({_clock_event(module, _identifier, writer_domain)}) begin",
            f"    if ({_reset_asserted(module, _identifier, writer_domain)}) begin",
        ))
        append_cell_reset("      ")
        append_read_reset(readable, "      ")
        logic.append("    end else begin")
        append_writes("      ")
        append_reads(readable, "      ")
        append_read_shifts(readable, "      ")
        logic.extend(("    end", "  end"))
        return declarations, logic

    if writer_domain is not None:
        logic.extend((
            f"  always_ff @({_clock_event(module, _identifier, writer_domain)}) begin",
            f"    if ({_reset_asserted(module, _identifier, writer_domain)}) begin",
        ))
        append_cell_reset("      ")
        logic.append("    end else begin")
        append_writes("      ")
        logic.extend(("    end", "  end"))

    for domain in read_domains:
        domain_ports = tuple(port for port in readable if port.domain == domain)
        if memory.read_latency == 0:
            for port in domain_ports:
                target = f"{name}_{_identifier(port.name)}_read_data"
                read_value = (
                    f"{cells_by_read_port[port.name]}[{render(port.address)}]"
                )
                if memory.collision is MemoryCollision.WRITE_FIRST:
                    for writer in reversed(priority):
                        collision = (
                            f"({effective_enable[writer.name]} && "
                            f"({render(writer.address)} == {render(port.address)}))"
                        )
                        read_value = (
                            f"{collision} ? {write_value(writer)} : ({read_value})"
                        )
                if memory.read_data_reset is MemoryResetPolicy.CLEAR:
                    read_value = (
                        f"{_reset_asserted(module, _identifier, domain)} "
                        f"? '0 : ({read_value})"
                    )
                logic.append(f"  assign {target} = {read_value};")
            continue
        logic.extend((
            f"  always_ff @({_clock_event(module, _identifier, domain)}) begin",
            f"    if ({_reset_asserted(module, _identifier, domain)}) begin",
        ))
        append_read_reset(domain_ports, "      ")
        logic.append("    end else begin")
        append_reads(domain_ports, "      ")
        append_read_shifts(domain_ports, "      ")
        logic.extend(("    end", "  end"))
    return declarations, logic


def _emit_memory(module: Module, *, ram_style: str | None = None) -> str:
    if len(module.memories) != 1 or not module.clock_domains:
        raise SystemVerilogEmissionError(
            "direct SystemVerilog memory emission requires one memory and clock/reset"
        )
    memory = module.memories[0]
    if memory.ports:
        declarations, lines = _ported_memory_fragments(
            module, memory, _expression, ram_style=ram_style
        )
        for assignment in module.assignments:
            if assignment.target.name == memory.name:
                continue
            lines.append(
                f"  assign {_identifier(_assignment_name(assignment))} = "
                f"{_expression(assignment.expression)};"
            )
        return _named_module(
            module.name,
            _physical_port_declarations(module),
            declarations + lines,
            typed_module=module,
        )
    if memory.domain is None:
        raise SystemVerilogEmissionError(
            f"memory '{memory.name}' has no resolved clock domain"
        )
    memory_clock = memory.domain
    if not 0 <= memory.read_latency <= 16:
        raise SystemVerilogEmissionError(
            "direct SystemVerilog memory emission requires read_latency in 0..16"
        )
    width = _width(memory.element_type)
    name = _identifier(memory.name)
    cells_name = rtl_memory_cells_identifier(memory.name)
    read_data_name = rtl_memory_read_data_identifier(memory.name)
    initial_word = (
        _expression(memory.initial_value)
        if memory.initial_value is not None else "'0"
    )
    style = f'(* ram_style = "{ram_style}" *) ' if ram_style else ""
    declarations = [
        f"  {style}logic [{width - 1}:0] {cells_name} [0:{memory.depth - 1}];",
        f"  logic [{width - 1}:0] {read_data_name};",
        *(
            f"  logic [{width - 1}:0] {name}_read_stage_{index};"
            for index in range(memory.read_latency - 1)
        ),
    ]
    read_capture = (
        f"{name}_read_stage_0"
        if memory.read_latency > 1 else read_data_name
    )
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
    if memory.read_latency == 0:
        read_value = f"{name}_cells[{_expression(memory.read_address)}]"
        if memory.collision is MemoryCollision.WRITE_FIRST:
            write_value = (
                name + "_write_merged"
                if memory.write_mask is not None
                else _expression(memory.write_data)
            )
            read_value = (
                f"({_reset_deasserted(module, _identifier, memory_clock)} && "
                f"{_expression(memory.write_enable)} && "
                f"({_expression(memory.read_address)} == "
                f"{_expression(memory.write_address)})) ? "
                f"{write_value} : ({read_value})"
            )
        if memory.read_data_reset is MemoryResetPolicy.CLEAR:
            read_value = (
                f"{_reset_asserted(module, _identifier, memory_clock)} ? '0 : ({read_value})"
            )
        combinational.append(f"  assign {name}_read_data = {read_value};")

    initialization: list[str] = []
    if (
        memory.initial_value is not None
        or memory.contents_reset is MemoryResetPolicy.PRESERVE
        or (
            memory.read_latency >= 1
            and memory.read_data_reset is MemoryResetPolicy.PRESERVE
        )
    ):
        initialization.append("  initial begin")
        if (
            memory.read_latency >= 1
            and memory.read_data_reset is MemoryResetPolicy.PRESERVE
        ):
            initialization.append(f"    {name}_read_data = '0;")
            initialization.extend(
                f"    {name}_read_stage_{index} = '0;"
                for index in range(memory.read_latency - 1)
            )
        if (
            memory.contents_reset is MemoryResetPolicy.PRESERVE
            or memory.initial_value is not None
        ):
            initialization.extend((
                f"    for (integer zlang_memory_reset_index = 0; "
                f"zlang_memory_reset_index < {memory.depth}; "
                "zlang_memory_reset_index = zlang_memory_reset_index + 1)",
                f"      {name}_cells[zlang_memory_reset_index] = {initial_word};",
            ))
        initialization.append("  end")

    lines = [
        *declarations,
        *combinational,
        *initialization,
        f"  {'always' if initialization else 'always_ff'} "
        f"@({_clock_event(module, _identifier, memory_clock)}) begin",
        f"    if ({_reset_asserted(module, _identifier, memory_clock)}) begin",
    ]
    if memory.read_latency >= 1 and memory.read_data_reset is MemoryResetPolicy.CLEAR:
        lines.append(f"      {name}_read_data <= '0;")
        lines.extend(
            f"      {name}_read_stage_{index} <= '0;"
            for index in range(memory.read_latency - 1)
        )
    if memory.contents_reset is MemoryResetPolicy.CLEAR:
        lines.append(
            f"      for (integer zlang_memory_reset_index = 0; "
            f"zlang_memory_reset_index < {memory.depth}; "
            "zlang_memory_reset_index = zlang_memory_reset_index + 1) "
        )
        lines.append(
            f"        {name}_cells[zlang_memory_reset_index] <= {initial_word};"
        )
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
    if memory.read_latency >= 1 and memory.collision is MemoryCollision.WRITE_FIRST:
        lines.append(
            f"      if ({_expression(memory.write_enable)} && "
            f"({_expression(memory.read_address)} == {_expression(memory.write_address)})) "
            f"{read_capture} <= "
            f"{name + '_write_merged' if memory.write_mask is not None else _expression(memory.write_data)};"
        )
        lines.append(
            f"      else {read_capture} <= {name}_cells[{_expression(memory.read_address)}];"
        )
    elif memory.read_latency >= 1:
        lines.append(
            f"      {read_capture} <= {name}_cells[{_expression(memory.read_address)}];"
        )
    if memory.read_latency > 1:
        lines.extend(
            f"      {name}_read_stage_{index} <= {name}_read_stage_{index - 1};"
            for index in range(1, memory.read_latency - 1)
        )
        lines.append(
            f"      {name}_read_data <= "
            f"{name}_read_stage_{memory.read_latency - 2};"
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

    if module.roms and not module.clock_domains:
        raise SystemVerilogEmissionError(
            "initialized ROM emission requires one module clock and reset"
        )
    declarations: list[str] = []
    logic: list[str] = []
    for rom in module.roms:
        if rom.domain is None:
            raise SystemVerilogEmissionError(
                f"ROM '{rom.name}' has no resolved clock domain"
            )
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
            f"  always_ff @({_clock_event(module, _identifier, rom.domain)}) begin",
            f"    if ({_reset_asserted(module, _identifier, rom.domain)}) {name}_read_data <= '0;",
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
    if len(module.fifos) != 1 or not module.clock_domains:
        raise SystemVerilogEmissionError("direct FIFO emission requires one clock/reset FIFO")
    fifo = module.fifos[0]
    if fifo.domain is None:
        raise SystemVerilogEmissionError(
            f"FIFO '{fifo.name}' has no resolved clock domain"
        )
    fifo_clock = fifo.domain
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
        ports = _physical_port_declarations(module)
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
            f"  assign {name}_valid = !({_reset_asserted(module, _identifier, fifo_clock)}) "
            f"&& ({name}_count != '0);"
        )
    if FifoSignal.READY in referenced_fifo_signals:
        observation_declarations.append(f"  logic {name}_ready;")
        observation_assignments.append(
            f"  assign {name}_ready = !({_reset_asserted(module, _identifier, fifo_clock)}) && "
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
        f"  assign {name}_pop = !({_reset_asserted(module, _identifier, fifo_clock)}) && "
        f"{name}_pop_request && ({name}_count != '0);",
        f"  assign {name}_push = !({_reset_asserted(module, _identifier, fifo_clock)}) && "
        f"{name}_push_request && "
        f"(({name}_count < {count_width}'d{fifo.depth}) || {name}_pop);",
    ]
    if FifoSignal.OVERFLOW in referenced_fifo_signals:
        request_declarations.append(f"  logic {name}_overflow;")
        request_assignments.append(
            f"  assign {name}_overflow = !({_reset_asserted(module, _identifier, fifo_clock)}) && "
            f"{name}_push_request && {name}_full && !{name}_pop;"
        )
    if FifoSignal.UNDERFLOW in referenced_fifo_signals:
        request_declarations.append(f"  logic {name}_underflow;")
        request_assignments.append(
            f"  assign {name}_underflow = !({_reset_asserted(module, _identifier, fifo_clock)}) && "
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
             f"  always_ff @({_clock_event(module, _identifier, fifo_clock)}) begin",
             f"    if ({_reset_asserted(module, _identifier, fifo_clock)}) begin {name}_count <= '0; {name}_rd <= '0; {name}_wr <= '0; end",
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
    if not module.clock_domains or module.resolved_transition is None:
        raise SystemVerilogEmissionError("unified state emission requires clock/reset transition IR")
    if any(not fifo.scheduled for fifo in module.fifos):
        raise SystemVerilogEmissionError("mixed legacy and scheduled FIFO resources are not implemented")
    transition = module.resolved_transition
    local_names = module_rtl_names(module)
    groups = ordered_state_groups(transition)
    activation_predicates = conditional_activation_predicates(transition)
    activation_names = tuple(
        f"zlang_condition_{index}_active"
        for index in range(len(activation_predicates))
    )

    for memory in module.memories:
        if not memory.ported:
            continue
        memory_declarations, memory_logic = _ported_memory_fragments(
            module, memory, render
        )
        declarations.extend(memory_declarations)
        logic.extend(memory_logic)

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
            f"  assign {name}_valid = "
            f"{_reset_deasserted(module, _identifier, fifo.domain)} && !{name}_empty;",
            f"  assign {name}_ready = "
            f"{_reset_deasserted(module, _identifier, fifo.domain)} && "
            f"({name}_count < {fifo.count_width}'d{fifo.depth});",
            f"  assign {name}_overflow = 1'b0;",
            f"  assign {name}_underflow = 1'b0;",
        ))
    for memory in module.memories:
        if memory.ported:
            continue
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

    fifo_by_name = {fifo.name: fifo for fifo in module.fifos}
    for physical_domain in module.clock_domains:
        local_transition = transition_for_domain(
            transition, physical_domain.clock
        )
        local_groups = ordered_state_groups(local_transition)
        local_fifos = tuple(
            resource
            for resource in local_transition.resources
            if resource.kind is StateResourceKind.FIFO
        )
        local_activations = conditional_activation_predicates(local_transition)
        guard_names = [group.rule_name for group in local_groups]
        for group in local_groups:
            clauses: list[str] = []
            for region in selection_regions(local_transition, group.rule_name):
                count_values = region[:len(local_fifos)]
                guard_values = region[
                    len(local_fifos):len(local_fifos) + len(guard_names)
                ]
                activation_values = region[
                    len(local_fifos) + len(guard_names):
                ]
                terms: list[str] = []
                for resource, value in zip(
                    local_fifos, count_values, strict=True
                ):
                    fifo = fifo_by_name[resource.name]
                    name = fifo.name
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
                    f"{activation_names[activation_predicates.index(activation)]} "
                    f"== 1'b{1 if value else 0}"
                    for activation, value in zip(
                        local_activations, activation_values, strict=True
                    )
                    if value is not None
                )
                clauses.append("(" + " && ".join(terms) + ")")
            condition = " || ".join(clauses) if clauses else "1'b0"
            logic.append(
                f"  assign {local_names.rule(group.rule_name, 'fire')} = "
                f"{_reset_deasserted(module, _identifier, physical_domain.clock)} "
                f"&& ({condition});"
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

    for memory in module.memories:
        initialize_contents = (
            memory.contents_reset is MemoryResetPolicy.PRESERVE
            or memory.initial_value is not None
        )
        initialize_read_data = (
            memory.read_data_reset is MemoryResetPolicy.PRESERVE
        )
        if not (initialize_contents or initialize_read_data):
            continue
        name = _identifier(memory.name)
        logic.append("  initial begin")
        if initialize_read_data:
            logic.append(f"    {name}_read_data = '0;")
        if initialize_contents:
            initial_word = (
                render(memory.initial_value)
                if memory.initial_value is not None else "'0"
            )
            logic.extend((
                f"    for (integer {name}_reset_index = 0; "
                f"{name}_reset_index < "
                f"{memory.depth}; {name}_reset_index = {name}_reset_index + 1)",
                f"      {name}_cells[{name}_reset_index] = {initial_word};",
            ))
        logic.append("  end")

    for physical_domain in module.clock_domains:
        domain_registers = tuple(
            item for item in module.registers
            if item.domain == physical_domain.clock
        )
        domain_fifos = tuple(
            item for item in module.fifos
            if item.domain == physical_domain.clock
        )
        domain_memories = tuple(
            item for item in module.memories
            if item.domain == physical_domain.clock
        )
        if not (domain_registers or domain_fifos or domain_memories):
            continue
        domain_has_initialization = any(
            memory.contents_reset is MemoryResetPolicy.PRESERVE
            or memory.read_data_reset is MemoryResetPolicy.PRESERVE
            or memory.initial_value is not None
            for memory in domain_memories
        )
        logic.append(
            f"  {'always' if domain_has_initialization else 'always_ff'} "
            f"@({_clock_event(module, _identifier, physical_domain.clock)}) begin"
        )
        logic.append(
            f"    if ({_reset_asserted(module, _identifier, physical_domain.clock)}) begin"
        )
        for register in domain_registers:
            logic.append(
                f"      {_identifier(register.name)} <= {render(register.initial)};"
            )
        for fifo in domain_fifos:
            name = _identifier(fifo.name)
            logic.append(
                f"      {name}_count <= '0; {name}_rd <= '0; {name}_wr <= '0;"
            )
        for memory in domain_memories:
            name = _identifier(memory.name)
            if memory.read_data_reset is MemoryResetPolicy.CLEAR:
                logic.append(f"      {name}_read_data <= '0;")
            if memory.contents_reset is MemoryResetPolicy.CLEAR:
                initial_word = (
                    render(memory.initial_value)
                    if memory.initial_value is not None else "'0"
                )
                logic.append(
                    f"      for (integer {name}_reset_index = 0; "
                    f"{name}_reset_index < "
                    f"{memory.depth}; {name}_reset_index = {name}_reset_index + 1) "
                    f"{name}_cells[{name}_reset_index] <= {initial_word};"
                )
            if (
                memory.read_data_reset is MemoryResetPolicy.PRESERVE
                and memory.contents_reset is MemoryResetPolicy.PRESERVE
            ):
                logic.append(
                    f"      // {name} contents and read result hold across reset."
                )
        logic.append("    end else begin")
        for register in domain_registers:
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
            default = next(
                (
                    item for item in module.next_assignments
                    if item.target.name == register.name
                ),
                None,
            )
            if default is not None:
                logic.append(
                    f"      {'else ' if writers else ''}{_identifier(register.name)} "
                    f"<= {render(default.expression)};"
                )
        for fifo in domain_fifos:
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
        for memory in domain_memories:
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
        # ``_assignment_name`` is boundary-aware and therefore returns the
        # physical packed-root alias for aggregate top outputs.  Keep this set
        # in that same namespace; mixing the semantic source name here emitted
        # the direct output assignment a second time after boundary inlining.
        emitted_scalar_outputs.add(_identifier(output.name))
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
        # The selected public top owns both boundary bridges and architectural
        # state. Production locators therefore begin directly at that module.
        # Formal-only observation ports have their own publication route.
        state_root = physical_state_root_path(naming_hierarchy.root.module)
        core_path = state_root[2:]
        root_rtl_module = rtl_identifier(module.name)
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
    # The complete public TopPhysicalABI is authoritative. Legacy protocol
    # spellings above describe internal packed roots and may differ when a
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
    module: Module,
    accepted: expr.Expression,
    clock: str | None = None,
) -> expr.Expression:
    """Gate the accepted schedule with the reset consumed by emitted state.

    ``module`` is the final backend-local component: synchronized-release roots
    consume their conditioner, while closed children consume the already
    conditioned native-release reset supplied through their component ABI.
    """

    if not module.clock_domains:
        return accepted
    domain = _module_domain(module, clock)
    bit = BitType()
    origin = getattr(accepted, "origin", None)
    deasserted = expr.Binary(
        expr.BinaryOperator.EQUAL,
        expr.InputRef(
            _effective_reset_signal(module, _identifier, domain.clock),
            bit,
            origin=origin,
        ),
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
    """Return the typed expression for one already-frozen safety verification observation."""

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
                else _formal_rule_fire_reset(module, accepted, group.domain)
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
        deferred_rule_outputs: dict[str, str] = {}
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
                    deferred_rule_outputs[output_map[binding.semantic_binding_id]] = (
                        binding.ref.local_semantic_id
                    )
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
            rule_domains = {
                rule_fire_observation_id(group.rule_name): group.domain
                for group in transformed.resolved_transition.action_groups
            } if transformed.resolved_transition is not None else {}
            transformed = replace(
                transformed,
                assignments=tuple(
                    replace(
                        assignment,
                        expression=_formal_rule_fire_reset(
                            reset_component,
                            assignment.expression,
                            rule_domains.get(
                                deferred_rule_outputs[assignment.target.name]
                            ),
                        ),
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
    formal_module_name = _identifier(formal_module.name)
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
        for observation in block.access_observations:
            if semantic_id == observation.read_hit_id:
                return ir_csr.csr_read_hit_port_name(observation)
            if semantic_id == observation.write_hit_id:
                return ir_csr.csr_observation_write_hit_port_name(observation)
            if semantic_id == observation.write_value_id:
                return ir_csr.csr_observation_write_value_port_name(observation)
            if semantic_id == observation.value_id:
                return ir_csr.csr_observation_value_port_name(observation)
        for register in block.registers:
            for event in register.events:
                if semantic_id == event.semantic_value_id:
                    return ir_csr.csr_event_port_name(event)
        for view in block.split_views:
            if semantic_id == view.semantic_value_id:
                return ir_csr.csr_split_port_name(block, view)
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
    if module.clock is None and len(module.clock_domains) > 1:
        if (
            len(module.assignments) != len(module.outputs)
            or assigned_names != {port.name for port in module.outputs}
        ):
            raise SystemVerilogEmissionError(
                "multi-clock pipeline emission requires every output to have "
                "one assignment"
            )
        declarations, sequential, render = _embedded_staging_emission(module)
        if not sequential:
            raise SystemVerilogEmissionError(
                "multi-clock sequential datapath requires a domain-qualified "
                "pipeline"
            )
        assignments = [
            f"  assign {_identifier(assignment.target.name)} = "
            f"{render(assignment.expression)};"
            for assignment in module.assignments
        ]
        return _module(
            module,
            [
                *_clock_reset_port_declarations(module),
                *(_port_declaration(port) for port in module.inputs),
                *(_port_declaration(port) for port in module.outputs),
            ],
            [*declarations, *sequential, *assignments],
        )
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
    aliases = ExpressionAliasMap(
        (node, _staged_signal_name(node, _staged_count(node)))
        for node in staged.values()
    )
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
    materialized_aliases = ExpressionAliasMap(
        (item.expression, item.name) for item in stage_materialized
    )

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
    aliases: ExpressionAliasMap,
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
    declarations, materialized, render = _materialized_emission(module)
    lines = [
        f"  assign {_assignment_name(assignment)} = "
        f"{render(assignment.expression)};"
        for assignment in module.assignments
    ]
    return _module(module, ports, [*declarations, *materialized, *lines])


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
        "  always_comb begin",
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

    if staged and not module.clock_domains:
        raise SystemVerilogEmissionError(
            "staged hierarchical component requires clock and reset"
        )

    aliases = ExpressionAliasMap()
    declarations: list[str] = []
    resets: dict[str, list[str]] = {}
    updates: dict[str, list[str]] = {}
    for value in staged:
        value_domain = (
            value.domain if isinstance(value, expr.Pipeline) else module.clock
        )
        if value_domain is None:
            raise SystemVerilogEmissionError(
                "delay in a multi-clock module requires domain-qualified "
                "lowering before direct emission"
            )
        domain_resets = resets.setdefault(value_domain, [])
        domain_updates = updates.setdefault(value_domain, [])
        count = value.cycles if isinstance(value, expr.Delay) else value.stages
        kind = "delay" if isinstance(value, expr.Delay) else "pipeline"
        signed = " signed" if isinstance(value.type, (SIntType, FixedType)) else ""
        for index in range(1, count + 1):
            name = local_names.stage(kind, value.instance, index)
            declarations.append(
                f"  logic{signed} {_range(_width(value.type))}{name};"
            )
            domain_resets.append(f"      {name} <= '0;")
        physical_input = _replace_materialized(value.expression, aliases)
        domain_updates.append(
            f"      {local_names.stage(kind, value.instance, 1)} <= {_expression(physical_input)};"
        )
        for index in range(2, count + 1):
            domain_updates.append(
                f"      {local_names.stage(kind, value.instance, index)} <= "
                f"{local_names.stage(kind, value.instance, index - 1)};"
            )
        aliases[value] = local_names.stage(kind, value.instance, count)

    sequential: list[str] = []
    for domain in module.clock_domains:
        if domain.clock not in updates:
            continue
        sequential.extend((
            f"  always_ff @({_clock_event(module, _identifier, domain.clock)}) begin",
            f"    if ({_reset_asserted(module, _identifier, domain.clock)}) begin",
            *resets[domain.clock],
            "    end else begin",
            *updates[domain.clock],
            "    end",
            "  end",
        ))

    # Rules and sequential modules historically used this staging renderer
    # directly instead of the ordinary combinational materialization path.
    # Compact functional regions are nevertheless ordinary combinational
    # values and must have the same statement-owned lowering in every route,
    # including register reset values.
    region_logic: list[str] = []
    region_items = tuple(
        item
        for item in _materialization_plan(module, local_names)
        if isinstance(item.expression, expr.FunctionalRegion)
    )
    region_names = tuple(item.name for item in region_items)
    for item in region_items:
        declarations.append(
            f"  logic {_range(_width(item.expression.type))}{item.name};"
        )
        # The general DAG planner may discover a region consumer before a
        # compact region nested below one of its invariant captures.  Publish
        # the complete producer map before rendering any owner so the nested
        # statement-owned value is always replaced by its exact-width alias.
        aliases[item.expression] = item.name
    for item in region_items:
        region = _replace_materialized(
            item.expression,
            aliases,
            keep=item.expression,
            rewrite_region_owned=True,
        )
        assert isinstance(region, expr.FunctionalRegion)
        replication = _functional_region_replication(region)
        if replication is not None:
            region_logic.append(f"  assign {item.name} = {replication};")
            continue
        plan = _functional_region_plan(
            region,
            item.name,
            reserved_names=region_names,
        )
        region_declarations, region_statements = _functional_region_rendering(
            region,
            plan,
            declaration_indent="  ",
            statement_indent="    ",
        )
        declarations.extend(region_declarations)
        region_logic.extend(("  always_comb begin", *region_statements, "  end"))

    def render(value: expr.Expression) -> str:
        physical = _instance_expression(module, value, local_names)
        return _expression(_replace_materialized(physical, aliases))

    return declarations, [*region_logic, *sequential], render


def _append_rule_state(
    module: Module,
    declarations: list[str],
    logic: list[str],
    render: Callable[[expr.Expression], str],
    *,
    compact_reset: bool = False,
) -> None:
    """Append classifier-approved scalar register/rule state to a module body."""

    if module.registers and not module.clock_domains:
        raise SystemVerilogEmissionError("direct rule emission requires clock/reset")
    ordered_rules = _ordered_rules(module)
    declarations.extend(
        _logic_declaration(
            rtl_register_state_identifier(register.name), register.type
        )
        for register in module.registers
    )
    for register in module.registers:
        if register.domain is None:
            raise SystemVerilogEmissionError(
                f"register '{register.name}' has no resolved clock domain"
            )
        writers = tuple(
            (rule, action)
            for rule in ordered_rules
            for action in rule.actions
            if action.target.name == register.name and rule.domain == register.domain
        )
        default = next(
            (
                assignment.expression
                for assignment in module.next_assignments
                if assignment.target.name == register.name
            ),
            None,
        )
        logic.append(
            f"  always_ff @({_clock_event(module, _identifier, register.domain)}) begin"
        )
        if compact_reset:
            logic.extend((
                f"    if ({_reset_asserted(module, _identifier, register.domain)}) "
                f"{_identifier(register.name)} <= {render(register.initial)};",
                "    else begin",
            ))
        else:
            logic.extend((
                f"    if ({_reset_asserted(module, _identifier, register.domain)}) begin",
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
    if not module.clock_domains:
        raise SystemVerilogEmissionError("direct rule emission requires clock/reset")
    ports = [
        *(
            item
            for domain in module.clock_domains
            for item in (
                f"input logic {_identifier(domain.clock)}",
                f"input logic {_identifier(domain.reset)}",
            )
        ),
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
    blocks = module.csr_blocks
    if not blocks:
        raise SystemVerilogEmissionError("direct CSR emission requires a block")
    if any(block.domain is None or block.reset is None for block in blocks):
        raise SystemVerilogEmissionError(
            "direct CSR emission requires an explicitly resolved physical domain"
        )
    physical_domains = {(block.domain, block.reset) for block in blocks}
    if len(physical_domains) != 1:
        raise SystemVerilogEmissionError(
            "direct CSR emission requires all blocks to share one physical domain"
        )
    csr_clock = blocks[0].domain
    assert csr_clock is not None
    access_observations = {
        block.name: ir_csr.derived_access_observations(
            block,
            module_identity=module.name,
            block_ordinal=index,
            clock_domain=block.domain,
        )
        for index, block in enumerate(blocks)
    }
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
                for block in blocks for binding in block.state_bindings
            ),
            *(
                _logic_port("output", ir_csr.csr_read_hit_port_name(binding),
                            BitType())
                for block in blocks
                for binding in access_observations[block.name]
            ),
            *(
                _logic_port(
                    "output",
                    ir_csr.csr_observation_write_hit_port_name(binding),
                    BitType(),
                )
                for block in blocks
                for binding in access_observations[block.name]
            ),
            *(
                _logic_port(
                    "output",
                    ir_csr.csr_observation_write_value_port_name(binding),
                    binding.canonical_type,
                )
                for block in blocks
                for binding in access_observations[block.name]
            ),
            *(
                _logic_port(
                    "output",
                    ir_csr.csr_observation_value_port_name(binding),
                    binding.canonical_type,
                )
                for block in blocks
                for binding in access_observations[block.name]
            ),
            *(
                _logic_port("output", ir_csr.csr_event_port_name(binding),
                            binding.canonical_type)
                for block in blocks for register in block.registers
                for binding in register.events
            ),
            *(
                _logic_port("output", ir_csr.csr_split_port_name(block, view),
                            view.canonical_type)
                for block in blocks for view in block.split_views
            ),
        ]
    ports = [
        *(
            item
            for domain in module.clock_domains
            for item in (
                f"input logic {_identifier(domain.clock)}",
                f"input logic {_identifier(domain.reset)}",
            )
        ),
        *(_port_declaration(port) for port in user_ports),
        *(_logic_port("input", name, type_)
          for name, type_ in access.input_types),
        *(_logic_port("output", name, type_)
          for name, type_ in access.output_types),
        *internal_ports,
    ]
    stored = tuple(
        (block, register, field)
        for block in blocks
        for register in block.registers
        for field in register.fields
        if ir_csr.access_owns_state(field.access)
    )
    lines = [
        *(
            f"  logic {_range(field.width)}{_csr_field_name(block, register, field)};"
            for block, register, field in stored
        ),
        f"  always_ff @({_clock_event(module, _identifier, csr_clock)}) begin",
        f"    if ({_reset_asserted(module, _identifier, csr_clock)}) begin",
        *(
            f"      {_csr_field_name(block, register, field)} <= "
            f"{field.width}'d{field.reset};"
            for block, register, field in stored
        ),
        "    end else begin",
    ]
    for block, register, field in stored:
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
    for block in blocks:
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
        for block in blocks for register in block.registers
    )
    lines.extend(
        (
            f"    {access.ready_port} = ({access.read_port} || {access.write_port}) && ({addresses});",
            "  end",
        )
    )
    for block, register, field in stored:
        if (
            field.binding is not None
            and field.binding.kind is ir_csr.CsrBindingKind.COMMAND
        ):
            lines.append(
                f"  assign {_identifier(field.binding.signal)} = "
                f"{_csr_field_name(block, register, field)};"
            )
    fields_by_identity = {
        field.identity: (block, register, field)
        for block in blocks
        for register in block.registers
        for field in register.fields
    }
    if expose_internal_abi:
        for block in blocks:
            for binding in block.state_bindings:
                _, register, field = fields_by_identity[binding.csr_field_id]
                lines.append(
                    f"  assign {ir_csr.csr_state_port_name(binding)} = "
                    f"{_csr_field_name(block, register, field)};"
                )
            for observation in access_observations[block.name]:
                _, register, field = fields_by_identity[observation.csr_field_id]
                address = block.base_address + register.offset
                if (
                    field.access is ir_csr.CsrAccess.READ_ONLY
                    and field.binding is not None
                    and field.binding.kind is ir_csr.CsrBindingKind.STATUS
                ):
                    observed_value = _identifier(field.binding.signal)
                elif field.access is ir_csr.CsrAccess.READ_ONLY:
                    observed_value = f"{field.width}'d{field.reset}"
                else:
                    observed_value = _csr_field_name(block, register, field)
                lines.extend((
                    f"  assign {ir_csr.csr_read_hit_port_name(observation)} = "
                    f"{access.read_port} && {access.address_port} == 32'h{address:08x};",
                    f"  assign {ir_csr.csr_observation_write_hit_port_name(observation)} = "
                    f"{access.write_port} && {access.address_port} == 32'h{address:08x};",
                    f"  assign {ir_csr.csr_observation_write_value_port_name(observation)} = "
                    f"{_slice(access.write_data_port, field.msb, field.lsb)};",
                    f"  assign {ir_csr.csr_observation_value_port_name(observation)} = "
                    f"{observed_value};",
                ))
            for view in block.split_views:
                _, low_register, low_field = fields_by_identity[view.low_field_id]
                _, high_register, high_field = fields_by_identity[view.high_field_id]
                lines.append(
                    f"  assign {ir_csr.csr_split_port_name(block, view)} = "
                    f"{{{_csr_field_name(block, high_register, high_field)}, "
                    f"{_csr_field_name(block, low_register, low_field)}}};"
                )
    for block in blocks:
        for register in block.registers:
            address = block.base_address + register.offset
            for event in register.events:
                hit_port = (
                    access.write_port
                    if event.kind is ir_csr.CsrEventKind.WRITE
                    else access.read_port
                )
                hit = (
                    f"({hit_port} && {access.address_port} == "
                    f"32'h{address:08x})"
                )
                if event.kind is ir_csr.CsrEventKind.WRITE:
                    selected = _slice(
                        access.write_data_port, event.msb, event.lsb
                    )
                    value = f"({hit} ? {selected} : '0)"
                else:
                    value = hit
                lines.append(f"  assign {_identifier(event.signal)} = {value};")
                if expose_internal_abi:
                    lines.append(
                        f"  assign {ir_csr.csr_event_port_name(event)} = {value};"
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
    name = _identifier(interface.name)
    request_payload_signal = f"{name}_request_payload"
    request_valid_signal = f"{name}_request_valid"
    request_ready_signal = f"{name}_request_ready"
    response_payload_signal = f"{name}_response_payload"
    response_valid_signal = f"{name}_response_valid"
    response_ready_signal = f"{name}_response_ready"
    count_width = max(1, interface.max_outstanding.bit_length())
    id_width = _width(interface.id_type)
    request_payload_value = _expression(request_payload.expression)
    request_id = _struct_field_expression(
        request_payload_value, interface.request_type, interface.match_by
    )
    response_id = _struct_field_expression(
        response_payload_signal, interface.response_type, interface.match_by
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
        f"  assign {request_payload_signal} = {request_payload_value};",
        f"  assign {_identifier(response_output.target.name)} = "
        f"{response_payload_signal};",
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
        f"{_expression(request_valid.expression)} && {request_ready_signal} && "
        f"({name}_outstanding < {count_width}'d{max_count}) && "
        f"{name}_request_id_present;",
        f"    {name}_missing = {_reset_deasserted(module, _identifier)} && "
        f"{_expression(response_ready.expression)} && {response_valid_signal} && "
        f"({name}_outstanding != '0) && !{name}_response_id_present;",
        "  end",
        f"  assign {request_valid_signal} = {_reset_deasserted(module, _identifier)} && "
        f"({name}_outstanding < {count_width}'d{max_count}) && !{name}_duplicate "
        f"&& {_expression(request_valid.expression)};",
        f"  assign {response_ready_signal} = {_reset_deasserted(module, _identifier)} && "
        f"({name}_outstanding != '0) && !{name}_missing "
        f"&& {_expression(response_ready.expression)};",
        f"  assign {name}_request_transfer = {request_valid_signal} && "
        f"{request_ready_signal};",
        f"  assign {name}_response_transfer = {response_valid_signal} && "
        f"{response_ready_signal};",
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
    request_payload_signal = f"{name}_request_payload"
    request_valid_signal = f"{name}_request_valid"
    request_ready_signal = f"{name}_request_ready"
    response_payload_signal = f"{name}_response_payload"
    response_valid_signal = f"{name}_response_valid"
    response_ready_signal = f"{name}_response_ready"
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
            f"  assign {request_payload_signal} = {request_payload};",
            f"  assign {request_valid_signal} = "
            f"{_reset_deasserted(module, _identifier)} && "
            f"({name}_outstanding < {count_width}'d{maximum}) && "
            f"({request_valid});",
            f"  assign {name}_request_transfer = "
            f"{request_valid_signal} && {request_ready_signal};",
            f"  assign {response_ready_signal} = "
            f"{_reset_deasserted(module, _identifier)} && "
            f"({name}_outstanding != '0 || {name}_request_transfer) && "
            f"({response_ready});",
            f"  assign {name}_response_transfer = "
            f"{response_valid_signal} && {response_ready_signal};",
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
            f"  assign {request_ready_signal} = "
            f"{_reset_deasserted(module, _identifier)} && "
            f"({name}_outstanding < {count_width}'d{maximum}) && "
            f"({request_ready});",
            f"  assign {name}_request_transfer = "
            f"{request_valid_signal} && {request_ready_signal};",
            f"  assign {response_payload_signal} = {response_payload};",
            f"  assign {response_valid_signal} = "
            f"{_reset_deasserted(module, _identifier)} && "
            f"({name}_outstanding != '0 || {name}_request_transfer) && "
            f"({response_valid});",
            f"  assign {name}_response_transfer = "
            f"{response_valid_signal} && {response_ready_signal};",
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


def _compile_time_expression(expression: int | CompileTimeExpr) -> str:
    """Render exact binder arithmetic using Python-compatible floor semantics."""

    if isinstance(expression, int) and not isinstance(expression, bool):
        return str(expression)
    if not isinstance(expression, CompileTimeExpr):
        raise SystemVerilogEmissionError(
            "functional compile-time value is not an integer expression"
        )
    operator = expression.operator
    if operator is CompileTimeOperator.LITERAL:
        value = expression.operands[0]
        assert isinstance(value, int)
        return str(value)
    if operator is CompileTimeOperator.BINDER:
        binder = expression.operands[0]
        context = _CURRENT_FUNCTIONAL_EXPRESSION.get()
        rendered = None if context is None else context.binder(binder.identity)
        if rendered is None:
            raise SystemVerilogEmissionError(
                f"functional binder '{binder.display_name}' escaped its region"
            )
        return rendered
    operands = tuple(
        _compile_time_expression(
            CompileTimeExpr.ref(item)
            if isinstance(item, CompileTimeBinderRef)
            else item
        )
        for item in expression.operands
    )
    if operator is CompileTimeOperator.ADD:
        return f"(({operands[0]}) + ({operands[1]}))"
    if operator is CompileTimeOperator.SUBTRACT:
        return f"(({operands[0]}) - ({operands[1]}))"
    if operator is CompileTimeOperator.MULTIPLY:
        return f"(({operands[0]}) * ({operands[1]}))"
    if operator is CompileTimeOperator.NEGATE:
        return f"(-({operands[0]}))"
    quotient = f"(({operands[0]}) / ({operands[1]}))"
    remainder = f"(({operands[0]}) % ({operands[1]}))"
    floor = (
        f"({quotient} - ((({remainder}) != 0 && "
        f"((({operands[0]}) < 0) != (({operands[1]}) < 0))) ? 1 : 0))"
    )
    if operator is CompileTimeOperator.FLOOR_DIVIDE:
        return floor
    assert operator is CompileTimeOperator.MODULO
    return f"(({operands[0]}) - (({floor}) * ({operands[1]})))"


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
        if expression.port is not None:
            return (
                f"{_identifier(expression.memory)}_"
                f"{_identifier(expression.port)}_{expression.signal.value}"
            )
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
    if isinstance(expression, expr.FunctionalCaptureRef):
        context = _CURRENT_FUNCTIONAL_EXPRESSION.get()
        captured = None if context is None else context.capture(expression.identity)
        if captured is None:
            raise SystemVerilogEmissionError(
                f"functional capture '{expression.display_name}' escaped its region"
            )
        return _expression(captured)
    if isinstance(expression, expr.FunctionalValue):
        value = _compile_time_expression(expression.expression)
        return f"{_width(expression.type)}'($unsigned({value}))"
    if isinstance(expression, expr.FunctionalTableLookup):
        context = _CURRENT_FUNCTIONAL_EXPRESSION.get()
        temporary = None if context is None else context.table_temporary(expression)
        if temporary is None:
            raise SystemVerilogEmissionError(
                f"functional table lookup '{expression.table_name}' escaped its region"
            )
        return temporary
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
            def literal(value: int) -> str:
                return f"{work_width}'d{value}"

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
        return "{" + _comma_join(
            tuple(_expression(value) for _, value in expression.fields)
        ) + "}"
    if isinstance(expression, expr.TupleConstruct):
        return "{" + _comma_join(tuple(
            f"{_width(value.type)}'({_expression(value)})"
            for value in reversed(expression.elements)
        )) + "}"
    if isinstance(expression, expr.TupleProject):
        tuple_type = expression.expression.type
        if not isinstance(tuple_type, TupleType):
            raise SystemVerilogEmissionError(
                "tuple projection requires a structural tuple"
            )
        lsb = ir_packing.tuple_element_lsb(tuple_type, expression.index)
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
        rendered_vector = _expression(expression.expression)
        if isinstance(expression.index, int):
            lsb = ir_packing.vector_element_lsb(vector, expression.index)
            msb = lsb + element_width - 1
            return _slice(rendered_vector, msb, lsb)
        rendered_index = _compile_time_expression(expression.index)
        base = f"(32'({rendered_index}) * 32'd{element_width})"
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", rendered_vector):
            return f"{rendered_vector}[{base} +: {element_width}]"
        return f"{element_width}'(($unsigned({rendered_vector})) >> ({base}))"
    if isinstance(expression, expr.RuntimeIndex):
        vector = expression.expression.type
        if not isinstance(vector, VecType):
            raise SystemVerilogEmissionError("runtime vector index requires a vector")
        element_width = _width(vector.element_type)
        rendered_vector = _expression(expression.expression)
        rendered_index = _expression(expression.index)
        base = f"(32'({rendered_index}) * 32'd{element_width})"
        # Yosys does not accept an indexed part-select whose base is a compound
        # expression (notably a concatenation), even though Verilator does.
        # Keep the compact indexed select for a plain packed signal and lower a
        # compound base to the equivalent unsigned shift plus sized truncation.
        # This also avoids creating a backend-only temporary for expressions
        # nested inside specialized callable bodies.
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", rendered_vector):
            return f"{rendered_vector}[{base} +: {element_width}]"
        return f"{element_width}'(($unsigned({rendered_vector})) >> ({base}))"
    if isinstance(expression, expr.VectorUpdate):
        vector = expression.expression.type
        if not isinstance(vector, VecType):
            raise SystemVerilogEmissionError("vector update requires a vector")
        total_width = _width(vector)
        element_width = _width(vector.element_type)
        rendered_vector = _expression(expression.expression)
        rendered_index = _expression(expression.index)
        rendered_value = _expression(expression.value)
        base = f"(32'({rendered_index}) * 32'd{element_width})"
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
        return "{" + _comma_join(tuple(
            f"{_width(operand.type)}'({_expression(operand)})"
            for operand in expression.operands
        )) + "}"
    if isinstance(expression, expr.VectorConcat):
        if len(expression.operands) < 2:
            raise SystemVerilogEmissionError(
                "typed vector concat requires at least two operands"
            )
        return "{" + _comma_join(tuple(
            f"{_width(operand.type)}'({_expression(operand)})"
            for operand in reversed(expression.operands)
        )) + "}"
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
        raise SystemVerilogEmissionError(
            "functional region requires statement-based emission"
        )
    if isinstance(expression, (expr.Generate, expr.Map)):
        if isinstance(expression.type, VecType):
            # Generate/map are semantically unrolled vectors by this stage.
            # SystemVerilog concatenations list the MSB first, whereas ZLang
            # indexed aggregates place element zero at the LSB.
            return "{" + _comma_join(tuple(
                _expression(element) for element in reversed(expression.elements)
            )) + "}"
        return _balanced_expression(expression.elements)
    if isinstance(expression, expr.Call):
        arguments = _comma_join(tuple(
            _expression(item) for item in expression.arguments
        ))
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


def _comma_join(items: tuple[str, ...]) -> str:
    """Keep ordinary RTL compact while bounding lexer work for giant DAGs."""

    separator = ",\n" if sum(len(item) for item in items) > 16_384 else ", "
    return separator.join(items)


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


def _functional_region_objects(value: object) -> tuple[expr.FunctionalRegion, ...]:
    """Return compact regions in deterministic dependency-first order.

    A region template may itself consume another compact region.  Walk every
    owned field and append the owner only after its dependencies so no nested
    region can leak into the scalar expression renderer.
    """

    return tuple(
        node.region for node in _functional_region_composition_plan(value).nodes
    )


def _functional_region_composition_plan(
    value: object,
) -> FunctionalRegionCompositionPlan:
    """Build the lexical region DAG without expanding virtual elements."""

    ordered: list[FunctionalRegionCompositionNode] = []
    state: dict[int, tuple[expr.FunctionalRegion, int]] = {}
    visited_expressions: dict[
        tuple[int, bool], expr.Expression
    ] = {}

    def free_binders(item: object, bound: frozenset[str]) -> frozenset[str]:
        if isinstance(item, CompileTimeBinderRef):
            return frozenset() if item.identity in bound else frozenset((item.identity,))
        if isinstance(item, expr.FunctionalRegion):
            owned = bound | frozenset((item.binder.identity,))
            template = free_binders(item.template, owned)
            tables = frozenset().union(
                *(
                    free_binders(table.values, owned)
                    for table in item.tables
                ),
                frozenset(),
            )
            captures = frozenset().union(
                *(
                    free_binders(captured, bound)
                    for _, captured in item.captures
                ),
                frozenset(),
            )
            return template | tables | captures
        if isinstance(item, tuple):
            return frozenset().union(
                *(free_binders(child, bound) for child in item),
                frozenset(),
            )
        if is_dataclass(item) and not isinstance(item, type):
            return frozenset().union(
                *(
                    free_binders(getattr(item, descriptor.name), bound)
                    for descriptor in fields(item)
                    if descriptor.name not in {"type", "origin", "source_origin"}
                ),
                frozenset(),
            )
        return frozenset()

    def dependencies(
        item: object,
        *,
        owner: expr.FunctionalRegion,
        region_template: bool = False,
    ) -> tuple[expr.FunctionalRegion, ...]:
        result: list[expr.FunctionalRegion] = []
        seen: set[int] = set()
        seen_expressions: dict[tuple[int, bool], expr.Expression] = {}

        def collect(current: object, *, template: bool) -> None:
            if isinstance(current, expr.FunctionalRegion):
                if current is owner or id(current) in seen:
                    return
                seen.add(id(current))
                result.append(current)
                return
            if isinstance(current, expr.Expression):
                key = (id(current), template)
                previous = seen_expressions.get(key)
                if previous is current:
                    return
                seen_expressions[key] = current
                if (
                    template
                    and isinstance(current, expr.Reduce)
                    and isinstance(current.collection, expr.FunctionalRegion)
                    and current.operator
                    in {
                        expr.ReductionOperator.ADD,
                        expr.ReductionOperator.BIT_AND,
                        expr.ReductionOperator.BIT_OR,
                        expr.ReductionOperator.BIT_XOR,
                    }
                ):
                    return
                for child in _expression_children(current):
                    collect(child, template=template)
                return
            if isinstance(current, tuple):
                for child in current:
                    collect(child, template=template)

        collect(item, template=region_template)
        return tuple(result)

    def visit(item: object, *, region_template: bool = False) -> None:
        if isinstance(item, expr.FunctionalRegion):
            status = state.get(id(item))
            if status is not None and status[0] is item:
                if status[1] == 1:
                    raise SystemVerilogEmissionError(
                        "functional region dependency graph contains a cycle"
                    )
                return
            state[id(item)] = (item, 1)
            children = dependencies(
                item.template,
                owner=item,
                region_template=True,
            )
            children += tuple(
                child
                for table in item.tables
                for child in dependencies(table.values, owner=item)
            )
            children += tuple(
                child
                for _, captured in item.captures
                for child in dependencies(captured, owner=item)
            )
            unique_children: list[expr.FunctionalRegion] = []
            child_ids: set[int] = set()
            for child in children:
                if id(child) not in child_ids:
                    child_ids.add(id(child))
                    unique_children.append(child)
                    visit(child)
            identity = expression_semantic_identity(item)
            ordered.append(
                FunctionalRegionCompositionNode(
                    region=item,
                    identity=identity,
                    dependencies=tuple(
                        expression_semantic_identity(child)
                        for child in unique_children
                    ),
                    free_binders=tuple(sorted(free_binders(item, frozenset()))),
                )
            )
            state[id(item)] = (item, 2)
            return
        if isinstance(item, expr.Expression):
            expression_key = (id(item), region_template)
            previous = visited_expressions.get(expression_key)
            if previous is item:
                return
            visited_expressions[expression_key] = item
            if (
                region_template
                and isinstance(item, expr.Reduce)
                and isinstance(item.collection, expr.FunctionalRegion)
                and item.operator
                in {
                    expr.ReductionOperator.ADD,
                    expr.ReductionOperator.BIT_AND,
                    expr.ReductionOperator.BIT_OR,
                    expr.ReductionOperator.BIT_XOR,
                }
            ):
                # The owning region renders this reduction under its current
                # binder scope.  It is not an independently hoistable node.
                return
            for child in _expression_children(item):
                visit(child, region_template=region_template)
            return
        if isinstance(item, tuple):
            for child in item:
                visit(child)

    visit(value)
    return FunctionalRegionCompositionPlan(tuple(ordered))


def _functional_table_lookups(value: object) -> tuple[expr.FunctionalTableLookup, ...]:
    found: list[expr.FunctionalTableLookup] = []
    seen: set[expr.FunctionalTableLookup] = set()

    def visit(item: object) -> None:
        if isinstance(item, expr.FunctionalTableLookup):
            if item not in seen:
                seen.add(item)
                found.append(item)
            return
        if isinstance(item, expr.FunctionalRegion):
            return
        if isinstance(item, expr.Expression):
            for child in _expression_children(item):
                visit(child)
            # Compile-time VectorIndex indices are not Expression children.
            if isinstance(item, expr.VectorIndex) and isinstance(
                item.index, CompileTimeExpr
            ):
                visit(item.index)
            return
        if isinstance(item, tuple):
            for child in item:
                visit(child)
            return
        if is_dataclass(item) and not isinstance(item, type):
            for descriptor in fields(item):
                if descriptor.name not in {"type", "origin", "source_origin"}:
                    visit(getattr(item, descriptor.name))

    visit(value)
    return tuple(found)


def _functional_binder_dependent(value: object, identity: str) -> bool:
    if isinstance(value, CompileTimeBinderRef):
        return value.identity == identity
    if isinstance(value, expr.FunctionalRegion):
        # A nested region owns its template binder, but its closed-over values
        # can still depend on the enclosing region.  Treating every nested
        # region as invariant incorrectly selects replication/materialization
        # and loses the explicit outer lowering boundary.
        return _functional_binder_dependent(
            value.template, identity
        ) or any(
            _functional_binder_dependent(item, identity)
            for table in value.tables
            for item in table.values
        ) or any(
            _functional_binder_dependent(captured, identity)
            for _, captured in value.captures
        )
    if isinstance(value, tuple):
        return any(_functional_binder_dependent(item, identity) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        return any(
            _functional_binder_dependent(getattr(value, descriptor.name), identity)
            for descriptor in fields(value)
            if descriptor.name not in {"type", "origin", "source_origin"}
        )
    return False


def _compact_bitwise_reductions(value: object) -> tuple[expr.Reduce, ...]:
    """Find outermost compact reductions that have an exact loop lowering."""

    found: list[expr.Reduce] = []
    seen: set[expr.Reduce] = set()

    def visit(item: object) -> None:
        if (
            isinstance(item, expr.Reduce)
            and isinstance(item.collection, expr.FunctionalRegion)
            and item.operator
            in {
                expr.ReductionOperator.ADD,
                expr.ReductionOperator.BIT_AND,
                expr.ReductionOperator.BIT_OR,
                expr.ReductionOperator.BIT_XOR,
            }
        ):
            if item not in seen:
                seen.add(item)
                found.append(item)
            return
        if isinstance(item, expr.FunctionalRegion):
            return
        if isinstance(item, tuple):
            for child in item:
                visit(child)
            return
        if is_dataclass(item) and not isinstance(item, type):
            for descriptor in fields(item):
                if descriptor.name not in {"type", "origin", "source_origin"}:
                    visit(getattr(item, descriptor.name))

    visit(value)
    return tuple(found)


def _contains_compact_bitwise_reduction(value: object) -> bool:
    return bool(_compact_bitwise_reductions(value))


def _functional_reduction_plan(
    reduction: expr.Reduce,
    *,
    owner_identity: str,
    ordinal: int,
    used: set[str],
) -> _FunctionalReductionEmissionPlan:
    region = reduction.collection
    assert isinstance(region, expr.FunctionalRegion)
    identity = expression_semantic_identity(reduction)
    accumulator = allocate_private_rtl_identifier(
        f"region_reduce_{identity[:10]}",
        semantic_identity=f"{owner_identity}:reduction:{ordinal}:{identity}:value",
        used=used,
    )
    loop_variable = allocate_private_rtl_identifier(
        f"region_reduce_i_{identity[:10]}",
        semantic_identity=f"{owner_identity}:reduction:{ordinal}:{identity}:binder",
        used=used,
    )
    table_temporaries: list[tuple[expr.FunctionalTableLookup, str]] = []
    for table_ordinal, lookup in enumerate(
        _functional_table_lookups(region.template)
    ):
        name = allocate_private_rtl_identifier(
            f"region_reduce_table_{identity[:10]}_{table_ordinal}",
            semantic_identity=(
                f"{owner_identity}:reduction:{ordinal}:{identity}:"
                f"table:{lookup.table_name}"
            ),
            used=used,
        )
        table_temporaries.append((lookup, name))
    children = tuple(
        _functional_reduction_plan(
            child,
            owner_identity=f"{owner_identity}:reduction:{ordinal}:{identity}",
            ordinal=child_ordinal,
            used=used,
        )
        for child_ordinal, child in enumerate(
            _compact_bitwise_reductions(region.template)
        )
    )
    return _FunctionalReductionEmissionPlan(
        reduction,
        accumulator,
        loop_variable,
        tuple(table_temporaries),
        children,
    )


def _resolve_functional_scatter_captures(
    value: object,
    captures: dict[str, expr.Expression],
    *,
    resolving: frozenset[str] = frozenset(),
) -> object:
    """Inline a closed region capture graph for structural recognition."""

    if isinstance(value, expr.FunctionalCaptureRef):
        if value.identity in resolving:
            raise SystemVerilogEmissionError(
                f"functional capture '{value.display_name}' is recursive"
            )
        captured = captures.get(value.identity)
        if captured is None or captured.type != value.type:
            raise SystemVerilogEmissionError(
                f"functional capture '{value.display_name}' is not bound exactly"
            )
        return _resolve_functional_scatter_captures(
            captured,
            captures,
            resolving=resolving | {value.identity},
        )
    if isinstance(value, tuple):
        return tuple(
            _resolve_functional_scatter_captures(
                item, captures, resolving=resolving
            )
            for item in value
        )
    if is_dataclass(value) and not isinstance(value, type):
        updates = {
            descriptor.name: _resolve_functional_scatter_captures(
                getattr(value, descriptor.name),
                captures,
                resolving=resolving,
            )
            for descriptor in fields(value)
            if descriptor.init
            and descriptor.name not in {"type", "origin", "source_origin"}
        }
        return replace(value, **updates) if updates else value
    return value


def _functional_scatter_shape(
    region: expr.FunctionalRegion,
) -> tuple[
    tuple[expr.FunctionalRegion, ...],
    tuple[_FunctionalScatterEntry, ...],
] | None:
    """Recognize an exact OR-of-addressed-candidates destination decode."""

    dimensions: list[expr.FunctionalRegion] = []
    current: expr.Expression = region.template
    leaves: tuple[expr.Expression, ...] | None = None
    while isinstance(current, expr.Reduce) and (
        current.operator is expr.ReductionOperator.BIT_OR
    ):
        if isinstance(current.collection, expr.FunctionalRegion):
            child = current.collection
            dimensions.append(child)
            current = child.template
            continue
        if isinstance(current.collection, (expr.Generate, expr.Map)):
            leaves = current.collection.elements
        break
    if leaves is None:
        leaves = (current,)
    if not leaves or any(not isinstance(leaf, expr.Mux) for leaf in leaves):
        return None

    captures = {
        reference.identity: value
        for owner in (region, *dimensions)
        for reference, value in owner.captures
    }
    if not isinstance(region.type.element_type, (BitType, BitsType, UIntType)):
        return None

    def match(leaf: expr.Expression) -> _FunctionalScatterEntry | None:
        assert isinstance(leaf, expr.Mux)
        try:
            resolved = tuple(
                _resolve_functional_scatter_captures(item, captures)
                for item in (leaf.condition, leaf.when_true, leaf.when_false)
            )
        except (TypeError, ValueError, SystemVerilogEmissionError):
            return None
        if not all(isinstance(item, expr.Expression) for item in resolved):
            return None
        condition, when_true, when_false = resolved
        assert isinstance(condition, expr.Expression)
        assert isinstance(when_true, expr.Expression)
        assert isinstance(when_false, expr.Expression)
        if not isinstance(when_false, expr.Constant) or when_false.value != 0:
            return None
        if when_true.type != region.type.element_type:
            return None

        factors: list[expr.Expression] = []

        def flatten_and(value: expr.Expression) -> None:
            if (
                isinstance(value, expr.Binary)
                and value.operator is expr.BinaryOperator.BIT_AND
                and isinstance(value.type, BitType)
            ):
                flatten_and(value.left)
                flatten_and(value.right)
                return
            factors.append(value)

        flatten_and(condition)
        destination: expr.FunctionalValue | None = None
        address: expr.Expression | None = None
        equality_index: int | None = None
        for index, factor in enumerate(factors):
            if not (
                isinstance(factor, expr.Binary)
                and factor.operator is expr.BinaryOperator.EQUAL
            ):
                continue
            for candidate_destination, candidate_address in (
                (factor.left, factor.right),
                (factor.right, factor.left),
            ):
                if not isinstance(candidate_destination, expr.FunctionalValue):
                    continue
                compile_time = candidate_destination.expression
                if not (
                    isinstance(compile_time, CompileTimeExpr)
                    and compile_time.operator is CompileTimeOperator.BINDER
                    and isinstance(compile_time.operands[0], CompileTimeBinderRef)
                    and compile_time.operands[0].identity == region.binder.identity
                ):
                    continue
                destination = candidate_destination
                address = candidate_address
                equality_index = index
                break
            if destination is not None:
                break
        if destination is None or address is None or equality_index is None:
            return None
        if region.binder.start != 0 or destination.type != address.type:
            return None
        if not isinstance(address.type, (BitsType, UIntType)):
            return None
        if any(
            _functional_binder_dependent(item, region.binder.identity)
            for item in (address, when_true)
        ):
            return None

        enabled_factors = tuple(
            factor
            for index, factor in enumerate(factors)
            if index != equality_index
        )
        if not enabled_factors:
            enabled: expr.Expression = expr.Constant(1, BitType())
        else:
            enabled = enabled_factors[0]
            for factor in enabled_factors[1:]:
                if not isinstance(factor.type, BitType):
                    return None
                enabled = expr.Binary(
                    expr.BinaryOperator.BIT_AND,
                    enabled,
                    factor,
                    BitType(),
                    BitType(),
                )
        if _functional_binder_dependent(enabled, region.binder.identity):
            return None
        return _FunctionalScatterEntry(enabled, address, when_true)

    entries = tuple(match(leaf) for leaf in leaves)
    if any(entry is None for entry in entries):
        return None
    return tuple(dimensions), tuple(
        entry for entry in entries if entry is not None
    )


def _functional_scatter_plan(
    region: expr.FunctionalRegion,
    *,
    owner_identity: str,
    used: set[str],
) -> _FunctionalScatterEmissionPlan | None:
    shape = _functional_scatter_shape(region)
    if shape is None:
        return None
    regions, entries = shape
    dimensions = [
        _FunctionalScatterDimension(
            child,
            allocate_private_rtl_identifier(
                f"region_scatter_i_{ordinal}",
                semantic_identity=(
                    f"{owner_identity}:scatter:{ordinal}:{child.binder.identity}"
                ),
                used=used,
            ),
            (),
        )
        for ordinal, child in enumerate(regions)
    ]
    tables = {
        table.name: table for child in (region, *regions) for table in child.tables
    }
    table_temporaries: list[tuple[expr.FunctionalTableLookup, str]] = []
    for ordinal, lookup in enumerate(
        dict.fromkeys(
            lookup
            for entry in entries
            for root in (entry.enable, entry.address, entry.value)
            for lookup in _functional_table_lookups(root)
        )
    ):
        if lookup.table_name not in tables:
            return None
        temporary = allocate_private_rtl_identifier(
            f"region_scatter_table_{ordinal}",
            semantic_identity=(
                f"{owner_identity}:scatter:table:{ordinal}:{lookup.table_name}"
            ),
            used=used,
        )
        table_temporaries.append((lookup, temporary))
    if table_temporaries and not dimensions:
        return None
    if dimensions:
        dimensions[-1] = replace(
            dimensions[-1], table_temporaries=tuple(table_temporaries)
        )
    return _FunctionalScatterEmissionPlan(
        tuple(dimensions),
        entries,
        allocate_private_rtl_identifier(
            "region_scatter_enable",
            semantic_identity=f"{owner_identity}:scatter:enable",
            used=used,
        ),
        allocate_private_rtl_identifier(
            "region_scatter_address",
            semantic_identity=f"{owner_identity}:scatter:address",
            used=used,
        ),
        allocate_private_rtl_identifier(
            "region_scatter_value",
            semantic_identity=f"{owner_identity}:scatter:value",
            used=used,
        ),
    )


def _functional_region_plan(
    region: expr.FunctionalRegion,
    result_name: str,
    *,
    reserved_names: tuple[str, ...] = (),
) -> FunctionalRegionEmissionPlan:
    identity = stable_digest(
        {
            "schema": FUNCTIONAL_REGION_EMISSION_SCHEMA,
            "expression": expression_semantic_identity(region),
            "result": result_name,
        }
    )
    prefix = identity[:10]
    used = set(reserved_names) | {result_name}
    scatter = _functional_scatter_plan(
        region,
        owner_identity=identity,
        used=used,
    )
    if scatter is not None:
        return FunctionalRegionEmissionPlan(
            identity=identity,
            result_name=result_name,
            loop_variable="",
            element_width=_width(region.type.element_type),
            table_temporaries=(),
            expression_temporaries=(),
            scatter=scatter,
        )
    loop_variable = allocate_private_rtl_identifier(
        f"region_i_{prefix}",
        semantic_identity=f"{identity}:binder:{region.binder.identity}",
        used=used,
    )
    reduction_temporaries = tuple(
        _functional_reduction_plan(
            reduction,
            owner_identity=identity,
            ordinal=ordinal,
            used=used,
        )
        for ordinal, reduction in enumerate(
            _compact_bitwise_reductions(region.template)
        )
    )
    materialized = [
        item
        for item in plan_materialization(
            (region.template,),
            reserved_names=tuple(used),
            generated_prefix=f"zlang_region_expr_{prefix}_",
            scope=f"functional_region:{identity}",
        )
        if not _contains_compact_bitwise_reduction(item.expression)
    ]
    used.update(item.name for item in materialized)
    selected = ExpressionAliasMap(
        (item.expression, item.name) for item in materialized
    )
    scalar_leaves = (
        expr.Constant,
        expr.InputRef,
        expr.ParameterRef,
        expr.RegisterRef,
        expr.InstanceOutputRef,
        expr.FunctionalCaptureRef,
        expr.FunctionalValue,
        expr.FunctionalTableLookup,
    )

    def partition(value: expr.Expression, *, root: bool = False) -> int:
        if value in selected:
            return 1
        effective_size = 1 + sum(
            partition(child) for child in _expression_children(value)
        )
        # Bound every procedural RHS independently of expression sharing.  A
        # small structural threshold is deliberate: exact-width casts and
        # signed comparisons can render far wider than their IR node count.
        if (
            not root
            and effective_size > 16
            and not isinstance(value, scalar_leaves)
            and not _contains_compact_bitwise_reduction(value)
        ):
            temporary = allocate_private_rtl_identifier(
                f"region_expr_{prefix}",
                semantic_identity=(
                    f"{identity}:expression:{expression_semantic_identity(value)}"
                ),
                used=used,
            )
            materialized.append(_MaterializedExpression(value, temporary))
            selected[value] = temporary
            return 1
        return effective_size

    partition(region.template, root=True)
    table_temporaries: list[tuple[expr.FunctionalTableLookup, str]] = []
    for index, lookup in enumerate(_functional_table_lookups(region.template)):
        temporary = allocate_private_rtl_identifier(
            f"region_table_{prefix}_{index}",
            semantic_identity=(
                f"{identity}:table:{lookup.table_name}:"
                f"{expression_semantic_identity(lookup)}"
            ),
            used=used,
        )
        table_temporaries.append((lookup, temporary))
    return FunctionalRegionEmissionPlan(
        identity=identity,
        result_name=result_name,
        loop_variable=loop_variable,
        element_width=_width(region.type.element_type),
        table_temporaries=tuple(table_temporaries),
        expression_temporaries=tuple(materialized),
        reduction_temporaries=reduction_temporaries,
    )


def _functional_region_rendering(
    region: expr.FunctionalRegion,
    plan: FunctionalRegionEmissionPlan,
    *,
    declaration_indent: str,
    statement_indent: str,
) -> tuple[list[str], list[str]]:
    """Render declarations and procedural statements for one region plan."""

    if plan.scatter is not None:
        return _functional_scatter_rendering(
            region,
            plan,
            declaration_indent=declaration_indent,
            statement_indent=statement_indent,
        )

    table_by_name = {table.name: table for table in region.tables}
    context = _FunctionalExpressionContext(
        binders=((region.binder.identity, plan.loop_variable),),
        captures=tuple(
            (reference.identity, value) for reference, value in region.captures
        ),
        table_temporaries=plan.table_temporaries,
    )
    declarations = [f"{declaration_indent}integer {plan.loop_variable};"]
    for lookup, temporary in plan.table_temporaries:
        signed = " signed" if isinstance(lookup.type, (SIntType, FixedType)) else ""
        declarations.append(
            f"{declaration_indent}logic{signed} {_range(_width(lookup.type))}{temporary};"
        )
    for item in plan.expression_temporaries:
        signed = (
            " signed" if isinstance(item.expression.type, (SIntType, FixedType)) else ""
        )
        declarations.append(
            f"{declaration_indent}logic{signed} "
            f"{_range(_width(item.expression.type))}{item.name};"
        )
    for reduction in plan.reduction_temporaries:
        declarations.extend(
            _functional_reduction_declarations(reduction, declaration_indent)
        )

    aliases = ExpressionAliasMap(
        (item.expression, item.name) for item in plan.expression_temporaries
    )
    aliases.update(
        (item.reduction, item.accumulator)
        for item in plan.reduction_temporaries
    )
    invariant: list[str] = []
    dependent: list[str] = []
    with _functional_expression_scope(context):
        for item in dependency_ordered_materialization(
            plan.expression_temporaries
        ):
            rewritten = _replace_materialized(
                item.expression,
                aliases,
                keep=item.expression,
            )
            assignment = f"{item.name} = {_expression(rewritten)};"
            target = (
                dependent
                if _functional_binder_dependent(
                    item.expression, region.binder.identity
                )
                else invariant
            )
            target.append(assignment)

        statements = [
            *(f"{statement_indent}{line}" for line in invariant),
            f"{statement_indent}{plan.result_name} = '0;",
            f"{statement_indent}for ({plan.loop_variable} = {region.binder.start}; "
            f"{plan.loop_variable} < {region.binder.stop}; "
            f"{plan.loop_variable} = {plan.loop_variable} + 1) begin",
        ]
        inner = statement_indent + "  "
        for lookup, temporary in plan.table_temporaries:
            table = table_by_name.get(lookup.table_name)
            if table is None:
                raise SystemVerilogEmissionError(
                    f"functional table '{lookup.table_name}' is not declared"
                )
            statements.append(
                f"{inner}case ({_compile_time_expression(lookup.index)})"
            )
            for offset, value in enumerate(table.values):
                statements.append(
                    f"{inner}  {table.start + offset}: {temporary} = "
                    f"{_expression(value)};"
                )
            statements.extend(
                (f"{inner}  default: {temporary} = '0;", f"{inner}endcase")
            )
        for reduction in plan.reduction_temporaries:
            statements.extend(
                _functional_reduction_statements(reduction, inner)
            )
        statements.extend(f"{inner}{line}" for line in dependent)
        rewritten_template = _replace_materialized(region.template, aliases)
        base = (
            f"(({plan.loop_variable} - {region.binder.start}) * "
            f"{plan.element_width})"
        )
        statements.append(
            f"{inner}{plan.result_name}[{base} +: {plan.element_width}] = "
            f"{_expression(rewritten_template)};"
        )
        statements.append(f"{statement_indent}end")
    return declarations, statements


def _functional_scatter_rendering(
    region: expr.FunctionalRegion,
    plan: FunctionalRegionEmissionPlan,
    *,
    declaration_indent: str,
    statement_indent: str,
) -> tuple[list[str], list[str]]:
    scatter = plan.scatter
    assert scatter is not None
    first_entry = scatter.entries[0]
    if any(
        entry.address.type != first_entry.address.type
        or entry.value.type != first_entry.value.type
        for entry in scatter.entries[1:]
    ):
        raise SystemVerilogEmissionError(
            "functional scatter entries do not share exact address/value types"
        )
    declarations: list[str] = []
    table_by_name = {
        table.name: table
        for owner in (region, *(dimension.region for dimension in scatter.dimensions))
        for table in owner.tables
    }
    for dimension in scatter.dimensions:
        declarations.append(
            f"{declaration_indent}integer {dimension.loop_variable};"
        )
        for lookup, temporary in dimension.table_temporaries:
            signed = (
                " signed"
                if isinstance(lookup.type, (SIntType, FixedType))
                else ""
            )
            declarations.append(
                f"{declaration_indent}logic{signed} "
                f"{_range(_width(lookup.type))}{temporary};"
            )
    declarations.extend(
        (
            f"{declaration_indent}logic {scatter.enable_temporary};",
            f"{declaration_indent}logic "
            f"{_range(_width(first_entry.address.type))}{scatter.address_temporary};",
            f"{declaration_indent}logic "
            f"{_range(_width(first_entry.value.type))}{scatter.value_temporary};",
        )
    )

    statements = [f"{statement_indent}{plan.result_name} = '0;"]

    def render_entry(entry: _FunctionalScatterEntry, indent: str) -> None:
        statements.extend(
            (
                f"{indent}{scatter.enable_temporary} = "
                f"{_expression(entry.enable)};",
                f"{indent}{scatter.address_temporary} = "
                f"{_expression(entry.address)};",
                f"{indent}{scatter.value_temporary} = "
                f"{_expression(entry.value)};",
            )
        )
        address = scatter.address_temporary
        enabled = scatter.enable_temporary
        value = scatter.value_temporary
        address_width = _width(entry.address.type)
        guard = (
            "1'b1"
            if region.type.length >= 1 << address_width
            else f"($unsigned({address}) < {address_width}'d{region.type.length})"
        )
        base = f"($unsigned({address}) * {plan.element_width})"
        destination = f"{plan.result_name}[{base} +: {plan.element_width}]"
        statements.extend(
            (
                f"{indent}if (({enabled}) && ({guard})) begin",
                f"{indent}  {destination} = "
                f"{plan.element_width}'($unsigned({destination})) | "
                f"{plan.element_width}'($unsigned({value}));",
                f"{indent}end",
            )
        )

    def render_dimension(
        ordinal: int,
        indent: str,
        parent: _FunctionalExpressionContext,
    ) -> None:
        dimension = scatter.dimensions[ordinal]
        owned = dimension.region
        context = _FunctionalExpressionContext(
            binders=(
                *parent.binders,
                (owned.binder.identity, dimension.loop_variable),
            ),
            captures=(
                *((reference.identity, value) for reference, value in owned.captures),
                *parent.captures,
            ),
            table_temporaries=(
                *dimension.table_temporaries,
                *parent.table_temporaries,
            ),
        )
        statements.extend(
            (
                f"{indent}for ({dimension.loop_variable} = {owned.binder.start}; "
                f"{dimension.loop_variable} < {owned.binder.stop}; "
                f"{dimension.loop_variable} = "
                f"{dimension.loop_variable} + 1) begin",
            )
        )
        inner = indent + "  "
        with _functional_expression_scope(context):
            for lookup, temporary in dimension.table_temporaries:
                table = table_by_name.get(lookup.table_name)
                if table is None:
                    raise SystemVerilogEmissionError(
                        f"functional table '{lookup.table_name}' is not declared"
                    )
                statements.append(
                    f"{inner}case ({_compile_time_expression(lookup.index)})"
                )
                for offset, value in enumerate(table.values):
                    statements.append(
                        f"{inner}  {table.start + offset}: {temporary} = "
                        f"{_expression(value)};"
                    )
                statements.extend(
                    (f"{inner}  default: {temporary} = '0;", f"{inner}endcase")
                )
            if ordinal + 1 < len(scatter.dimensions):
                render_dimension(ordinal + 1, inner, context)
            else:
                for entry in scatter.entries:
                    render_entry(entry, inner)
        statements.append(f"{indent}end")

    root_context = _FunctionalExpressionContext((), (), ())
    if scatter.dimensions:
        render_dimension(0, statement_indent, root_context)
    else:
        with _functional_expression_scope(root_context):
            for entry in scatter.entries:
                render_entry(entry, statement_indent)
    return declarations, statements


def _functional_reduction_declarations(
    plan: _FunctionalReductionEmissionPlan,
    indent: str,
) -> list[str]:
    signed = (
        " signed"
        if isinstance(plan.reduction.type, (SIntType, FixedType))
        else ""
    )
    declarations = [
        f"{indent}logic{signed} {_range(_width(plan.reduction.type))}"
        f"{plan.accumulator};",
        f"{indent}integer {plan.loop_variable};",
    ]
    for lookup, temporary in plan.table_temporaries:
        table_signed = (
            " signed" if isinstance(lookup.type, (SIntType, FixedType)) else ""
        )
        declarations.append(
            f"{indent}logic{table_signed} {_range(_width(lookup.type))}{temporary};"
        )
    for child in plan.children:
        declarations.extend(_functional_reduction_declarations(child, indent))
    return declarations


def _functional_reduction_statements(
    plan: _FunctionalReductionEmissionPlan,
    indent: str,
) -> list[str]:
    reduction = plan.reduction
    region = reduction.collection
    assert isinstance(region, expr.FunctionalRegion)
    parent = _CURRENT_FUNCTIONAL_EXPRESSION.get()
    if parent is None:
        raise SystemVerilogEmissionError(
            "functional reduction requires an enclosing region context"
        )
    table_by_name = {table.name: table for table in region.tables}
    context = _FunctionalExpressionContext(
        binders=(*parent.binders, (region.binder.identity, plan.loop_variable)),
        captures=(
            *((reference.identity, value) for reference, value in region.captures),
            *parent.captures,
        ),
        table_temporaries=(*plan.table_temporaries, *parent.table_temporaries),
    )
    identity = (
        "'1"
        if reduction.operator is expr.ReductionOperator.BIT_AND
        else "'0"
    )
    lines = [
        f"{indent}{plan.accumulator} = {identity};",
        f"{indent}for ({plan.loop_variable} = {region.binder.start}; "
        f"{plan.loop_variable} < {region.binder.stop}; "
        f"{plan.loop_variable} = {plan.loop_variable} + 1) begin",
    ]
    inner = indent + "  "
    with _functional_expression_scope(context):
        for lookup, temporary in plan.table_temporaries:
            table = table_by_name.get(lookup.table_name)
            if table is None:
                raise SystemVerilogEmissionError(
                    f"functional table '{lookup.table_name}' is not declared"
                )
            lines.append(f"{inner}case ({_compile_time_expression(lookup.index)})")
            for offset, value in enumerate(table.values):
                lines.append(
                    f"{inner}  {table.start + offset}: {temporary} = "
                    f"{_expression(value)};"
                )
            lines.extend(
                (f"{inner}  default: {temporary} = '0;", f"{inner}endcase")
            )
        for child in plan.children:
            lines.extend(_functional_reduction_statements(child, inner))
        aliases = {
            child.reduction: child.accumulator for child in plan.children
        }
        template = _replace_materialized(region.template, aliases)
        if reduction.operator is expr.ReductionOperator.ADD:
            combined = expr.Add(
                expr.InputRef(plan.accumulator, reduction.type),
                template,
                reduction.type,
            )
        else:
            operator = {
                expr.ReductionOperator.BIT_AND: expr.BinaryOperator.BIT_AND,
                expr.ReductionOperator.BIT_OR: expr.BinaryOperator.BIT_OR,
                expr.ReductionOperator.BIT_XOR: expr.BinaryOperator.BIT_XOR,
            }[reduction.operator]
            combined = expr.Binary(
                operator,
                expr.InputRef(plan.accumulator, reduction.type),
                template,
                reduction.type,
                reduction.type,
            )
        lines.append(f"{inner}{plan.accumulator} = {_expression(combined)};")
    lines.append(f"{indent}end")
    return lines


def _functional_region_replication(
    region: expr.FunctionalRegion,
) -> str | None:
    """Render a binder-independent region as one packed replication."""

    if _functional_binder_dependent(region.template, region.binder.identity):
        return None
    if _functional_table_lookups(region.template):
        return None
    context = _FunctionalExpressionContext(
        binders=(),
        captures=tuple(
            (reference.identity, value) for reference, value in region.captures
        ),
        table_temporaries=(),
    )
    with _functional_expression_scope(context):
        element = _expression(region.template)
    length = region.binder.stop - region.binder.start
    return "{" + f"{length}{{{_width(region.type.element_type)}'({element})}}" + "}"


def _function_region_emission(
    body: expr.Expression,
    *,
    reserved_names: tuple[str, ...],
) -> tuple[expr.Expression, list[str], list[str]]:
    """Materialize every region nested in one function body.

    A FunctionalRegion is a statement-owned value even when it appears below
    a mux, call argument, or aggregate constructor.  Function emission cannot
    delegate those nodes to the scalar expression renderer, so give each one
    a stable local and emit its procedural producer before the final result.
    """

    regions = _functional_region_objects(body)
    if not regions:
        return body, [], []
    used = set(reserved_names)
    aliases = ExpressionAliasMap()
    for region in regions:
        identity = expression_semantic_identity(region)
        aliases[region] = allocate_private_rtl_identifier(
            f"zlang_fn_region_{identity[:10]}",
            semantic_identity=(
                f"{FUNCTIONAL_REGION_EMISSION_SCHEMA}:function:{identity}"
            ),
            used=used,
        )
    declarations = [
        f"    logic{(' signed' if isinstance(region.type, (SIntType, FixedType)) else '')} "
        f"{_range(_width(region.type))}{aliases[region]};"
        for region in regions
    ]
    statements: list[str] = []
    for region in regions:
        rewritten = _replace_materialized(
            region,
            aliases,
            keep=region,
            rewrite_region_owned=True,
        )
        assert isinstance(rewritten, expr.FunctionalRegion)
        result_name = aliases[region]
        replication = _functional_region_replication(rewritten)
        if replication is not None:
            statements.append(f"    {result_name} = {replication};")
            continue
        plan = _functional_region_plan(
            rewritten,
            result_name,
            reserved_names=tuple((*reserved_names, *aliases.values())),
        )
        owned_declarations, owned_statements = _functional_region_rendering(
            rewritten,
            plan,
            declaration_indent="    ",
            statement_indent="    ",
        )
        declarations.extend(owned_declarations)
        statements.extend(owned_statements)
    rewritten_body = _replace_materialized(body, aliases)
    return rewritten_body, declarations, statements


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
        if isinstance(renamed_body, expr.FunctionalRegion):
            replication = _functional_region_replication(renamed_body)
            if replication is not None:
                definitions.append(
                    f"  function automatic logic{return_signed} "
                    f"{_range(_width(function.return_type))}{name}("
                    f"{declaration});\n"
                    f"{identity_note}"
                    f"    {name} = {replication};\n"
                    "  endfunction"
                )
                continue
            region_plan = _functional_region_plan(
                renamed_body,
                name,
                reserved_names=tuple((*parameter_names.values(), name, *used)),
            )
            region_declarations, region_statements = _functional_region_rendering(
                renamed_body,
                region_plan,
                declaration_indent="    ",
                statement_indent="    ",
            )
            region_block = "\n".join(
                (*region_declarations, *region_statements)
            )
            definitions.append(
                f"  function automatic logic{return_signed} "
                f"{_range(_width(function.return_type))}{name}("
                f"{declaration});\n"
                f"{identity_note}"
                f"{region_block}\n"
                "  endfunction"
            )
            continue
        renamed_body, region_declarations, region_statements = (
            _function_region_emission(
                renamed_body,
                reserved_names=tuple((*parameter_names.values(), name, *used)),
            )
        )
        materialized = plan_materialization(
            (renamed_body,),
            reserved_names=(*parameter_names.values(), name),
            generated_prefix="zlang_fn_expr_",
        )
        aliases = ExpressionAliasMap(
            (item.expression, item.name) for item in materialized
        )

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
        local_block = "\n".join(
            (
                *region_declarations,
                *function_locals,
                *region_statements,
                *function_assignments,
            )
        )
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
    boundary = _CURRENT_TOP_BOUNDARY.get()
    name = (
        boundary.physical_module_name
        if boundary is not None and boundary.module_name == module.name
        else rtl_identifier(module.name)
    )
    return _named_module(name, ports, lines, typed_module=module)


def _identifier(name: str) -> str:
    boundary = _CURRENT_TOP_BOUNDARY.get()
    if boundary is not None:
        alias = boundary.alias(name)
        if alias is not None:
            return alias
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
