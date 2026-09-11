"""Emit the Milestone 0 typed IR as synthesizable Clash source."""

from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, fields, is_dataclass, replace

from zlang.ir import expressions as expr
from zlang.ir.callables import (
    CallableReachabilityError,
    reachable_module_callables,
)
from zlang.ir.functional import (
    lower_reduction,
    materialize_exact_reduction,
    materialize_functional_region,
)
from zlang.ir import csr as ir_csr
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
from zlang.ir.storage import MemoryCollision, MemoryResetPolicy, RomSignal
from zlang.ir.state import (
    FifoOccupancy,
    StateActionKind,
    StateResourceKind,
    action_activation_predicate_index,
    conditional_activation_predicates,
    conditional_actions,
    ordered_groups as ordered_state_groups,
    selection_regions,
)
from zlang.ir.formal import signal_bindings
from zlang.ir.formal_observations import (
    request_response_observation_id,
    rule_fire_observation_id,
)
from zlang.ir.hierarchy import HierarchyError, build_hierarchy_index
from zlang.ir.arbitration import ArbitrationPolicy, GrantScope
from zlang.ir.module import (
    Function,
    Module,
    Port,
    ProtocolEndpoint,
    default_selected_ir_identity,
)
from zlang.ir.module import PortDirection
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
from zlang.backend.module_features import (
    ModuleFeatureAccountingError,
    ModuleFeatureGroup,
    claims_for_groups,
    module_feature_inventory,
    unsupported_legacy_state_mix,
    validate_feature_claims,
)
from zlang.backend.clash.cdc import (
    CDCRendering,
    emit_cdc_module as _emit_cdc_subsystem,
)
from zlang.backend.clash.domain import (
    ClashDomainError as _ClashDomainError,
    domain_declaration as _render_domain_declaration,
    top_reset_expression as _render_top_reset_expression,
)
from zlang.backend.clash.hierarchy import (
    ProtocolComponentOwner as _ProtocolComponentOwner,
    RecursiveProtocolCatalog as _RecursiveProtocolCatalog,
    closed_protocol_child_call as _closed_protocol_child_call,
    closed_protocol_child_projection as _closed_protocol_child_projection,
    component_accessor as _component_accessor,
    component_type_name as _component_type_name,
    elaborated_child_for_instance as _elaborated_child_for_instance,
    protocol_child_name as _protocol_child_name,
    protocol_instance_child_name as _protocol_instance_child_name,
    recursive_protocol_components as _recursive_protocol_components,
    specialized_protocol_children as _specialized_protocol_children,
    uses_bundled_component_abi as _uses_bundled_component_abi,
)
from zlang.backend.clash.syntax import (
    apply_argument as _clash_apply_arg,
    or_signal_expressions as _clash_or_signals,
)
from zlang.backend.clash.ports import (
    forward_port_annotation as _top_forward_port_annotation,
    value_port_annotation as _top_value_port_annotation,
)
from zlang.backend.expression_materialization import (
    dependency_ordered_materialization,
    module_expression_roots,
    plan_materialization,
    replace_materialized,
)
from zlang.backend.identifiers import (
    first_rtl_leaf_identifier_collision,
    rtl_identifier,
)
from zlang.backend.manifest import BackendArtifact, publish_artifact
from zlang.backend.naming import build_component_name_plan, module_rtl_names
from zlang.backend.source_map import GeneratedSourceMap, build_generated_source_map
from zlang.fixed_point import quantize_rational
from zlang.ir.recursive_formal import BackendPhysicalLocator
from zlang.ir.signed_reductions import selection_expression_semantic_identity
from zlang.diagnostics import DiagnosticError


class ClashEmissionError(DiagnosticError):
    """Typed IR is outside the subset implemented by the Clash backend."""

    default_code = "ZL-BACKEND-CLASH-001"


def _first_conditional_rule_action(module: Module) -> object | None:
    """Return one nested-action effect for backend capability preflights."""

    return next(
        (
            action
            for rule in module.rules
            for action in rule.actions
            if action.activation is not None
        ),
        None,
    )


def _reject_conditional_action_route(module: Module, route: str) -> None:
    """Fail closed when a specialized emitter cannot retain activation."""

    action = _first_conditional_rule_action(module)
    if action is None:
        return
    raise ClashEmissionError(
        f"Clash {route} emission cannot preserve nested conditional action "
        f"activation in module '{module.name}'",
        code="ZL-BACKEND-CLASH-CONDITIONAL-ACTION",
        primary=(
            action.activation.origin
            if action.activation is not None else None
        ),
        notes=(
            "emission stopped before artifact publication; the activation "
            "predicate was not lowered as an unconditional effect",
        ),
    )


def _domain_declaration(module: Module, name: str) -> str:
    try:
        return _render_domain_declaration(module, name)
    except _ClashDomainError as error:
        raise ClashEmissionError(str(error)) from error


def _top_reset_expression(module: Module) -> str:
    try:
        return _render_top_reset_expression(module)
    except _ClashDomainError as error:
        raise ClashEmissionError(str(error)) from error


def _cdc_rendering() -> CDCRendering:
    return CDCRendering(
        ClashEmissionError,
        _emit_struct,
        _all_structs,
        _ready_valid_declarations,
        _emit_type,
        _zero_value,
    )


@dataclass(frozen=True)
class _StorageCircuitEmission:
    """Typed storage circuit plus the interface metadata used by its wrapper."""

    extensions: str
    imports: str
    declarations: str
    domain: str
    input_types: tuple[str, ...]
    input_names: tuple[str, ...]
    input_annotations: tuple[str, ...]
    output_type: str
    output_annotations: tuple[str, ...]
    circuit: str


_CLASH_MODULE_NAME = re.compile(r"[A-Z][A-Za-z0-9_]*\Z")
_CLASH_RESERVED = {
    "case", "class", "data", "default", "deriving", "do", "else", "foreign",
    "if", "import", "in", "infix", "infixl", "infixr", "instance", "let",
    "module", "newtype", "of", "then", "type", "where", "forall", "mdo",
    "rec", "family", "role", "stock", "anyclass", "via",
    # Clash.Prelude constants/functions are ordinary Haskell identifiers and
    # can be shadowed by legal ZLang port names.  Shadowing `high`/`low`, for
    # example, changes a Bit comparison into a nested Signal expression.
    "forward", "high", "low",
}

# Scalar child helpers share the unqualified Haskell value namespace with
# Clash.Prelude and with the small runtime emitted below.  A source module such
# as ``Resize`` or ``Register`` is legal ZLang, but its historical lower-cased
# helper spelling would otherwise replace the primitive at every use site.
# Keep this conservative inventory explicit: these are value-level identifiers
# emitted unqualified by this backend, not source-name guesses.
_CLASH_HELPER_RUNTIME_RESERVED = {
    "abs", "asyncRam", "bitCoerce", "bitToBool", "blockRam",
    "blockRamPow2", "boolToBit", "bundle", "clockGen", "complement",
    "concat", "concatMap", "const", "deepErrorX", "enableGen", "errorX",
    "exposeClockResetEnable", "findIndex", "flip", "fold", "foldl",
    "foldr", "fromIntegral", "fromInteger", "fst", "head", "id", "init",
    "last", "map", "max", "mealy", "min", "moore", "mux", "negate",
    "noReset", "not", "otherwise", "pack", "pure", "regEn", "register",
    "repeat", "replace", "replicate", "resetGen", "resize", "reverse",
    "romFile", "romFilePow2", "rotateL", "rotateR", "shiftL", "shiftR",
    "signExtend", "singleton", "slice", "snd", "tail", "testBit",
    "truncateB", "unbundle", "undefined", "unpack", "unsafeFromActiveHigh",
    "unsafeToActiveHigh", "withClockResetEnable", "zeroExtend", "zip",
    "zipWith",
    # Backend-emitted runtime declarations imported into the same module.
    "zlangContainsId", "zlangCreditPayload", "zlangCreditReturnPulse",
    "zlangCreditSend", "zlangDspMac", "zlangInsertId", "zlangPacketLast",
    "zlangPacketPayload", "zlangPacketReady", "zlangPacketValid",
    "zlangPayloadElement", "zlangPayloadItem", "zlangPayloadLeaf",
    "zlangPayloadValue", "zlangRemoveId", "zlangReshapeSource",
    "zlangRuntimeIndex", "zlangRuntimeVector", "zlangRvPayload",
    "zlangRvReady", "zlangRvValid", "zlangTuple", "zlangUpdateIds",
    "zlangUpdateIndex", "zlangUpdateValue", "zlangUpdateVector",
    "zlangVcCreditPayload", "zlangVcCreditReturnPulse",
    "zlangVcCreditReturnVc", "zlangVcCreditSend", "zlangVcCreditVc",
}


def _clash_name(name: str) -> str:
    return f"{name}_zlang" if name in _CLASH_RESERVED else name


def _clash_instance_name(name: str, module: Module | None = None) -> str:
    """Map a typed physical instance path to one deterministic Haskell name."""

    if module is not None:
        return _clash_module_names(module).instance(name)
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\[([0-9]+)\]", name)
    if match is None:
        return _clash_name(name)
    return f"{_clash_name(match.group(1))}_{match.group(2)}"


def _clash_module_names(module: Module):
    """Allocate private names in one closed component's lexical namespace."""

    if len(module.children) != len(module.elaborated_instances):
        raise ClashEmissionError(
            f"hierarchical child '{module.name}' has incomplete elaborated "
            "instance metadata"
        )
    return module_rtl_names(
        module,
        identifier=_clash_name,
        reserved=(*_CLASH_RESERVED, *_CLASH_HELPER_RUNTIME_RESERVED),
    )


def _clash_child_leaf_names(module: Module, names=None) -> dict[tuple[object, ...], str]:
    """Map semantic child-output leaves, never infer identity from a token."""

    plan = names or _clash_module_names(module)
    return {
        _leaf_key(expr.InstanceOutputRef(instance.name, port.name, port.type)):
        plan.child_signal(instance.name, port.name)
        for instance, child in zip(module.instances, module.children, strict=False)
        for port in child.outputs
        if port.protocol is InterfaceProtocol.WIRE
    }


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
    try:
        inventory = module_feature_inventory(module)
        validate_feature_claims(
            inventory,
            claims_for_groups(module, plan, (*_VALUE_GROUPS, *extra)),
            backend="clash",
            plan=plan,
        )
    except ModuleFeatureAccountingError as error:
        raise ClashEmissionError(str(error)) from error


def emit(module: Module) -> str:
    """Emit one typed selected module as Clash."""

    non_default_domain = next(
        (domain for domain in module.clock_domains if not domain.is_legacy_default),
        None,
    )
    if (
        non_default_domain is not None
        and (
            non_default_domain.power_up.value == "reset"
            or len(module.clock_domains) != 1
        )
    ):
        raise ClashEmissionError(
            "Clash 1.11 physical clock/reset contracts are not implemented: "
            f"clock '{non_default_domain.clock}' uses edge="
            f"{non_default_domain.edge.value}, reset_mode="
            f"{non_default_domain.reset_mode.value}, reset_polarity="
            f"{non_default_domain.reset_polarity.value}, power_up="
            f"{non_default_domain.power_up.value}"
        )

    def external_name(current: Module) -> str | None:
        if current.external_contract is not None:
            return current.external_contract.logical_name
        return next(
            (
                found
                for child in current.children
                if (found := external_name(child)) is not None
            ),
            None,
        )

    unsupported_external = external_name(module)
    if unsupported_external is not None:
        raise ClashEmissionError(
            f"external module '{unsupported_external}' is not supported by the "
            "Clash backend in the bounded scalar-external slice; use the "
            "direct-SystemVerilog backend with an exact physical mapping"
        )

    if _CLASH_MODULE_NAME.fullmatch(module.name) is None:
        raise ClashEmissionError(
            f"Clash module name '{module.name}' must begin with an uppercase letter"
        )
    unsafe_state_engines = unsupported_legacy_state_mix(module)
    if unsafe_state_engines:
        raise ClashEmissionError(
            f"module '{module.name}' combines user registers/rules with "
            f"{', '.join(unsafe_state_engines)}; Clash emission stopped before "
            "artifact publication because the specialized state engines are "
            "not compositional"
        )
    if any(connection.crossing is not None for connection in module.connections):
        _account_emission_plan(
            module,
            "cdc",
            ModuleFeatureGroup.PROTOCOL_PORTS,
            ModuleFeatureGroup.HIERARCHICAL_PROTOCOL_ENDPOINTS,
            ModuleFeatureGroup.AGGREGATE_PROTOCOL_ENDPOINTS,
            ModuleFeatureGroup.CONNECTIONS,
            ModuleFeatureGroup.AGGREGATE_PROTOCOL_CONNECTIONS,
        )
        return _emit_cdc_subsystem(module, _cdc_rendering())
    if module.aggregate_protocol_endpoints:
        _account_emission_plan(module, "aggregate_hierarchy", *_COMPOSED_GROUPS)
        return _emit_hierarchical_protocol_module(module)
    if module.hierarchical_connections:
        _account_emission_plan(module, "protocol_hierarchy", *_COMPOSED_GROUPS)
        return _emit_hierarchical_protocol_module(module)
    if module.csr_blocks:
        _account_emission_plan(
            module,
            "csr_composed_state",
            *_STATE_GROUPS,
            ModuleFeatureGroup.CSR_BLOCKS,
            ModuleFeatureGroup.PROTOCOL_PORTS,
        )
        return _emit_csr_module(module)
    if module.arbiters:
        _account_emission_plan(
            module,
            "packet_arbiter",
            ModuleFeatureGroup.ARBITERS,
            ModuleFeatureGroup.PROTOCOL_PORTS,
        )
        return _emit_packet_arbiter_module(module)
    if (
        module.fifos
        or module.memories
        or (module.roms and not (module.registers or module.rules or module.next_assignments))
    ):
        _account_emission_plan(
            module,
            "storage",
            *_STATE_GROUPS,
            *_STORAGE_GROUPS,
            ModuleFeatureGroup.PROTOCOL_PORTS,
        )
        return _emit_storage_module(module)
    if any(
        port.protocol is InterfaceProtocol.VC_CREDIT for port in module.ports
    ):
        _account_emission_plan(
            module,
            "vc_credit",
            ModuleFeatureGroup.PROTOCOL_PORTS,
            ModuleFeatureGroup.VC_CREDIT_PORTS,
        )
        return _emit_vc_credit_module(module)
    if any(
        connection.buffer_depth
        or connection.adapter is not None
        or connection.crossing is not None
        for connection in module.connections
    ) or (
        module.connections
        and module.is_sequential
        and any(
            port.protocol is InterfaceProtocol.READY_VALID
            for port in module.ports
        )
    ):
        _account_emission_plan(
            module,
            "connection",
            *_STATE_GROUPS,
            ModuleFeatureGroup.PROTOCOL_PORTS,
            ModuleFeatureGroup.CONNECTIONS,
            ModuleFeatureGroup.CREDIT_PORTS,
            ModuleFeatureGroup.VC_CREDIT_PORTS,
        )
        return _emit_connection_module(module)
    if module.elastic_pipeline_regions:
        _account_emission_plan(
            module,
            "elastic_pipeline",
            ModuleFeatureGroup.PROTOCOL_PORTS,
            ModuleFeatureGroup.ELASTIC_PIPELINE_REGIONS,
        )
        return _emit_elastic_pipeline_module(module)
    if module.request_responses:
        if any(port.protocol is not InterfaceProtocol.WIRE for port in module.ports):
            raise ClashEmissionError(
                "mixing request/response with other protocols is not implemented"
            )
        _account_emission_plan(
            module,
            "request_response",
            *_STATE_GROUPS,
            ModuleFeatureGroup.REQUEST_RESPONSE_INTERFACES,
        )
        return _emit_request_response_module(module)
    if any(
        port.protocol is InterfaceProtocol.CREDIT for port in module.ports
    ):
        if any(
            port.protocol is InterfaceProtocol.READY_VALID for port in module.ports
        ):
            raise ClashEmissionError(
                "mixing ready/valid and credit interfaces is not implemented"
            )
        _account_emission_plan(
            module,
            "credit",
            ModuleFeatureGroup.PROTOCOL_PORTS,
            ModuleFeatureGroup.CREDIT_PORTS,
        )
        return _emit_credit_module(module)
    if module.has_protocol_interfaces:
        if module.is_sequential:
            _account_emission_plan(
                module,
                "stateful_protocol",
                *_STATE_GROUPS,
                ModuleFeatureGroup.PROTOCOL_PORTS,
                ModuleFeatureGroup.HIERARCHICAL_PROTOCOL_ENDPOINTS,
                ModuleFeatureGroup.AGGREGATE_PROTOCOL_ENDPOINTS,
                ModuleFeatureGroup.CONNECTIONS,
                ModuleFeatureGroup.AGGREGATE_PROTOCOL_CONNECTIONS,
            )
            return _emit_standalone_stateful_protocol_module(module)
        _account_emission_plan(
            module,
            "protocol",
            ModuleFeatureGroup.PROTOCOL_PORTS,
            ModuleFeatureGroup.HIERARCHICAL_PROTOCOL_ENDPOINTS,
            ModuleFeatureGroup.AGGREGATE_PROTOCOL_ENDPOINTS,
            ModuleFeatureGroup.CONNECTIONS,
            ModuleFeatureGroup.AGGREGATE_PROTOCOL_CONNECTIONS,
        )
        return _emit_interface_module(module)
    if module.elaborated_instances and not module.is_sequential:
        _account_emission_plan(
            module,
            "combinational_hierarchy",
            ModuleFeatureGroup.INSTANCES,
            ModuleFeatureGroup.CONNECTIONS,
        )
        return _emit_combinational_hierarchy(module)
    if module.is_sequential:
        _account_emission_plan(
            module,
            "sequential",
            *_STATE_GROUPS,
            ModuleFeatureGroup.INSTANCES,
            ModuleFeatureGroup.CONNECTIONS,
        )
        return _emit_sequential_module(module)
    _account_emission_plan(
        module,
        "combinational",
        ModuleFeatureGroup.CONNECTIONS,
    )
    return _emit_scalar_wire_module(module)


def emit_artifact(module: Module, *, selected_ir_identity: str | None = None,
                  recursive_design: object | None = None) -> BackendArtifact:
    """Emit Clash source and publish stable top-level bindings."""
    production = emit(module)
    text = production
    identity = selected_ir_identity or default_selected_ir_identity(module)
    names = {
        f"port:{port.name}": rtl_identifier(port.name)
        for port in module.ports
    }
    physical_leaves = tuple(module.top_physical_abi.leaves)
    collision = first_rtl_leaf_identifier_collision(physical_leaves)
    if collision is not None:
        raise ClashEmissionError(
            "Clash public top identifier collision after mangling: "
            f"'{collision.first_external_name}' "
            f"({collision.first_semantic_id}) and "
            f"'{collision.second_external_name}' "
            f"({collision.second_semantic_id}) both map to "
            f"'{collision.physical_name}'"
        )
    names.update({
        leaf.leaf_semantic_id: _clash_public_leaf_name(leaf)
        for leaf in physical_leaves
    })
    if module.clock:
        names["clock"] = module.clock
    if module.reset:
        names["reset"] = module.reset
    return publish_artifact(module, text, backend="clash",
                            selected_ir_identity=identity, rtl_names=names,
                            validated_physical_paths=frozenset(
                                _clash_annotation_leaf_paths(physical_leaves)
                            ),
                            recursive_design=recursive_design,
                            companions=collect_rom_companions(module))


def _clash_public_leaf_name(leaf: object) -> str:
    """Return the exact physical leaf spelling produced by top annotations."""

    return rtl_identifier(leaf.external_name)


def _clash_annotation_leaf_paths(leaves: tuple[object, ...]) -> tuple[str, ...]:
    """Return public leaves proven to be concrete Clash annotation ports.

    A recursively split struct leaf has no combined token in Haskell source,
    but its typed ``PortProduct`` path is still a concrete generated RTL port.
    Non-contiguous leaves of ``Vec<Struct>`` remain hidden in one packed core
    port and are deliberately omitted until the typed public wrapper exists.
    Aggregate hierarchy emission already materializes each ABI leaf directly.
    """

    return tuple(
        _clash_public_leaf_name(leaf)
        for leaf in leaves
        if (
            leaf.category == "aggregate"
            or (
                leaf.packed_msb is not None
                and leaf.packed_lsb is not None
            )
        )
    )


def emit_artifact_with_source_map(
    module: Module,
    *,
    selected_ir_identity: str | None = None,
    recursive_design: object | None = None,
) -> tuple[BackendArtifact, GeneratedSourceMap]:
    """Publish a Clash artifact and its exact, deterministic source-map sidecar."""

    artifact = emit_artifact(
        module,
        selected_ir_identity=selected_ir_identity,
        recursive_design=recursive_design,
    )
    return artifact, build_generated_source_map(module, artifact)


def emit_formal_artifact(module: Module, recursive_design: object,
                         *, selected_ir_identity: str | None = None) -> BackendArtifact:
    """Publish the separate v4 formal-observation artifact metadata.

    The production Clash source is intentionally untouched.  The recursive
    binding table is published before any RTL normalization; a later backend
    adapter is responsible for materializing the deterministic observation
    ports required by the selected property set.
    """
    from zlang.backend.clash.formal_registers import (
        emit_receiver_credit_formal_source,
        emit_register_formal_source,
        supports_receiver_credit_formal,
        supports_register_formal,
    )

    structured_supported = supports_register_formal(module, recursive_design)
    receiver_credit_supported = supports_receiver_credit_formal(
        module, recursive_design
    )
    if structured_supported or receiver_credit_supported:
        structured = (
            emit_register_formal_source(module, recursive_design)
            if structured_supported
            else emit_receiver_credit_formal_source(module, recursive_design)
        )
        text = structured.text
        # Register observations become physically available only after real
        # Clash RTL generation and port/width validation.
        materialized: dict[str, str] = {}
    else:
        from zlang.ir.recursive_formal import instrument_csr_observation_ports
        needs_csr_transport = any(
            item.ref.local_semantic_id.startswith("csr-field:")
            and len(item.physical_instance_path) > 2
            for item in recursive_design.bindings
        )
        if needs_csr_transport:
            formal_module, csr_ports = instrument_csr_observation_ports(module)
        else:
            formal_module, csr_ports = module, {}
        production = emit(formal_module)
        csr_overrides = {
            item.semantic_binding_id: csr_ports[
                (item.physical_instance_path[1:], item.ref.local_semantic_id)
            ]
            for item in recursive_design.bindings
            if (item.physical_instance_path[1:], item.ref.local_semantic_id)
            in csr_ports
        }
        text, materialized = _emit_clash_formal_artifact(
            production, formal_module, recursive_design,
            materialized_overrides=csr_overrides,
        )
    identity = selected_ir_identity or default_selected_ir_identity(module)
    names = {f"port:{port.name}": port.name for port in module.ports}
    if module.clock:
        names["clock"] = module.clock
    if module.reset:
        names["reset"] = module.reset
    digest = hashlib.sha256(text.encode()).hexdigest()
    bound = []
    for item in recursive_design.bindings:
        token = materialized.get(item.semantic_binding_id)
        locator = None if token is None else BackendPhysicalLocator(
            "clash", digest, f"{module.name}_formal", (), token, token
        )
        bound.append(replace(item, locator=locator))
    formal_design = replace(recursive_design, bindings=tuple(bound))
    return publish_artifact(module, text, backend="clash",
                            selected_ir_identity=identity, rtl_names=names,
                            recursive_design=formal_design,
                            formal_artifact_hash=digest,
                            companions=collect_rom_companions(module))


def _emit_clash_formal_artifact(
    production: str,
    module: Module,
    recursive_design: object,
    *,
    materialized_overrides: dict[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Add a formal-only observation product while preserving production text."""
    start = production.find("circuit ::")
    if start < 0:
        return production, {}
    segment = production[start:]
    tokens: list[tuple[object, str]] = []
    materialized_overrides = materialized_overrides or {}
    token_overrides = _clash_recursive_token_overrides(module)
    for index, item in enumerate(sorted(recursive_design.bindings,
                                        key=lambda value: value.semantic_binding_id)):
        token = (
            materialized_overrides.get(item.semantic_binding_id)
            or _clash_csr_observation_token(module, recursive_design, item)
            or token_overrides.get(item.ref.local_semantic_id)
            or _recursive_formal_token(item.ref.local_semantic_id)
        )
        if token and re.search(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", segment):
            tokens.append((item, f"zlang_formal_obs_{index}"))
    if not tokens:
        return production, {}
    formal = segment.replace("circuit", "formalCircuit").replace("topEntity", "formalTopEntity")
    signature = re.search(r"^formalCircuit :: (.+)$", formal, re.MULTILINE)
    definition = re.search(r"^formalCircuit ([^\n]+) = (.+)$", formal, re.MULTILINE)
    if signature is None or definition is None:
        return production, {}
    observation_types = ", ".join(_formal_observation_type(item) for item, _ in tokens)
    def append_result_types(type_text: str) -> str:
        prefix, separator, result_type = type_text.rpartition(" -> ")
        if not separator:
            return f"({type_text}, {observation_types})"
        if result_type.startswith("(") and result_type.endswith(")"):
            result_type = result_type[:-1] + ", " + observation_types + ")"
        else:
            result_type = f"({result_type}, {observation_types})"
        return prefix + separator + result_type

    old_type = signature.group(1)
    new_type = append_result_types(old_type)
    formal = formal.replace(signature.group(0), f"formalCircuit :: {new_type}", 1)
    top_signature = re.search(r"^formalTopEntity :: (.+)$", formal, re.MULTILINE)
    if top_signature is not None:
        top_type = top_signature.group(1)
        formal = formal.replace(
            top_signature.group(0),
            f"formalTopEntity :: {append_result_types(top_type)}",
            1,
        )
    original_result = definition.group(2).strip()
    observation_values = ", ".join(
        _clash_formal_observation_value(item, source_token, recursive_design)
        for item, _ in tokens
        for source_token in [
            materialized_overrides.get(item.semantic_binding_id)
            or _clash_csr_observation_token(module, recursive_design, item)
            or token_overrides.get(item.ref.local_semantic_id)
            or _recursive_formal_token(item.ref.local_semantic_id)
        ]
    )
    if original_result.startswith("(") and original_result.endswith(")"):
        formal_result = original_result[:-1] + ", " + observation_values + ")"
    else:
        formal_result = f"({original_result}, {observation_values})"
    formal = formal.replace(
        definition.group(0),
        f"formalCircuit {definition.group(1)} = {formal_result}", 1,
    )
    # Port annotations are deliberately kept to one generated source line.
    # Treat the complete annotation as an opaque typed tree: recursive struct
    # leaves contain nested ``PortProduct`` brackets, so the old first-`]'
    # regular expression could truncate a valid public ABI.
    output_annotation = re.search(r't_output = ([^\n]+)', formal)
    if output_annotation is not None:
        existing = output_annotation.group(1).strip()
        ports = ', '.join(f'PortName "{token}"' for _, token in tokens)
        if existing.startswith('PortName "'):
            existing = 'PortName "value"'
        if existing.startswith('PortProduct "" [') and existing.endswith("]"):
            existing = existing[len('PortProduct "" ['):-1]
        replacement = f't_output = PortProduct "" [{existing}, {ports}]'
        formal = formal.replace(output_annotation.group(0), replacement, 1)
    formal = formal.replace('t_name = "' + module.name + '"',
                            't_name = "' + module.name + '_formal"', 1)
    return production + "\n" + formal, {
        item.semantic_binding_id: token for item, token in tokens
    }


def _clash_formal_observation_value(
    binding: object, source_token: str, recursive_design: object,
) -> str:
    local_id = binding.ref.local_semantic_id
    if not local_id.startswith("csr-field:"):
        return source_token
    nodes = {item.instance_identity: item for item in recursive_design.instances}
    node = nodes.get(binding.ref.instance_identity)
    if node is None or len(node.physical_instance_path) != 1:
        return source_token
    if local_id.endswith(":write-hit"):
        return f"boolToBit <$> {source_token}"
    return f"unpack <$> {source_token}"


def _clash_csr_observation_token(
    module: Module, recursive_design: object, binding: object,
) -> str | None:
    """Resolve a CSR observation through typed instance-output projections."""
    local_id = binding.ref.local_semantic_id
    if not local_id.startswith("csr-field:"):
        return None
    nodes = {item.instance_identity: item for item in recursive_design.instances}
    node = nodes.get(binding.ref.instance_identity)
    if node is None:
        return None
    try:
        hierarchy = build_hierarchy_index(module)
        owner_entry = hierarchy.at(tuple(node.physical_instance_path))
    except HierarchyError as error:
        raise ClashEmissionError(str(error)) from error
    if owner_entry.module.name != node.module_name:
        raise ClashEmissionError(
            f"recursive CSR path '{'.'.join(node.physical_instance_path)}' names "
            f"module '{node.module_name}', not typed module "
            f"'{owner_entry.module.name}'"
        )
    if (
        owner_entry.elaborated is not None
        and owner_entry.specialization_identity != node.specialization_identity
    ):
        raise ClashEmissionError(
            f"recursive CSR path '{'.'.join(node.physical_instance_path)}' "
            "specialization does not match typed elaboration"
        )
    owner = owner_entry.module
    physical = next(
        (item.rtl_name for item in signal_bindings(owner)
         if item.semantic_signal_id == local_id), None
    )
    if physical is None:
        return None
    path = node.physical_instance_path
    if len(path) == 1:
        return physical
    child_port = next(
        (
            port_name
            for block in owner.csr_blocks
            for state in block.state_bindings
            for semantic_id, port_name in (
                (state.semantic_state_id, ir_csr.csr_state_port_name(state)),
                (state.write_hit_id, ir_csr.csr_write_hit_port_name(state)),
                (state.write_value_id, ir_csr.csr_write_value_port_name(state)),
            )
            if semantic_id == local_id
        ),
        None,
    )
    if child_port is None:
        return None
    current_path = path
    while len(current_path) > 1:
        parent_path = current_path[:-1]
        instance_name = current_path[-1]
        try:
            parent = hierarchy.at(parent_path).module
        except HierarchyError as error:
            raise ClashEmissionError(str(error)) from error
        projected = next(
            (assignment.target.name for assignment in parent.assignments
             if isinstance(assignment.expression, expr.InstanceOutputRef)
             and assignment.expression.instance == instance_name
             and assignment.expression.port == child_port
             and assignment.signal is None),
            None,
        )
        if projected is not None:
            child_port = projected
            current_path = parent_path
            continue
        return f"{instance_name}_{child_port}" if len(parent_path) == 1 else None
    return child_port


def _recursive_formal_token(semantic_id: str) -> str | None:
    if semantic_id.startswith("rr:") and semantic_id.endswith(":outstanding"):
        return "rr_" + semantic_id.replace(":", "_").replace("->", "_").replace(".", "_")
    if semantic_id.startswith("fifo:"):
        return semantic_id.split(":", 1)[1].replace(".", "_") + "_count"
    return None


def _formal_observation_type(item: object) -> str:
    canonical = str(item.ref.canonical_type)
    if item.width == 1 and (canonical == "bit" or "BitType" in canonical):
        return "Signal ZLangSystem Bit"
    return f"Signal ZLangSystem (Unsigned {item.width})"


def _clash_recursive_token_overrides(module: Module) -> dict[str, str]:
    """Map semantic protocol observations to names emitted by the generic ABI.

    This table is derived from typed connection objects, not reconstructed from
    an RTL string or from a source spelling.  Objects for which the current
    Clash component ABI has no observable signal remain unavailable.
    """
    private_names = _clash_module_names(module)
    overrides: dict[str, str] = {}
    for connection in module.request_response_connections:
        descriptor = connection.semantic_id
        source = connection.request.source
        base = (
            f"rr_{private_names.instance(source.owner)}_"
            f"{_clash_name(source.name)}_"
            f"{private_names.instance(connection.response.destination.owner)}_"
            "outstanding"
        )
        overrides[request_response_observation_id(
            descriptor, "outstanding"
        )] = base
        overrides[request_response_observation_id(
            descriptor, "request_accept"
        )] = base + "_request_accept"
        overrides[request_response_observation_id(
            descriptor, "response_consume"
        )] = base + "_response_consume"
        overrides[request_response_observation_id(
            descriptor, "request_occupancy"
        )] = base + "_request_occupancy"
        overrides[request_response_observation_id(
            descriptor, "response_occupancy"
        )] = base + "_response_occupancy"
    return overrides


def _emit_interface_module(module: Module) -> str:
    extensions, imports, declarations = _emit_prelude(module)
    if "{-# LANGUAGE DeriveAnyClass #-}" not in extensions:
        extensions = extensions.replace(
            "{-# LANGUAGE NoImplicitPrelude #-}",
            "{-# LANGUAGE DeriveAnyClass #-}\n"
            "{-# LANGUAGE DeriveGeneric #-}\n"
            "{-# LANGUAGE NoImplicitPrelude #-}",
        )
    if "import GHC.Generics (Generic)" not in imports:
        imports += "import GHC.Generics (Generic)\n"
    declarations = _ready_valid_declarations() + declarations

    signature_inputs: list[str] = []
    argument_patterns: list[str] = []
    input_annotations: list[str] = []
    output_types: list[str] = []
    output_values: list[str] = []
    output_annotations: list[str] = []

    for port in module.ports:
        if port.protocol is InterfaceProtocol.WIRE:
            if port.direction is PortDirection.INPUT:
                signature_inputs.append(_emit_type(port.type))
                argument_patterns.append(port.name)
                input_annotations.append(
                    _top_value_port_annotation(port.name, port.type)
                )
            else:
                output_types.append(_emit_type(port.type))
                output_values.append(port.name)
                output_annotations.append(
                    _top_value_port_annotation(port.name, port.type)
                )
            continue

        if port.direction is PortDirection.INPUT:
            signature_inputs.append(
                f"ZLangReadyValidForward ({_emit_type(port.type)})"
            )
            argument_patterns.append(
                f"(ZLangReadyValidForward {port.name}_payload {port.name}_valid)"
            )
            input_annotations.append(
                _top_forward_port_annotation(port.name, port.type, "valid")
            )
            output_types.append("ZLangReadyValidBackward")
            output_values.append(f"ZLangReadyValidBackward {port.name}_ready")
            output_annotations.append(f'PortName "{port.name}_ready"')
        else:
            signature_inputs.append("ZLangReadyValidBackward")
            argument_patterns.append(
                f"(ZLangReadyValidBackward {port.name}_ready)"
            )
            input_annotations.append(f'PortName "{port.name}_ready"')
            output_types.append(
                f"ZLangReadyValidForward ({_emit_type(port.type)})"
            )
            output_values.append(
                f"ZLangReadyValidForward {port.name}_payload {port.name}_valid"
            )
            output_annotations.append(
                _top_forward_port_annotation(port.name, port.type, "valid")
            )

    output_type = _emit_product(output_types)
    signature = " -> ".join((*signature_inputs, output_type))
    arguments = " ".join(argument_patterns)
    result = _emit_product(output_values)
    bindings = "\n".join(
        f"  {_assignment_name(assignment)} = "
        f"{_emit_expression(assignment.expression)}"
        for assignment in module.assignments
    )
    input_ports = ", ".join(input_annotations)
    output_port = (
        output_annotations[0]
        if len(output_annotations) == 1
        else f'PortProduct "" [{", ".join(output_annotations)}]'
    )
    lhs = f"topEntity {arguments}" if arguments else "topEntity"

    return f'''{extensions}
module {module.name} where

{imports}
{declarations}topEntity :: {signature}
{lhs} = {result}
 where
{bindings}

{{-# ANN topEntity
  (Synthesize
    {{ t_name = "{module.name}"
    , t_inputs = [{input_ports}]
    , t_output = {output_port}
    }}) #-}}
'''


def _emit_elastic_pipeline_component(module: Module, function_name: str) -> str:
    """Emit one closed bundled globally-stalled ready/valid component."""

    private_names = _clash_module_names(module)
    if len(module.elastic_pipeline_regions) != 1:
        raise ClashEmissionError("elastic component requires exactly one region")
    region = module.elastic_pipeline_regions[0]
    source = next(port for port in module.ports if port.name == region.source_endpoint)
    destination = next(
        port for port in module.ports if port.name == region.destination_endpoint
    )
    root = region.selected_candidate.expression
    staged: dict[int, expr.Delay | expr.Pipeline] = {}
    _collect_delays(root, staged)
    if any(isinstance(node, expr.Delay) for node in staged.values()):
        raise ClashEmissionError("elastic component does not accept Delay nodes")
    if tuple(sorted((node.instance, node.stages) for node in staged.values())) != (
        region.plan.data_stage_instances
    ):
        raise ClashEmissionError("elastic component stages disagree with its plan")
    source_name = _clash_name(source.name)
    destination_name = _clash_name(destination.name)
    backward_name = f"{destination_name}_backward"
    delay_names = {
        _leaf_key(node): private_names.stage(_stage_prefix(node), node.instance, node.stages)
        for node in staged.values()
    }
    bindings: list[str] = [
        "reset_active = unsafeToActiveHigh hasReset",
        f"{source_name}_payload = zlangRvPayload <$> {source_name}",
        f"{source_name}_valid = zlangRvValid <$> {source_name}",
        f"{destination_name}_ready = zlangRvReady <$> {backward_name}",
    ]
    valid_names = [
        f"zlang_elastic_valid_{index}"
        for index in range(region.timing.minimum_unstalled_latency)
    ]
    bindings.append(
        "zlang_elastic_advance = "
        "(\\resetActive valid ready -> not resetActive "
        "&& (valid == low || ready == high)) "
        f"<$> reset_active <*> {valid_names[-1]} <*> {destination_name}_ready"
    )
    previous_valid = f"{source_name}_valid"
    for valid_name in valid_names:
        bindings.append(
            f"{valid_name} = regEn low zlang_elastic_advance {previous_valid}"
        )
        previous_valid = valid_name
    for node in staged.values():
        previous = _emit_signal_expression(node.expression, delay_names)
        for stage in range(1, node.stages + 1):
            stage_name = private_names.stage(_stage_prefix(node), node.instance, stage)
            bindings.append(
                f"{stage_name} = regEn {_zero_value(node.type)} "
                f"zlang_elastic_advance ({_clash_apply_arg(previous)})"
            )
            previous = stage_name
    bindings.extend((
        f"{source_name}_ready = boolToBit <$> zlang_elastic_advance",
        f"{destination_name}_payload = {_emit_signal_expression(root, delay_names)}",
        f"{destination_name}_valid = (\\resetActive valid -> if resetActive "
        f"then low else valid) <$> reset_active <*> {valid_names[-1]}",
    ))
    signature = " -> ".join((
        f"Signal ZLangSystem (ZLangReadyValidForward ({_emit_type(source.type)}))",
        "Signal ZLangSystem ZLangReadyValidBackward",
        _emit_product([
            "Signal ZLangSystem ZLangReadyValidBackward",
            f"Signal ZLangSystem (ZLangReadyValidForward ({_emit_type(destination.type)}))",
        ]),
    ))
    result = _emit_product([
        f"ZLangReadyValidBackward <$> {source_name}_ready",
        f"ZLangReadyValidForward <$> {destination_name}_payload <*> {destination_name}_valid",
    ])
    where_block = "\n".join(f"  {binding}" for binding in bindings)
    return (
        f"{function_name} :: HiddenClockResetEnable ZLangSystem => {signature}\n"
        f"{function_name} {source_name} {backward_name} = {result}\n"
        f" where\n{where_block}\n"
    )


def _emit_elastic_pipeline_module(module: Module) -> str:
    """Expose the same closed component as a clock/reset-explicit top."""

    if module.clock is None or module.reset is None:
        raise ClashEmissionError("elastic pipeline requires clock/reset")
    region = module.elastic_pipeline_regions[0]
    source = next(port for port in module.ports if port.name == region.source_endpoint)
    destination = next(
        port for port in module.ports if port.name == region.destination_endpoint
    )
    extensions, imports, declarations = _emit_prelude(module)
    if "{-# LANGUAGE DeriveAnyClass #-}" not in extensions:
        extensions = extensions.replace(
            "{-# LANGUAGE NoImplicitPrelude #-}",
            "{-# LANGUAGE DeriveAnyClass #-}\n{-# LANGUAGE DeriveGeneric #-}\n"
            "{-# LANGUAGE NoImplicitPrelude #-}",
        )
    if "import GHC.Generics (Generic)" not in imports:
        imports += "import GHC.Generics (Generic)\n"
    declarations = _ready_valid_declarations() + declarations
    declarations += 'createDomain vSystem{vName="ZLangSystem", vResetKind=Synchronous}\n'
    component_name = _protocol_child_name(module)
    declarations += _emit_elastic_pipeline_component(module, component_name) + "\n"
    source_name = _clash_name(source.name)
    backward_name = f"{_clash_name(destination.name)}_backward"
    result_type = _emit_product([
        "Signal ZLangSystem ZLangReadyValidBackward",
        f"Signal ZLangSystem (ZLangReadyValidForward ({_emit_type(destination.type)}))",
    ])
    circuit_type = " -> ".join((
        f"Signal ZLangSystem (ZLangReadyValidForward ({_emit_type(source.type)}))",
        "Signal ZLangSystem ZLangReadyValidBackward",
        result_type,
    ))
    top_type = " -> ".join((
        "Clock ZLangSystem",
        "Reset ZLangSystem",
        f"Signal ZLangSystem (ZLangReadyValidForward ({_emit_type(source.type)}))",
        "Signal ZLangSystem ZLangReadyValidBackward",
        result_type,
    ))
    input_annotations = ", ".join((
        f'PortName "{module.clock}"',
        f'PortName "{module.reset}"',
        _top_forward_port_annotation(source.name, source.type, "valid"),
        f'PortName "{destination.name}_ready"',
    ))
    output_annotation = (
        'PortProduct "" ['
        f'PortName "{source.name}_ready", '
        f'{_top_forward_port_annotation(destination.name, destination.type, "valid")}'
        ']'
    )
    return f'''{extensions}
module {module.name} where

{imports}
{declarations}circuit :: HiddenClockResetEnable ZLangSystem => {circuit_type}
circuit {source_name} {backward_name} = {component_name} {source_name} {backward_name}

topEntity :: {top_type}
topEntity {module.clock} {module.reset} {source_name} {backward_name} = exposeClockResetEnable circuit {module.clock} {module.reset} enableGen {source_name} {backward_name}

{{-# ANN topEntity
  (Synthesize
    {{ t_name = "{module.name}"
    , t_inputs = [{input_annotations}]
    , t_output = {output_annotation}
    }}) #-}}
'''


def _emit_standalone_stateful_protocol_module(module: Module) -> str:
    """Expose the existing closed child ABI directly as a public top.

    Stateful ready/valid semantics are identical for a selected top and for an
    elaborated child.  Reuse the typed child component instead of maintaining a
    second scheduler or capturing top-level Signals in a generated helper.
    """

    if module.clock is None or module.reset is None:
        raise ClashEmissionError(
            "stateful ready/valid modules require one clock and reset"
        )
    if module.children or module.elaborated_instances or module.hierarchical_connections:
        raise ClashEmissionError(
            "standalone stateful ready/valid ABI does not accept child hierarchy"
        )
    if (
        module.aggregate_protocol_endpoints
        or module.request_responses
        or module.csr_blocks
        or module.fifos
        or module.memories
        or module.roms
        or module.arbiters
    ):
        raise ClashEmissionError(
            "standalone stateful ready/valid ABI currently supports ordinary "
            "wire and ready/valid ports with register/rule state"
        )
    if any(
        port.protocol not in {
            InterfaceProtocol.WIRE,
            InterfaceProtocol.READY_VALID,
        }
        for port in module.ports
    ):
        raise ClashEmissionError(
            "standalone stateful ready/valid ABI has an unsupported protocol"
        )

    wire_inputs = [
        port for port in module.inputs
        if port.protocol is InterfaceProtocol.WIRE
    ]
    rv_inputs = [
        port for port in module.inputs
        if port.protocol is InterfaceProtocol.READY_VALID
    ]
    rv_outputs = [
        port for port in module.outputs
        if port.protocol is InterfaceProtocol.READY_VALID
    ]
    wire_outputs = [
        port for port in module.outputs
        if port.protocol is InterfaceProtocol.WIRE
    ]
    if not rv_inputs and not rv_outputs:
        raise ClashEmissionError(
            "standalone stateful protocol ABI requires a ready/valid port"
        )

    component_name = _protocol_child_name(module)
    component = _emit_protocol_child_function(module, component_name)
    argument_names = [
        *(_clash_name(port.name) for port in wire_inputs),
        *(_clash_name(port.name) for port in rv_inputs),
        *(f"{_clash_name(port.name)}_backward" for port in rv_outputs),
    ]
    input_types = [
        *(f"Signal ZLangSystem ({_emit_type(port.type)})" for port in wire_inputs),
        *(
            f"Signal ZLangSystem (ZLangReadyValidForward "
            f"({_emit_type(port.type)}))"
            for port in rv_inputs
        ),
        *("Signal ZLangSystem ZLangReadyValidBackward" for _ in rv_outputs),
    ]
    result_ports = [*rv_inputs, *rv_outputs, *wire_outputs]
    result_types = [
        *("Signal ZLangSystem ZLangReadyValidBackward" for _ in rv_inputs),
        *(
            f"Signal ZLangSystem (ZLangReadyValidForward "
            f"({_emit_type(port.type)}))"
            for port in rv_outputs
        ),
        *(f"Signal ZLangSystem ({_emit_type(port.type)})" for port in wire_outputs),
    ]
    if not result_ports:
        raise ClashEmissionError(
            "standalone stateful ready/valid module has no physical outputs"
        )

    application = _closed_protocol_child_call(
        module,
        component_name,
        argument_names,
        error=ClashEmissionError,
    )
    bindings = [f"component_result = {application}"]
    output_values: list[str] = []
    for index, port in enumerate(result_ports):
        value = _closed_protocol_child_projection(
            module,
            port,
            "component_result",
            index,
            len(result_ports),
            component_name,
        )
        output_values.append(value)

    circuit_type = " -> ".join((*input_types, _emit_product(result_types)))
    circuit_arguments = " ".join(argument_names)
    circuit_lhs = (
        f"circuit {circuit_arguments}" if circuit_arguments else "circuit"
    )
    circuit_result = _emit_product(output_values)
    top_type = " -> ".join(
        (
            "Clock ZLangSystem",
            "Reset ZLangSystem",
            *input_types,
            _emit_product(result_types),
        )
    )
    top_arguments = " ".join(
        item for item in (module.clock, module.reset, circuit_arguments) if item
    )
    circuit_application = f" {circuit_arguments}" if circuit_arguments else ""

    input_annotations = [
        f'PortName "{module.clock}"',
        f'PortName "{module.reset}"',
        *(
            _top_value_port_annotation(port.name, port.type)
            for port in wire_inputs
        ),
        *(
            _top_forward_port_annotation(port.name, port.type, "valid")
            for port in rv_inputs
        ),
        *(f'PortName "{port.name}_ready"' for port in rv_outputs),
    ]
    output_annotations = [
        *(f'PortName "{port.name}_ready"' for port in rv_inputs),
        *(
            _top_forward_port_annotation(port.name, port.type, "valid")
            for port in rv_outputs
        ),
        *(
            _top_value_port_annotation(port.name, port.type)
            for port in wire_outputs
        ),
    ]
    output_annotation = (
        output_annotations[0]
        if len(output_annotations) == 1
        else f'PortProduct "" [{", ".join(output_annotations)}]'
    )

    extensions, imports, declarations = _emit_prelude(module)
    if "{-# LANGUAGE DeriveAnyClass #-}" not in extensions:
        extensions = extensions.replace(
            "{-# LANGUAGE NoImplicitPrelude #-}",
            "{-# LANGUAGE DeriveAnyClass #-}\n"
            "{-# LANGUAGE DeriveGeneric #-}\n"
            "{-# LANGUAGE NoImplicitPrelude #-}",
        )
    if "import GHC.Generics (Generic)" not in imports:
        imports += "import GHC.Generics (Generic)\n"
    declarations = _ready_valid_declarations() + declarations
    declarations += _domain_declaration(module, "ZLangSystem") + "\n"
    where_block = "\n".join(f"  {item}" for item in bindings)
    return f'''{extensions}
module {module.name} where

{imports}
{declarations}
{component}
circuit :: HiddenClockResetEnable ZLangSystem => {circuit_type}
{circuit_lhs} = {circuit_result}
 where
{where_block}

topEntity :: {top_type}
topEntity {top_arguments} = exposeClockResetEnable circuit {module.clock} {_top_reset_expression(module)} enableGen{circuit_application}

{{-# ANN topEntity
  (Synthesize
    {{ t_name = "{module.name}"
    , t_inputs = [{', '.join(input_annotations)}]
    , t_output = {output_annotation}
    }}) #-}}
'''


def _ready_valid_declarations() -> str:
    return '''data ZLangReadyValidForward a = ZLangReadyValidForward
  { zlangRvPayload :: a
  , zlangRvValid :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

data ZLangReadyValidBackward = ZLangReadyValidBackward
  { zlangRvReady :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

'''


def _emit_csr_module(module: Module) -> str:
    if not module.is_sequential or module.clock is None or module.reset is None:
        raise ClashEmissionError("CSR blocks require a module clock and reset")
    if (
        module.request_responses
        or module.connections
    ):
        raise ClashEmissionError(
            "CSR composition does not support request/response or connection state"
        )
    if any(port.protocol is not InterfaceProtocol.WIRE for port in module.ports):
        raise ClashEmissionError("CSR hardware connections require wire ports")
    extensions, imports, declarations = _emit_prelude(module)
    extensions = extensions.replace(
        "{-# LANGUAGE NoImplicitPrelude #-}",
        "{-# LANGUAGE TemplateHaskell #-}\n"
        "{-# LANGUAGE NoImplicitPrelude #-}",
    )
    domain = "ZLangSystem"
    bindings: list[str] = []
    csr_driven_outputs: set[str] = set()
    register_words: list[tuple[int, str]] = []
    addresses: list[int] = []

    for block in module.csr_blocks:
        for register in block.registers:
            address = block.base_address + register.offset
            addresses.append(address)
            prefix = _csr_identifier(block.name, register.name)
            hit = f"{prefix}_write_hit"
            bindings.append(
                f"{hit} = (\\address writeRequest -> writeRequest == high && "
                f"address == ({address} :: Unsigned 32)) <$> addr <*> write"
            )
            readable: list[tuple[ir_csr.CsrField, str]] = []
            for field in register.fields:
                field_name = _csr_identifier(block.name, register.name, field.name)
                if field.access is ir_csr.CsrAccess.RESERVED:
                    continue
                state_type = f"BitVector {field.width}"
                initial = f"({field.reset} :: {state_type})"
                if field.access is ir_csr.CsrAccess.READ_ONLY:
                    if (
                        field.binding is not None
                        and field.binding.kind is ir_csr.CsrBindingKind.STATUS
                    ):
                        bindings.append(
                            f"{field_name} = pack <$> {field.binding.signal}"
                        )
                    else:
                        bindings.append(f"{field_name} = pure {initial}")
                else:
                    value = f"{field_name}_write_value"
                    bindings.append(
                        f"{value} = slice d{field.msb} d{field.lsb} <$> wdata"
                    )
                    bindings.append(
                        f"{field_name} = register {initial} {field_name}_next"
                    )
                    if (
                        field.access is ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR
                        and field.binding is not None
                        and field.binding.kind is ir_csr.CsrBindingKind.STICKY
                    ):
                        hardware_set = f"{field_name}_hardware_set"
                        bindings.append(
                            f"{hardware_set} = pack <$> {field.binding.signal}"
                        )
                        if field.binding.priority is ir_csr.CsrPriority.SOFTWARE:
                            update = (
                                "(old .|. hardwareSet) .&. "
                                "complement (if writeHit then incoming else 0)"
                            )
                        else:
                            update = (
                                "(old .&. complement (if writeHit then incoming "
                                "else 0)) .|. hardwareSet"
                            )
                        bindings.append(
                            f"{field_name}_next = (\\old writeHit incoming hardwareSet -> "
                            f"{update}) <$> {field_name} <*> {hit} <*> {value} "
                            f"<*> {hardware_set}"
                        )
                    elif field.access is ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR:
                        bindings.append(
                            f"{field_name}_next = (\\old writeHit incoming -> "
                            f"if writeHit then old .&. complement incoming else old) "
                            f"<$> {field_name} <*> {hit} <*> {value}"
                        )
                    elif field.access is ir_csr.CsrAccess.PULSE:
                        bindings.append(
                            f"{field_name}_next = (\\_ writeHit incoming -> "
                            f"if writeHit then incoming else 0) <$> {field_name} "
                            f"<*> {hit} <*> {value}"
                        )
                    else:
                        bindings.append(
                            f"{field_name}_next = (\\old writeHit incoming -> "
                            f"if writeHit then incoming else old) <$> {field_name} "
                            f"<*> {hit} <*> {value}"
                        )
                    if (
                        field.binding is not None
                        and field.binding.kind is ir_csr.CsrBindingKind.COMMAND
                    ):
                        csr_driven_outputs.add(field.binding.signal)
                        bindings.append(
                            f"{field.binding.signal} = unpack <$> {field_name}"
                        )
                if field.access in {
                    ir_csr.CsrAccess.READ_WRITE,
                    ir_csr.CsrAccess.READ_ONLY,
                    ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR,
                }:
                    readable.append((field, field_name))
            word_name = f"{prefix}_read_word"
            if not readable:
                bindings.append(f"{word_name} = pure (0 :: BitVector 32)")
            else:
                parameters = " ".join(
                    f"field{index}" for index in range(len(readable))
                )
                parts = [
                    f"shiftL (resize field{index} :: BitVector 32) {field.lsb}"
                    for index, (field, _) in enumerate(readable)
                ]
                applications = "".join(
                    (" <$> " if index == 0 else " <*> ") + signal
                    for index, (_, signal) in enumerate(readable)
                )
                bindings.append(
                    f"{word_name} = (\\{parameters} -> {' .|. '.join(parts)})"
                    f"{applications}"
                )
            register_words.append((address, word_name))

    word_parameters = " ".join(
        f"word{index}" for index in range(len(register_words))
    )
    read_cases = "; ".join(
        f"{address} -> word{index}"
        for index, (address, _) in enumerate(register_words)
    )
    word_applications = "".join(
        f" <*> {word}" for _, word in register_words
    )
    bindings.append(
        f"rdata = (\\address readRequest {word_parameters} -> if readRequest == low "
        f"then 0 else case address of {{ {read_cases}; _ -> 0 }}) <$> addr <*> read"
        f"{word_applications}"
    )
    ready_cases = "; ".join(f"{address} -> high" for address in addresses)
    bindings.append(
        "ready = (\\address readRequest writeRequest -> if readRequest == high "
        "|| writeRequest == high then case address of { "
        f"{ready_cases}; _ -> low }} else low) <$> addr <*> read <*> write"
    )

    has_user_transition = bool(
        module.registers or module.next_assignments or module.rules
    )
    has_user_assignments = bool(module.assignments)
    if has_user_transition or has_user_assignments:
        materialized_bindings, render_expression = _storage_materialization(module)
        bindings.extend(materialized_bindings)
        if has_user_transition:
            bindings.append("reset_active = unsafeToActiveHigh hasReset")
            bindings.extend(
                _emit_unified_schedule_bindings(module, render_expression)
            )
            bindings.extend(
                _emit_unified_register_bindings(module, render_expression)
            )

        direct_outputs = _scalar_output_assignments(module)
        groups = (
            ordered_state_groups(module.resolved_transition)
            if has_user_transition and module.resolved_transition is not None
            else ()
        )
        for output in module.outputs:
            if output.name in csr_driven_outputs:
                continue
            value = direct_outputs.get(output.name)
            resource = next(
                (
                    item for item in (
                        module.resolved_transition.resources
                        if module.resolved_transition is not None else ()
                    )
                    if item.kind is StateResourceKind.OUTPUT
                    and item.name == output.name
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
            if value is None and not writers:
                continue
            scheduled: expr.Expression = (
                value if value is not None
                else expr.Constant(0, output.type)
            )
            for group, action in reversed(writers):
                scheduled = expr.Mux(
                    expr.InputRef(
                        _unified_action_enable(
                            module.resolved_transition, group, action,
                            _clash_module_names(module),
                        ),
                        BitType(),
                    ),
                    action.operands[0],
                    scheduled,
                    output.type,
                )
            bindings.append(
                f"{_clash_name(output.name)} = {render_expression(scheduled)}"
            )
    where_block = "\n".join(f"  {binding}" for binding in bindings)

    internal_ports = ir_csr.csr_internal_port_names(
        module.csr_access, module.csr_blocks
    )
    hardware_inputs = [p for p in module.inputs if p.name not in internal_ports]
    hardware_outputs = [p for p in module.outputs if p.name not in internal_ports]
    bus_input_types = [
        f"Signal {domain} (Unsigned 32)",
        f"Signal {domain} Bit",
        f"Signal {domain} (BitVector 32)",
        f"Signal {domain} Bit",
    ]
    hardware_input_types = [
        f"Signal {domain} ({_emit_type(port.type)})" for port in hardware_inputs
    ]
    output_types = [
        f"Signal {domain} (BitVector 32)",
        f"Signal {domain} Bit",
        *(f"Signal {domain} ({_emit_type(port.type)})" for port in hardware_outputs),
    ]
    circuit_signature = " -> ".join(
        (*bus_input_types, *hardware_input_types, _emit_product(output_types))
    )
    input_names = ["addr", "write", "wdata", "read", *(p.name for p in hardware_inputs)]
    result = _emit_product(["rdata", "ready", *(p.name for p in hardware_outputs)])
    top_signature = " -> ".join(
        (f"Clock {domain}", f"Reset {domain}", circuit_signature)
    )
    circuit_arguments = " ".join(input_names)
    top_arguments = " ".join((module.clock, module.reset, *input_names))
    application = " ".join(input_names)
    input_annotations = [
        f'PortName "{module.clock}"',
        f'PortName "{module.reset}"',
        'PortName "addr"',
        'PortName "write"',
        'PortName "wdata"',
        'PortName "read"',
        *(
            _top_value_port_annotation(port.name, port.type)
            for port in hardware_inputs
        ),
    ]
    output_annotations = [
        'PortName "rdata"',
        'PortName "ready"',
        *(
            _top_value_port_annotation(port.name, port.type)
            for port in hardware_outputs
        ),
    ]
    output_annotation = f'PortProduct "" [{", ".join(output_annotations)}]'

    return f'''{extensions}
module {module.name} where

{imports}
{declarations}{_domain_declaration(module, domain)}

circuit :: HiddenClockResetEnable {domain} => {circuit_signature}
circuit {circuit_arguments} = {result}
 where
{where_block}

topEntity :: {top_signature}
topEntity {top_arguments} = exposeClockResetEnable circuit {module.clock} {_top_reset_expression(module)} enableGen {application}

{{-# ANN topEntity
  (Synthesize
    {{ t_name = "{module.name}"
    , t_inputs = [{", ".join(input_annotations)}]
    , t_output = {output_annotation}
    }}) #-}}
'''


def _csr_identifier(*parts: str) -> str:
    return "csr_" + "_".join(parts).lower()


def _emit_packet_arbiter_module(module: Module) -> str:
    """Emit a single fixed-priority or round-robin packet arbiter."""

    if len(module.arbiters) != 1:
        raise ClashEmissionError(
            "packet arbiter backend currently requires exactly one arbiter"
        )
    if module.clock is None or module.reset is None:
        raise ClashEmissionError("packet arbiters require one clock and reset")
    if (
        module.assignments
        or module.connections
        or module.registers
        or module.rules
        or module.fifos
        or module.memories
        or module.csr_blocks
        or module.request_responses
    ):
        raise ClashEmissionError(
            "packet arbiter modules currently contain only arbiter endpoints"
        )
    arbiter = module.arbiters[0]
    endpoints = {
        *(source.name for source in arbiter.sources),
        arbiter.destination.name,
    }
    if {port.name for port in module.ports} != endpoints:
        raise ClashEmissionError(
            "packet arbiter backend currently requires only arbiter endpoints"
        )

    count = len(arbiter.sources)
    owner_width = max(1, (count - 1).bit_length())
    owner_type = f"Unsigned {owner_width}"
    payload_type = _emit_type(arbiter.destination.type)
    source_names = [_clash_name(source.name) for source in arbiter.sources]
    valid_names = [f"{name}_valid" for name in source_names]
    valid_parameters = [f"valid_{index}" for index in range(count)]

    def first_valid(order: list[int]) -> str:
        result = "0"
        for index in reversed(order):
            result = (
                f"if valid_{index} == high then {index} else {result}"
            )
        return result

    valid_application = "".join(
        (" <$> " if index == 0 else " <*> ") + name
        for index, name in enumerate(valid_names)
    )
    if arbiter.policy is ArbitrationPolicy.FIXED_PRIORITY:
        candidate = (
            f"(\\{' '.join(valid_parameters)} -> {first_valid(list(range(count)))})"
            f"{valid_application}"
        )
        priority_bindings: list[str] = []
    else:
        alternatives = "; ".join(
            f"{start} -> {first_valid([(start + offset) % count for offset in range(count)])}"
            for start in range(count)
        )
        candidate = (
            f"(\\priority {' '.join(valid_parameters)} -> case priority of "
            f"{{ {alternatives}; _ -> 0 }}) <$> next_priority"
            + "".join(f" <*> {name}" for name in valid_names)
        )
        increment = (
            f"if selected == {count - 1} then 0 else selected + 1"
        )
        priority_bindings = [
            f"next_priority = register (0 :: {owner_type}) next_priority_next",
            f"next_priority_next = (\\priority selected complete -> if complete == high then {increment} else priority) <$> next_priority <*> selected <*> grant_complete",
        ]

    def selected_signal(signals: list[str], label: str) -> str:
        parameters = [f"{label}_{index}" for index in range(count)]
        alternatives = "; ".join(
            f"{index} -> {parameters[index]}" for index in range(count)
        )
        return (
            f"(\\selected {' '.join(parameters)} -> case selected of "
            f"{{ {alternatives}; _ -> {parameters[0]} }}) <$> selected"
            + "".join(f" <*> {signal}" for signal in signals)
        )

    source_inputs = [
        f"Signal ZLangSystem (ZLangPacketForward ({payload_type}))"
        for _ in arbiter.sources
    ]
    destination_backward_type = "Signal ZLangSystem ZLangPacketBackward"
    output_types = [
        *("Signal ZLangSystem ZLangPacketBackward" for _ in arbiter.sources),
        f"Signal ZLangSystem (ZLangPacketForward ({payload_type}))",
    ]
    input_names = [
        *source_names,
        f"{_clash_name(arbiter.destination.name)}_backward",
    ]
    output_values = [
        *(
            f"ZLangPacketBackward <$> {name}_ready"
            for name in source_names
        ),
        f"ZLangPacketForward <$> {_clash_name(arbiter.destination.name)}_payload "
        f"<*> {_clash_name(arbiter.destination.name)}_valid "
        f"<*> {_clash_name(arbiter.destination.name)}_last",
    ]
    bindings = ["reset_active = unsafeToActiveHigh hasReset"]
    for source, source_name in zip(arbiter.sources, source_names, strict=True):
        bindings.extend(
            (
                f"{source_name}_payload = zlangPacketPayload <$> {source_name}",
                f"{source_name}_valid = zlangPacketValid <$> {source_name}",
                f"{source_name}_last = zlangPacketLast <$> {source_name}",
            )
        )
    destination = _clash_name(arbiter.destination.name)
    bindings.extend(
        (
            f"{destination}_ready = zlangPacketReady <$> {destination}_backward",
            f"candidate = {candidate}",
            f"grant_active = register low grant_active_next",
            f"grant_owner = register (0 :: {owner_type}) grant_owner_next",
            f"selected = (\\active owner available -> if active == high then owner else available) <$> grant_active <*> grant_owner <*> candidate",
            f"selected_valid_raw = {selected_signal(valid_names, 'valid')}",
            f"selected_last = {selected_signal([f'{name}_last' for name in source_names], 'last')}",
            f"selected_payload = {selected_signal([f'{name}_payload' for name in source_names], 'payload')}",
            f"{destination}_valid = (\\valid resetActive -> if resetActive then low else valid) <$> selected_valid_raw <*> reset_active",
            f"{destination}_last = selected_last",
            f"{destination}_payload = selected_payload",
            f"{destination}_transfer = (\\valid ready -> valid .&. ready) <$> {destination}_valid <*> {destination}_ready",
        )
    )
    if arbiter.grant_scope is GrantScope.PACKET:
        bindings.append(
            f"grant_complete = (\\transferred lastBeat -> transferred .&. lastBeat) <$> {destination}_transfer <*> {destination}_last"
        )
    else:
        bindings.append(f"grant_complete = {destination}_transfer")
    bindings.extend(
        (
            "grant_active_next = (\\active selectedValid complete -> if active == high then if complete == high then low else high else if selectedValid == high && complete == low then high else low) <$> grant_active <*> selected_valid_raw <*> grant_complete",
            "grant_owner_next = (\\active owner chosen selectedValid -> if active == low && selectedValid == high then chosen else owner) <$> grant_active <*> grant_owner <*> selected <*> selected_valid_raw",
            *priority_bindings,
        )
    )
    for index, source_name in enumerate(source_names):
        bindings.append(
            f"{source_name}_ready = (\\chosen valid ready resetActive -> if not resetActive && chosen == {index} && valid == high then ready else low) <$> selected <*> {destination}_valid <*> {destination}_ready <*> reset_active"
        )

    extensions, imports, declarations = _emit_prelude(module)
    if "{-# LANGUAGE DeriveAnyClass #-}" not in extensions:
        extensions = extensions.replace(
            "{-# LANGUAGE NoImplicitPrelude #-}",
            "{-# LANGUAGE DeriveAnyClass #-}\n"
            "{-# LANGUAGE DeriveGeneric #-}\n"
            "{-# LANGUAGE NoImplicitPrelude #-}",
        )
    extensions = extensions.replace(
        "{-# LANGUAGE NoImplicitPrelude #-}",
        "{-# LANGUAGE TemplateHaskell #-}\n{-# LANGUAGE NoImplicitPrelude #-}",
    ).rstrip()
    if "import GHC.Generics (Generic)" not in imports:
        imports += "import GHC.Generics (Generic)\n"
    declarations = _packet_declarations() + declarations
    circuit_signature = " -> ".join(
        (*source_inputs, destination_backward_type, _emit_product(output_types))
    )
    top_signature = " -> ".join(
        (
            "Clock ZLangSystem",
            "Reset ZLangSystem",
            *source_inputs,
            destination_backward_type,
            _emit_product(output_types),
        )
    )
    arguments = " ".join(input_names)
    where_block = "\n".join(f"  {binding}" for binding in bindings)
    input_annotations = [
        f'PortName "{module.clock}"',
        f'PortName "{module.reset}"',
        *(
            _top_forward_port_annotation(
                source.name, source.type, "valid", "last"
            )
            for source in arbiter.sources
        ),
        f'PortName "{destination}_ready"',
    ]
    output_annotations = [
        *(f'PortName "{source.name}_ready"' for source in arbiter.sources),
        _top_forward_port_annotation(
            destination, arbiter.destination.type, "valid", "last"
        ),
    ]

    return f'''{extensions}
module {module.name} where

{imports}
{declarations}{_domain_declaration(module, "ZLangSystem")}

circuit :: HiddenClockResetEnable ZLangSystem => {circuit_signature}
circuit {arguments} = {_emit_product(output_values)}
 where
{where_block}

topEntity :: {top_signature}
topEntity {module.clock} {module.reset} {arguments} = exposeClockResetEnable circuit {module.clock} {_top_reset_expression(module)} enableGen {arguments}

{{-# ANN topEntity
  (Synthesize
    {{ t_name = "{module.name}"
    , t_inputs = [{", ".join(input_annotations)}]
    , t_output = PortProduct "" [{", ".join(output_annotations)}]
    }}) #-}}
'''


def _packet_declarations() -> str:
    return '''data ZLangPacketForward a = ZLangPacketForward
  { zlangPacketPayload :: a
  , zlangPacketValid :: Bit
  , zlangPacketLast :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

data ZLangPacketBackward = ZLangPacketBackward
  { zlangPacketReady :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

'''


def _storage_materialization(module: Module):
    """Plan exact typed Signal temporaries for one storage/state circuit.

    The shared planner operates on backend-independent typed expression
    identity.  Clash only supplies legal Haskell names and renders each plan
    item once; no quantization or arithmetic boundary is changed here.
    """

    leaf_names = _clash_child_leaf_names(module)
    preferred = {
        local.expression: _clash_name(local.name)
        for local in module.locals
        if not local.compile_time
    }
    reserved = {
        _clash_name(item.name)
        for item in (
            *module.ports,
            *module.registers,
            *module.locals,
            *module.fifos,
            *module.memories,
            *module.roms,
        )
    }
    materialized = plan_materialization(
        # Clash register reset values are plain values passed to ``register``;
        # they are not rendered through the Signal-expression alias map.  Do
        # not let repeated aggregate reset literals create dead polymorphic
        # ``pure`` bindings with an ambiguous Applicative domain.
        module_expression_roots(module, include_register_initials=False),
        preferred_names=preferred,
        reserved_names=reserved,
    )
    aliases = {item.expression: item.name for item in materialized}

    def render(
        value: expr.Expression,
        *,
        keep: expr.Expression | None = None,
    ) -> str:
        return _emit_signal_expression(
            replace_materialized(value, aliases, keep=keep),
            leaf_names,
        )

    bindings = tuple(
        f"{item.name} = {render(item.expression, keep=item.expression)}"
        for item in materialized
    )
    return bindings, render


def _emit_storage_circuit(
    module: Module,
    function_name: str = "circuit",
    *,
    scalar_catalog: _ScalarChildCatalog | None = None,
    component_owner: _ScalarComponentOwner | None = None,
    include_child_declarations: bool = True,
) -> _StorageCircuitEmission:
    """Build one typed storage-transition circuit declaration.

    Both a standalone storage module and a hierarchical child use this helper.
    It returns the circuit declaration directly; neither caller needs to parse
    or rewrite previously emitted Haskell text.
    """
    if re.fullmatch(r"[a-z][A-Za-z0-9_]*", function_name) is None:
        raise ClashEmissionError(
            f"illegal Clash circuit function name '{function_name}'"
        )
    if function_name in _CLASH_RESERVED:
        raise ClashEmissionError(
            f"Clash circuit function name '{function_name}' is reserved"
        )
    if not module.is_sequential or module.clock is None or module.reset is None:
        raise ClashEmissionError("storage resources require a module clock and reset")
    scheduled_fifo = any(fifo.scheduled for fifo in module.fifos)
    scheduled_memory = any(memory.scheduled for memory in module.memories)
    has_scheduled_state = scheduled_fifo or scheduled_memory
    if (module.registers or module.next_assignments or module.rules) and not has_scheduled_state:
        raise ClashEmissionError(
            "storage modules with user registers or rules are not implemented"
        )
    if has_scheduled_state and any(not fifo.scheduled for fifo in module.fifos):
        raise ClashEmissionError("mixed legacy and scheduled FIFO resources are not implemented")
    if scheduled_memory and any(not memory.scheduled for memory in module.memories):
        raise ClashEmissionError("mixed legacy and scheduled memory resources are not implemented")
    if module.connections or module.request_responses or module.csr_blocks:
        raise ClashEmissionError(
            "storage modules cannot yet be mixed with connection, request/response, "
            "or CSR backends"
        )
    if any(port.protocol is InterfaceProtocol.CREDIT for port in module.ports):
        raise ClashEmissionError(
            "storage modules cannot yet be mixed with credit interfaces"
        )
    if any(
        port.protocol is not InterfaceProtocol.WIRE
        for child in module.children
        for port in child.ports
    ):
        raise ClashEmissionError(
            "storage parents currently require scalar wire child ports"
        )
    if len(module.instances) != len(module.children):
        raise ClashEmissionError(
            "storage hierarchy instance/child cardinality differs"
        )

    private_names = _clash_module_names(module)
    selected_catalog = scalar_catalog
    selected_owner = component_owner
    if module.children:
        selected_catalog = selected_catalog or _scalar_child_catalog(module)
        selected_owner = selected_owner or selected_catalog.root_owner

    extensions, imports, declarations = _emit_prelude(module)
    if module.children and include_child_declarations:
        assert selected_catalog is not None
        declarations += _emit_scalar_child_declarations(
            module, catalog=selected_catalog,
        )
        if declarations and not declarations.endswith("\n"):
            declarations += "\n"
    extensions = extensions.replace(
        "{-# LANGUAGE NoImplicitPrelude #-}",
        "{-# LANGUAGE TemplateHaskell #-}\n{-# LANGUAGE NoImplicitPrelude #-}",
    )
    if any(
        port.protocol is InterfaceProtocol.READY_VALID for port in module.ports
    ):
        if "{-# LANGUAGE DeriveAnyClass #-}" not in extensions:
            extensions = extensions.replace(
                "{-# LANGUAGE NoImplicitPrelude #-}",
                "{-# LANGUAGE DeriveAnyClass #-}\n"
                "{-# LANGUAGE DeriveGeneric #-}\n"
                "{-# LANGUAGE NoImplicitPrelude #-}",
            )
        if "import GHC.Generics (Generic)" not in imports:
            imports += "import GHC.Generics (Generic)\n"
        declarations = _ready_valid_declarations() + declarations

    domain = "ZLangSystem"
    input_types: list[str] = []
    input_names: list[str] = []
    input_annotations: list[str] = [
        f'PortName "{module.clock}"',
        f'PortName "{module.reset}"',
    ]
    output_types: list[str] = []
    output_values: list[str] = []
    output_annotations: list[str] = []
    bindings: list[str] = ["reset_active = unsafeToActiveHigh hasReset"]
    materialized_bindings, render_expression = _storage_materialization(module)
    if module.roms:
        # romFile itself has no reset port.  Keep its undefined/last reset-cycle
        # value masked until the first non-reset edge, without adding a second
        # register to the ROM data path.
        bindings.append("zlang_rom_reset_hold = register True (pure False)")
    for rom in module.roms:
        bindings.extend(_emit_rom_bindings(rom, render_expression))

    for port in module.ports:
        type_ = _emit_type(port.type)
        if port.protocol is InterfaceProtocol.WIRE:
            if port.direction is PortDirection.INPUT:
                input_types.append(f"Signal {domain} ({type_})")
                input_names.append(_clash_name(port.name))
                input_annotations.append(
                    _top_value_port_annotation(port.name, port.type)
                )
            else:
                output_types.append(f"Signal {domain} ({type_})")
                output_values.append(_clash_name(port.name))
                output_annotations.append(
                    _top_value_port_annotation(port.name, port.type)
                )
            continue

        if port.direction is PortDirection.INPUT:
            input_types.append(
                f"Signal {domain} (ZLangReadyValidForward ({type_}))"
            )
            input_names.append(port.name)
            input_annotations.append(
                _top_forward_port_annotation(port.name, port.type, "valid")
            )
            bindings.extend(
                (
                    f"{port.name}_payload = zlangRvPayload <$> {port.name}",
                    f"{port.name}_valid = zlangRvValid <$> {port.name}",
                )
            )
            output_types.append(f"Signal {domain} ZLangReadyValidBackward")
            output_values.append(
                f"ZLangReadyValidBackward <$> {port.name}_ready"
            )
            output_annotations.append(f'PortName "{port.name}_ready"')
        else:
            backward = f"{port.name}_backward"
            input_types.append(f"Signal {domain} ZLangReadyValidBackward")
            input_names.append(backward)
            input_annotations.append(f'PortName "{port.name}_ready"')
            bindings.append(
                f"{port.name}_ready = zlangRvReady <$> {backward}"
            )
            output_types.append(
                f"Signal {domain} (ZLangReadyValidForward ({type_}))"
            )
            output_values.append(
                f"ZLangReadyValidForward <$> {port.name}_payload "
                f"<*> {port.name}_valid"
            )
            output_annotations.append(
                _top_forward_port_annotation(port.name, port.type, "valid")
            )

    for port in module.ports:
        if port.protocol is InterfaceProtocol.READY_VALID:
            bindings.append(
                f"{port.name}_transfer = (\\valid ready -> valid .&. ready) "
                f"<$> {port.name}_valid <*> {port.name}_ready"
            )

    for instance, child in zip(
        module.instances, module.children, strict=True
    ):
        child_bindings = {
            item.port: item.expression
            for item in module.instance_bindings
            if item.instance == instance.name
        }
        missing = [
            port.name
            for port in child.inputs
            if port.name not in child_bindings
        ]
        if missing:
            raise ClashEmissionError(
                f"instance '{instance.name}' is missing bindings for "
                f"{', '.join(missing)}"
            )
        applications = [
            render_expression(child_bindings[port.name])
            for port in child.inputs
        ]
        function = _scalar_child_function_for_instance(
            module,
            instance.name,
            selected_catalog,
            selected_owner,
        )
        application = _emit_child_application(
            function,
            applications,
            sequential_child=child.is_sequential,
        )
        child_outputs = [
            private_names.child_signal(instance.name, port.name)
            for port in child.outputs
        ]
        bindings.append(
            f"{_emit_product(child_outputs)} = {application}"
        )

    if has_scheduled_state:
        bindings.extend(_emit_unified_schedule_bindings(module, render_expression))
    for fifo in module.fifos:
        if fifo.scheduled:
            bindings.extend(
                _emit_scheduled_fifo_bindings(module, fifo, render_expression)
            )
        else:
            bindings.extend(_emit_declared_fifo_bindings(fifo, render_expression))
    for memory in module.memories:
        bindings.extend(
            _emit_scheduled_memory_bindings(module, memory, render_expression)
            if memory.scheduled else _emit_memory_bindings(memory, render_expression)
        )
    bindings.extend(materialized_bindings)
    scheduled_output_names = {
        resource.name
        for resource in (
            module.resolved_transition.resources
            if module.resolved_transition is not None else ()
        )
        if resource.kind is StateResourceKind.OUTPUT
    }
    for assignment in module.assignments:
        if _assignment_name(assignment) in scheduled_output_names:
            continue
        bindings.append(
            f"{_clash_name(_assignment_name(assignment))} = "
            f"{render_expression(assignment.expression)}"
        )

    if has_scheduled_state and module.resolved_transition is not None:
        bindings.extend(
            _emit_unified_register_bindings(module, render_expression)
        )
        bindings.extend(
            _emit_unified_output_bindings(
                module,
                render_expression,
                include=scheduled_output_names,
            )
        )

    output_type = _emit_product(output_types)
    result = _emit_product(output_values)
    circuit_signature = " -> ".join((*input_types, output_type))
    circuit_arguments = " ".join(input_names)
    circuit_lhs = (
        f"{function_name} {circuit_arguments}"
        if circuit_arguments else function_name
    )
    where_block = "\n".join(f"  {binding}" for binding in bindings)
    circuit = f'''{function_name} :: HiddenClockResetEnable {domain} => {circuit_signature}
{circuit_lhs} = {result}
 where
{where_block}
'''
    return _StorageCircuitEmission(
        extensions=extensions,
        imports=imports,
        declarations=declarations,
        domain=domain,
        input_types=tuple(input_types),
        input_names=tuple(input_names),
        input_annotations=tuple(input_annotations),
        output_type=output_type,
        output_annotations=tuple(output_annotations),
        circuit=circuit,
    )


def _emit_storage_module(module: Module) -> str:
    """Emit a standalone storage module around the shared typed circuit."""
    emission = _emit_storage_circuit(module)
    top_signature = " -> ".join(
        (
            f"Clock {emission.domain}",
            f"Reset {emission.domain}",
            *emission.input_types,
            emission.output_type,
        )
    )
    top_arguments = " ".join((module.clock, module.reset, *emission.input_names))
    application = (
        f" {' '.join(emission.input_names)}"
        if emission.input_names else ""
    )
    input_ports = ", ".join(emission.input_annotations)
    output_port = (
        emission.output_annotations[0]
        if len(emission.output_annotations) == 1
        else f'PortProduct "" [{", ".join(emission.output_annotations)}]'
    )
    return f'''{emission.extensions}
module {module.name} where

{emission.imports}
{emission.declarations}{_domain_declaration(module, emission.domain)}

{emission.circuit}
topEntity :: {top_signature}
topEntity {top_arguments} = exposeClockResetEnable circuit {module.clock} {_top_reset_expression(module)} enableGen{application}

{{-# ANN topEntity
  (Synthesize
    {{ t_name = "{module.name}"
    , t_inputs = [{input_ports}]
    , t_output = {output_port}
    }}) #-}}
'''


def _emit_declared_fifo_bindings(fifo: object, render=None) -> tuple[str, ...]:
    if render is None:
        render = lambda value: _emit_signal_expression(value, {})
    name = fifo.name
    depth = fifo.depth
    count_type = f"Unsigned {fifo.count_width}"
    payload_type = _emit_type(fifo.element_type)
    data = render(fifo.data)
    push = render(fifo.push)
    pop = render(fifo.pop)
    return (
        f"{name}_data_request = {data}",
        f"{name}_push_request = {push}",
        f"{name}_pop_request = {pop}",
        f"{name}_count = register (0 :: {count_type}) {name}_count_next",
        f"{name}_slots = register (repeat (deepErrorX \"empty FIFO {name}\") :: Vec {depth} ({payload_type})) {name}_slots_next",
        f"{name}_empty = (\\count -> if count == 0 then high else low) <$> {name}_count",
        f"{name}_full = (\\count -> if count >= {depth} then high else low) <$> {name}_count",
        f"{name}_valid = (\\count resetActive -> if resetActive || count == 0 then low else high) <$> {name}_count <*> reset_active",
        f"{name}_dequeue = (\\requested count resetActive -> if not resetActive && requested == high && count > 0 then high else low) <$> {name}_pop_request <*> {name}_count <*> reset_active",
        f"{name}_ready = (\\count dequeued resetActive -> if not resetActive && (count < {depth} || dequeued == high) then high else low) <$> {name}_count <*> {name}_dequeue <*> reset_active",
        f"{name}_enqueue = (\\requested ready -> requested .&. ready) <$> {name}_push_request <*> {name}_ready",
        f"{name}_overflow = (\\requested count dequeued resetActive -> if not resetActive && requested == high && count >= {depth} && dequeued == low then high else low) <$> {name}_push_request <*> {name}_count <*> {name}_dequeue <*> reset_active",
        f"{name}_underflow = (\\requested count resetActive -> if not resetActive && requested == high && count == 0 then high else low) <$> {name}_pop_request <*> {name}_count <*> reset_active",
        f"{name}_front = head <$> {name}_slots",
        f"{name}_count_next = (\\count enqueued dequeued -> case (enqueued == high, dequeued == high) of {{ (True, False) -> count + 1; (False, True) -> count - 1; _ -> count }}) <$> {name}_count <*> {name}_enqueue <*> {name}_dequeue",
        f"{name}_slots_next = (\\slots count enqueued dequeued payload -> case (enqueued == high, dequeued == high) of {{ (True, False) -> replace (fromIntegral count) payload slots; (False, True) -> slots <<+ deepErrorX \"empty FIFO {name}\"; (True, True) -> replace (fromIntegral (count - 1)) payload (slots <<+ deepErrorX \"empty FIFO {name}\"); _ -> slots }}) <$> {name}_slots <*> {name}_count <*> {name}_enqueue <*> {name}_dequeue <*> {name}_data_request",
    )


def _emit_unified_schedule_bindings(module: Module, render=None) -> tuple[str, ...]:
    private_names = _clash_module_names(module)
    if render is None:
        render = lambda value: _emit_signal_expression(value, {})
    transition = module.resolved_transition
    assert transition is not None
    groups = ordered_state_groups(transition)
    conditional = conditional_actions(transition)
    activation_predicates = conditional_activation_predicates(transition)
    activation_names = tuple(
        f"zlang_condition_{index}_active"
        for index in range(len(activation_predicates))
    )
    enable_names = {
        action.semantic_id: f"zlang_action_{index}_enable"
        for index, action in enumerate(conditional)
    }
    lines = [
        f"{private_names.rule(group.rule_name, 'guard')} = {render(group.guard)}"
        for group in groups
    ]
    lines.extend(
        f"{activation_names[index]} = {render(activation)}"
        for index, activation in enumerate(activation_predicates)
    )
    scheduled_fifo_names = {
        resource.name
        for resource in transition.resources
        if resource.kind is StateResourceKind.FIFO
    }
    scheduled_fifos = [
        fifo for fifo in module.fifos
        if fifo.name in scheduled_fifo_names
    ]
    count_names = [fifo.name for fifo in scheduled_fifos]
    guard_names = [group.rule_name for group in groups]
    guard_parameters = {name: f"guard_{index}" for index, name in enumerate(guard_names)}
    parameters = [
        *(f"count_{name}" for name in count_names),
        *(guard_parameters[name] for name in guard_names),
        *(f"active_{index}" for index in range(len(activation_predicates))),
        "resetActive",
    ]
    applications = [
        *(f"{name}_count" for name in count_names),
        *(private_names.rule(name, 'guard') for name in guard_names),
        *activation_names,
        "reset_active",
    ]
    for selected_group in groups:
        clauses: list[str] = []
        for region in selection_regions(transition, selected_group.rule_name):
            count_values = region[:len(count_names)]
            guard_values = region[
                len(count_names):len(count_names) + len(guard_names)
            ]
            activation_values = region[
                len(count_names) + len(guard_names):
            ]
            terms: list[str] = []
            for name, fifo, value in zip(
                count_names, scheduled_fifos, count_values, strict=True
            ):
                if value is FifoOccupancy.EMPTY:
                    terms.append(f"count_{name} == 0")
                elif value is FifoOccupancy.FULL:
                    terms.append(f"count_{name} == {fifo.depth}")
                elif value is FifoOccupancy.MIDDLE:
                    terms.append(
                        f"(count_{name} > 0 && count_{name} < {fifo.depth})"
                    )
            terms.extend(
                f"{guard_parameters[name]} == {'high' if value else 'low'}"
                for name, value in zip(
                    guard_names, guard_values, strict=True
                )
                if value is not None
            )
            terms.extend(
                f"active_{index} == {'high' if value else 'low'}"
                for index, value in enumerate(activation_values)
                if value is not None
            )
            clauses.append("(" + " && ".join(terms) + ")")
        condition = " || ".join(clauses) if clauses else "False"
        lambda_ = "\\" + " ".join(parameters) + " -> "
        application = " <$> " + " <*> ".join(applications)
        lines.append(
            f"{private_names.rule(selected_group.rule_name, 'fire')} = ({lambda_}if not resetActive && ({condition}) then high else low){application}"
        )
    lines.extend(
        f"{enable_names[action.semantic_id]} = (\\fire active -> fire .&. active) "
        f"<$> {private_names.rule(group.rule_name, 'fire')} "
        f"<*> {activation_names[action_activation_predicate_index(transition, action)]}"
        for group in groups
        for action in group.actions
        if action.activation is not None
    )
    return tuple(lines)


def _unified_action_enable(
    transition: object,
    group: object,
    action: object,
    names=None,
) -> str:
    """Return the Signal whose high value commits this exact effect."""

    if action.activation is None:
        if names is None:
            raise ClashEmissionError("scheduled action lacks its physical rule-name plan")
        return names.rule(group.rule_name, "fire")
    names = {
        candidate.semantic_id: f"zlang_action_{index}_enable"
        for index, candidate in enumerate(conditional_actions(transition))
    }
    return names[action.semantic_id]


def _emit_unified_register_bindings(
    module: Module,
    render=None,
    *,
    parenthesize_next: bool = False,
) -> tuple[str, ...]:
    """Lower ordinary register writes from the authoritative transition.

    This helper deliberately contains no scheduler: rule-fire values come from
    :func:`_emit_unified_schedule_bindings`, which consumes the already-resolved
    backend-independent action groups and priorities.  It is shared by state
    engines such as the composed CSR path that own disjoint physical state.
    """

    private_names = _clash_module_names(module)
    if render is None:
        render = lambda value: _emit_signal_expression(value, {})
    transition = module.resolved_transition
    if transition is None:
        if module.registers or module.next_assignments or module.rules:
            raise ClashEmissionError(
                "ordinary register/rule state lacks resolved transition IR"
            )
        return ()
    groups = ordered_state_groups(transition)
    next_by_register = {
        item.target.name: item.expression for item in module.next_assignments
    }
    lines: list[str] = []
    for register in module.registers:
        next_signal = (
            f"({register.name}_next)"
            if parenthesize_next else f"{register.name}_next"
        )
        lines.append(
            f"{register.name} = register {_emit_expression(register.initial)} "
            f"{next_signal}"
        )
        resource_id = next(
            item.semantic_id for item in transition.resources
            if item.kind.value == "register" and item.name == register.name
        )
        scheduled: expr.Expression = next_by_register.get(
            register.name, expr.RegisterRef(register.name, register.type)
        )
        for group in reversed(groups):
            for action in reversed(group.actions):
                if (
                    action.resource_id != resource_id
                    or action.kind is not StateActionKind.REGISTER_WRITE
                ):
                    continue
                scheduled = expr.Mux(
                    expr.InputRef(
                        _unified_action_enable(transition, group, action, private_names),
                        BitType(),
                    ),
                    action.operands[0], scheduled, register.type,
                )
        lines.append(f"{register.name}_next = {render(scheduled)}")
    return tuple(lines)


def _emit_unified_output_bindings(
    module: Module,
    render=None,
    *,
    include: set[str] | None = None,
    output_names: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Lower scalar rule outputs through the authoritative action schedule."""

    private_names = _clash_module_names(module)
    if render is None:
        render = lambda value: _emit_signal_expression(value, {})
    transition = module.resolved_transition
    if transition is None:
        return ()
    groups = ordered_state_groups(transition)
    direct = {
        assignment.target.name: assignment.expression
        for assignment in module.assignments
        if (
            isinstance(assignment.target, Port)
            and assignment.target.direction is PortDirection.OUTPUT
            and assignment.target.protocol is InterfaceProtocol.WIRE
            and assignment.signal is None
            and assignment.channel is None
        )
    }
    lines: list[str] = []
    for output in module.outputs:
        if output.protocol is not InterfaceProtocol.WIRE:
            continue
        if include is not None and output.name not in include:
            continue
        resource = next(
            (
                item for item in transition.resources
                if item.kind is StateResourceKind.OUTPUT
                and item.name == output.name
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
        value = direct.get(output.name)
        if value is None and not writers:
            continue
        scheduled: expr.Expression = (
            value if value is not None else expr.Constant(0, output.type)
        )
        for group, action in reversed(writers):
            scheduled = expr.Mux(
                expr.InputRef(
                    _unified_action_enable(transition, group, action, private_names),
                    BitType(),
                ),
                action.operands[0],
                scheduled,
                output.type,
            )
        physical_name = (output_names or {}).get(
            output.name, _clash_name(output.name)
        )
        lines.append(f"{physical_name} = {render(scheduled)}")
    return tuple(lines)


def _emit_scheduled_fifo_bindings(module: Module, fifo: object, render=None) -> tuple[str, ...]:
    private_names = _clash_module_names(module)
    if render is None:
        render = lambda value: _emit_signal_expression(value, {})
    transition = module.resolved_transition
    assert transition is not None
    groups = ordered_state_groups(transition)
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

    payload = f"pure (deepErrorX \"no FIFO push payload for {fifo.name}\")"
    for group, action in reversed(push_actions):
        enable = _unified_action_enable(transition, group, action, private_names)
        payload = (
            f"(\\fire value fallback -> if fire == high then value else fallback) "
            f"<$> {enable} "
            f"<*> ({render(action.operands[0])}) <*> ({payload})"
        )
    name = fifo.name
    depth = fifo.depth
    count_type = f"Unsigned {fifo.count_width}"
    payload_type = _emit_type(fifo.element_type)
    return (
        f"{name}_data_request = {payload}",
        f"{name}_enqueue = {_clash_or_signals([_unified_action_enable(transition, group, action, private_names) for group, action in push_actions])}",
        f"{name}_dequeue = {_clash_or_signals([_unified_action_enable(transition, group, action, private_names) for group, action in pop_actions])}",
        f"{name}_push = {name}_enqueue",
        f"{name}_pop = {name}_dequeue",
        f"{name}_count = register (0 :: {count_type}) {name}_count_next",
        f"{name}_slots = register (repeat (deepErrorX \"empty FIFO {name}\") :: Vec {depth} ({payload_type})) {name}_slots_next",
        f"{name}_empty = (\\count -> if count == 0 then high else low) <$> {name}_count",
        f"{name}_full = (\\count -> if count >= {depth} then high else low) <$> {name}_count",
        f"{name}_valid = (\\count resetActive -> if resetActive || count == 0 then low else high) <$> {name}_count <*> reset_active",
        f"{name}_ready = (\\count resetActive -> if not resetActive && count < {depth} then high else low) <$> {name}_count <*> reset_active",
        f"{name}_overflow = (\\_ -> low) <$> reset_active",
        f"{name}_underflow = (\\_ -> low) <$> reset_active",
        f"{name}_front = head <$> {name}_slots",
        f"{name}_count_next = (\\count enqueued dequeued -> case (enqueued == high, dequeued == high) of {{ (True, False) -> count + 1; (False, True) -> count - 1; _ -> count }}) <$> {name}_count <*> {name}_enqueue <*> {name}_dequeue",
        f"{name}_slots_next = (\\slots count enqueued dequeued payload -> case (enqueued == high, dequeued == high) of {{ (True, False) -> replace (fromIntegral count) payload slots; (False, True) -> slots <<+ deepErrorX \"empty FIFO {name}\"; (True, True) -> replace (fromIntegral (count - 1)) payload (slots <<+ deepErrorX \"empty FIFO {name}\"); _ -> slots }}) <$> {name}_slots <*> {name}_count <*> {name}_enqueue <*> {name}_dequeue <*> {name}_data_request",
    )


def _memory_byte_mask_expression(
    mask_name: str,
    *,
    element_width: int,
    lane_count: int,
) -> str:
    """Render the exact packed byte mask, truncating only absent MSB bits."""

    expected_lanes = (element_width + 7) // 8
    if lane_count != expected_lanes:
        raise ClashEmissionError(
            "memory write-mask width does not match its element width"
        )
    expanded_width = lane_count * 8
    expanded = (
        f"(pack (concatMap (\\lane -> repeat lane :: Vec 8 Bit) "
        f"(unpack {mask_name} :: Vec {lane_count} Bit)) :: "
        f"BitVector {expanded_width})"
    )
    if expanded_width == element_width:
        return expanded
    return f"(resize {expanded} :: BitVector {element_width})"


def _emit_memory_bindings(memory: object, render=None) -> tuple[str, ...]:
    if (
        memory.read_latency != 1
        or memory.contents_reset is not MemoryResetPolicy.CLEAR
        or memory.read_data_reset is not MemoryResetPolicy.CLEAR
    ):
        return _emit_profiled_memory_bindings(memory, render)
    if render is None:
        render = lambda value: _emit_signal_expression(value, {})
    name = memory.name
    element_type = _emit_type(memory.element_type)
    zero = _zero_value(memory.element_type)
    read_address = render(memory.read_address)
    write_enable = render(memory.write_enable)
    write_address = render(memory.write_address)
    write_data = render(memory.write_data)
    if memory.write_mask_width is not None:
        write_mask = render(memory.write_mask)
        old_selected = f"cells !! ({memory.depth - 1} :: Index {memory.depth})"
        for index in reversed(range(memory.depth - 1)):
            old_selected = (
                f"if writeAddress == {index} then "
                f"cells !! ({index} :: Index {memory.depth}) else ({old_selected})"
            )
        effective_mask = _memory_byte_mask_expression(
            "mask",
            element_width=memory.element_type.width,
            lane_count=memory.write_mask_width,
        )
        merged = (
            f"{name}_write_merged = (\\cells writeAddress newValue mask -> "
            f"let oldValue = {old_selected}; "
            f"effectiveMask = {effective_mask} in "
            f"bitCoerce (((pack oldValue :: BitVector {memory.element_type.width}) .&. complement effectiveMask) .|. "
            f"((pack newValue :: BitVector {memory.element_type.width}) .&. effectiveMask))) "
            f"<$> {name}_cells <*> {name}_write_address <*> {name}_write_data <*> {name}_write_mask"
        )
        selected = f"cells !! ({memory.depth - 1} :: Index {memory.depth})"
        for index in reversed(range(memory.depth - 1)):
            selected = (
                f"if readAddress == {index} then "
                f"cells !! ({index} :: Index {memory.depth}) else ({selected})"
            )
        if memory.collision is MemoryCollision.WRITE_FIRST:
            read_value = (
                f"{name}_read_value = (\\cells readAddress writeEnable writeAddress mergedValue -> "
                f"if writeEnable == high && readAddress == writeAddress then mergedValue "
                f"else ({selected})) <$> {name}_cells "
                f"<*> {name}_read_address <*> {name}_write_enable "
                f"<*> {name}_write_address <*> {name}_write_merged"
            )
        else:
            read_value = (
                f"{name}_read_value = (\\cells readAddress -> {selected}) "
                f"<$> {name}_cells <*> {name}_read_address"
            )
        return (
            f"{name}_read_address = {read_address}",
            f"{name}_write_enable = {write_enable}",
            f"{name}_write_address = {write_address}",
            f"{name}_write_data = {write_data}",
            f"{name}_write_mask = {write_mask}",
            f"{name}_cells = register (repeat {zero} :: Vec {memory.depth} ({element_type})) {name}_cells_next",
            merged,
            read_value,
            f"{name}_read_data = register {zero} {name}_read_value",
            f"{name}_cells_next = (\\cells writeEnable writeAddress mergedValue -> if writeEnable == high then replace (bitCoerce writeAddress :: Index {memory.depth}) mergedValue cells else cells) <$> {name}_cells <*> {name}_write_enable <*> {name}_write_address <*> {name}_write_merged",
        )
    # Clash's dynamic Vec indexing primitive carries a machine-sized index into
    # generated RTL even when the semantic address and Index representations
    # are the same small width.  A fixed-depth typed selector keeps every
    # comparison and leaf at the source address/element widths.
    selected = f"cells !! ({memory.depth - 1} :: Index {memory.depth})"
    for index in reversed(range(memory.depth - 1)):
        selected = (
            f"if readAddress == {index} then "
            f"cells !! ({index} :: Index {memory.depth}) else ({selected})"
        )
    if memory.collision is MemoryCollision.WRITE_FIRST:
        read_value = (
            f"{name}_read_value = (\\cells readAddress writeEnable writeAddress writeData -> "
            f"if writeEnable == high && readAddress == writeAddress then writeData "
            f"else ({selected})) <$> {name}_cells "
            f"<*> {name}_read_address <*> {name}_write_enable "
            f"<*> {name}_write_address <*> {name}_write_data"
        )
    else:
        read_value = (
            f"{name}_read_value = (\\cells readAddress -> {selected}) "
            f"<$> {name}_cells <*> {name}_read_address"
        )
    return (
        f"{name}_read_address = {read_address}",
        f"{name}_write_enable = {write_enable}",
        f"{name}_write_address = {write_address}",
        f"{name}_write_data = {write_data}",
        f"{name}_cells = register (repeat {zero} :: Vec {memory.depth} ({element_type})) {name}_cells_next",
        read_value,
        f"{name}_read_data = register {zero} {name}_read_value",
        f"{name}_cells_next = (\\cells writeEnable writeAddress writeData -> if writeEnable == high then replace (bitCoerce writeAddress :: Index {memory.depth}) writeData cells else cells) <$> {name}_cells <*> {name}_write_enable <*> {name}_write_address <*> {name}_write_data",
    )


def _profiled_memory_register(
    initial: str,
    next_value: str,
    policy: MemoryResetPolicy,
) -> str:
    """Render one state cell under an explicitly typed memory reset policy."""

    register = f"register {initial} {next_value}"
    if policy is MemoryResetPolicy.CLEAR:
        return register
    if policy is MemoryResetPolicy.PRESERVE:
        return f"withReset noReset ({register})"
    raise ClashEmissionError(f"unsupported memory reset policy '{policy}'")


def _emit_profiled_memory_bindings(memory: object, render=None) -> tuple[str, ...]:
    """Emit non-legacy global memory profiles without changing legacy text.

    ``noReset`` removes the enclosing component reset from preserved state.
    The explicit active-write signal is still reset-gated, so a preserved
    memory cannot commit a write (or expose a write-first bypass) while reset
    is active.
    """

    if memory.scheduled:
        raise ClashEmissionError(
            "scheduled memory must use the scheduled Clash lowering"
        )
    if memory.read_latency not in {0, 1}:
        raise ClashEmissionError("memory read latency must be zero or one")
    if render is None:
        render = lambda value: _emit_signal_expression(value, {})

    name = memory.name
    element_type = _emit_type(memory.element_type)
    zero = _zero_value(memory.element_type)
    bindings = [
        f"{name}_read_address = {render(memory.read_address)}",
        f"{name}_write_enable = {render(memory.write_enable)}",
        f"{name}_write_address = {render(memory.write_address)}",
        f"{name}_write_data = {render(memory.write_data)}",
    ]
    if memory.write_mask_width is not None:
        bindings.append(f"{name}_write_mask = {render(memory.write_mask)}")
    bindings.append(
        f"{name}_write_active = (\\resetActive writeEnable -> "
        f"if resetActive then low else writeEnable) <$> reset_active "
        f"<*> {name}_write_enable"
    )
    bindings.append(
        f"{name}_cells = "
        + _profiled_memory_register(
            f"(repeat {zero} :: Vec {memory.depth} ({element_type}))",
            f"{name}_cells_next",
            memory.contents_reset,
        )
    )

    effective_write_data = f"{name}_write_data"
    if memory.write_mask_width is not None:
        old_selected = f"cells !! ({memory.depth - 1} :: Index {memory.depth})"
        for index in reversed(range(memory.depth - 1)):
            old_selected = (
                f"if writeAddress == {index} then "
                f"cells !! ({index} :: Index {memory.depth}) else ({old_selected})"
            )
        effective_mask = _memory_byte_mask_expression(
            "mask",
            element_width=memory.element_type.width,
            lane_count=memory.write_mask_width,
        )
        bindings.append(
            f"{name}_write_merged = (\\cells writeAddress newValue mask -> "
            f"let oldValue = {old_selected}; "
            f"effectiveMask = {effective_mask} in "
            f"bitCoerce (((pack oldValue :: BitVector {memory.element_type.width}) .&. complement effectiveMask) .|. "
            f"((pack newValue :: BitVector {memory.element_type.width}) .&. effectiveMask))) "
            f"<$> {name}_cells <*> {name}_write_address <*> {name}_write_data <*> {name}_write_mask"
        )
        effective_write_data = f"{name}_write_merged"

    selected = f"cells !! ({memory.depth - 1} :: Index {memory.depth})"
    for index in reversed(range(memory.depth - 1)):
        selected = (
            f"if readAddress == {index} then "
            f"cells !! ({index} :: Index {memory.depth}) else ({selected})"
        )
    if memory.collision is MemoryCollision.WRITE_FIRST:
        read_body = (
            f"if writeActive == high && readAddress == writeAddress "
            f"then writeData else ({selected})"
        )
    else:
        read_body = selected
    bindings.append(
        f"{name}_read_value = (\\cells readAddress writeActive writeAddress writeData -> "
        f"{read_body}) <$> {name}_cells <*> {name}_read_address "
        f"<*> {name}_write_active <*> {name}_write_address <*> {effective_write_data}"
    )

    if memory.read_latency == 0:
        if memory.read_data_reset is MemoryResetPolicy.CLEAR:
            bindings.append(
                f"{name}_read_data = (\\resetActive value -> "
                f"if resetActive then {zero} else value) <$> reset_active "
                f"<*> {name}_read_value"
            )
        elif memory.read_data_reset is MemoryResetPolicy.PRESERVE:
            bindings.append(f"{name}_read_data = {name}_read_value")
        else:
            raise ClashEmissionError(
                f"unsupported memory read-data reset policy "
                f"'{memory.read_data_reset}'"
            )
    else:
        bindings.append(
            f"{name}_read_data = "
            + _profiled_memory_register(
                zero,
                (
                    f"{name}_read_value"
                    if memory.read_data_reset is MemoryResetPolicy.CLEAR
                    else f"{name}_read_data_next"
                ),
                memory.read_data_reset,
            )
        )
        if memory.read_data_reset is MemoryResetPolicy.PRESERVE:
            bindings.append(
                f"{name}_read_data_next = (\\resetActive value old -> "
                f"if resetActive then old else value) <$> reset_active "
                f"<*> {name}_read_value <*> {name}_read_data"
            )

    bindings.append(
        f"{name}_cells_next = (\\cells writeActive writeAddress writeData -> "
        f"if writeActive == high then replace "
        f"(bitCoerce writeAddress :: Index {memory.depth}) writeData cells else cells) "
        f"<$> {name}_cells <*> {name}_write_active <*> {name}_write_address "
        f"<*> {effective_write_data}"
    )
    return tuple(bindings)


def _emit_scheduled_memory_bindings(module: Module, memory: object, render=None) -> tuple[str, ...]:
    private_names = _clash_module_names(module)
    """Emit one rule-owned synchronous memory from the shared transition."""

    if render is None:
        render = lambda value: _emit_signal_expression(value, {})
    transition = module.resolved_transition
    assert transition is not None and memory.scheduled
    groups = ordered_state_groups(transition)
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

    address_type = f"Unsigned {memory.address_width}"
    read_address = f"pure (0 :: {address_type})"
    for group, action in reversed(read_actions):
        enable = _unified_action_enable(transition, group, action, private_names)
        read_address = (
            f"(\\fire value fallback -> if fire == high then value else fallback) "
            f"<$> {enable} "
            f"<*> ({render(action.operands[0])}) "
            f"<*> ({read_address})"
        )
    write_address = f"pure (0 :: {address_type})"
    write_data = f"pure ({_zero_value(memory.element_type)})"
    write_mask = (
        f"pure (0 :: BitVector {memory.write_mask_width})"
        if memory.write_mask_width is not None else None
    )
    for group, action in reversed(write_actions):
        fire = _unified_action_enable(transition, group, action, private_names)
        write_address = (
            f"(\\selected value fallback -> if selected == high then value else fallback) "
            f"<$> {fire} <*> ({render(action.operands[0])}) "
            f"<*> ({write_address})"
        )
        write_data = (
            f"(\\selected value fallback -> if selected == high then value else fallback) "
            f"<$> {fire} <*> ({render(action.operands[1])}) "
            f"<*> ({write_data})"
        )
        if write_mask is not None:
            write_mask = (
                f"(\\selected value fallback -> if selected == high then value else fallback) "
                f"<$> {fire} <*> ({render(action.operands[2])}) "
                f"<*> ({write_mask})"
            )

    name = memory.name
    element_type = _emit_type(memory.element_type)
    zero = _zero_value(memory.element_type)
    profiled_reset = (
        memory.contents_reset is not MemoryResetPolicy.CLEAR
        or memory.read_data_reset is not MemoryResetPolicy.CLEAR
    )
    read_fire_name = f"{name}_read_active" if profiled_reset else f"{name}_read_fire"
    write_fire_name = f"{name}_write_active" if profiled_reset else f"{name}_write_fire"
    merged_binding: str | None = None
    effective_write_data = f"{name}_write_data"
    if write_mask is not None:
        old_selected = f"cells !! ({memory.depth - 1} :: Index {memory.depth})"
        for index in reversed(range(memory.depth - 1)):
            old_selected = (
                f"if writeAddress == {index} then "
                f"cells !! ({index} :: Index {memory.depth}) else ({old_selected})"
            )
        effective_mask = _memory_byte_mask_expression(
            "mask",
            element_width=memory.element_type.width,
            lane_count=memory.write_mask_width,
        )
        merged_binding = (
            f"{name}_write_merged = (\\cells writeAddress newValue mask -> "
            f"let oldValue = {old_selected}; "
            f"effectiveMask = {effective_mask} in "
            f"bitCoerce (((pack oldValue :: BitVector {memory.element_type.width}) .&. complement effectiveMask) .|. "
            f"((pack newValue :: BitVector {memory.element_type.width}) .&. effectiveMask))) "
            f"<$> {name}_cells <*> {name}_write_address <*> {name}_write_data <*> {name}_write_mask"
        )
        effective_write_data = f"{name}_write_merged"
    selected = f"cells !! ({memory.depth - 1} :: Index {memory.depth})"
    for index in reversed(range(memory.depth - 1)):
        selected = (
            f"if readAddress == {index} then "
            f"cells !! ({index} :: Index {memory.depth}) else ({selected})"
        )
    read_value = (
        f"{name}_read_value = (\\cells readAddress writeFire writeAddress writeData -> "
        + (
            f"if writeFire == high && readAddress == writeAddress then writeData else ({selected})"
            if memory.collision is MemoryCollision.WRITE_FIRST
            else selected
        )
        + f") <$> {name}_cells <*> {name}_read_address <*> {write_fire_name} "
        f"<*> {name}_write_address <*> {effective_write_data}"
    )
    bindings = [
        f"{name}_read_fire = {_clash_or_signals([_unified_action_enable(transition, group, action, private_names) for group, action in read_actions])}",
        f"{name}_write_fire = {_clash_or_signals([_unified_action_enable(transition, group, action, private_names) for group, action in write_actions])}",
        f"{name}_read_address = {read_address}",
        f"{name}_write_address = {write_address}",
        f"{name}_write_data = {write_data}",
    ]
    if write_mask is not None:
        bindings.append(f"{name}_write_mask = {write_mask}")
    if profiled_reset:
        bindings.extend((
            f"{name}_read_active = (\\resetActive fire -> "
            f"if resetActive then low else fire) <$> reset_active "
            f"<*> {name}_read_fire",
            f"{name}_write_active = (\\resetActive fire -> "
            f"if resetActive then low else fire) <$> reset_active "
            f"<*> {name}_write_fire",
        ))
    bindings.append(
        f"{name}_cells = "
        + _profiled_memory_register(
            f"(repeat {zero} :: Vec {memory.depth} ({element_type}))",
            f"{name}_cells_next",
            memory.contents_reset,
        ),
    )
    if merged_binding is not None:
        bindings.append(merged_binding)
    bindings.extend((
        read_value,
        f"{name}_read_data = "
        + _profiled_memory_register(
            zero,
            f"{name}_read_data_next",
            memory.read_data_reset,
        ),
        f"{name}_read_data_next = (\\fire value old -> if fire == high then value else old) "
        f"<$> {read_fire_name} <*> {name}_read_value <*> {name}_read_data",
        f"{name}_cells_next = (\\cells writeFire writeAddress writeData -> "
        f"if writeFire == high then replace (bitCoerce writeAddress :: Index {memory.depth}) "
        f"writeData cells else cells) <$> {name}_cells <*> {write_fire_name} "
        f"<*> {name}_write_address <*> {effective_write_data}",
    ))
    return tuple(bindings)


def _emit_rom_bindings(rom: object, render=None) -> tuple[str, ...]:
    """Lower one typed ROM through Clash's single synchronous romFile stage."""

    if render is None:
        render = lambda value: _emit_signal_expression(value, {})

    name = rom.name
    element_type = _emit_type(rom.element_type)
    width = rom.element_type.width
    zero = _zero_value(rom.element_type)
    read_address = render(rom.read_address)
    companion = companion_for_rom(rom)
    if rom.depth == 1:
        # ``romFilePow2 @1`` denotes two physical words.  A depth-one ROM is
        # legal ZLang storage and must retain its exact shape, so use the
        # length-indexed primitive even though its only typed address is zero.
        read_raw = (
            f"romFile (SNat @1) "
            f'"{companion.logical_path}" {name}_read_address'
        )
    elif rom.depth & (rom.depth - 1) == 0:
        address_bits = max(1, (rom.depth - 1).bit_length())
        read_raw = (
            f'romFilePow2 @{address_bits} @{width} "{companion.logical_path}" '
            f"{name}_read_address"
        )
    else:
        read_raw = (
            f"romFile (SNat @{rom.depth}) "
            f'"{companion.logical_path}" {name}_read_address'
        )
    return (
        f"{name}_read_address = {read_address}",
        f"{name}_read_raw = {read_raw}",
        f"{name}_read_value = "
        f"((unpack :: BitVector {width} -> {element_type}) <$> {name}_read_raw)",
        f"{name}_read_data = mux zlang_rom_reset_hold (pure {zero}) {name}_read_value",
    )


def _emit_connection_module(
    module: Module,
    *,
    formal_observations: tuple[tuple[Port, str, str], ...] = (),
) -> str:
    """Emit clocked buffers and explicit protocol adapters."""

    private_names = _clash_module_names(module)
    if not module.is_sequential or module.clock is None or module.reset is None:
        raise ClashEmissionError(
            "buffered and adapted connections require a module clock and reset"
        )
    extensions, imports, declarations = _emit_prelude(module)
    if "{-# LANGUAGE DeriveAnyClass #-}" not in extensions:
        extensions = extensions.replace(
            "{-# LANGUAGE NoImplicitPrelude #-}",
            "{-# LANGUAGE DeriveAnyClass #-}\n"
            "{-# LANGUAGE DeriveGeneric #-}\n"
            "{-# LANGUAGE NoImplicitPrelude #-}",
        )
    extensions = extensions.replace(
        "{-# LANGUAGE NoImplicitPrelude #-}",
        "{-# LANGUAGE TemplateHaskell #-}\n"
        "{-# LANGUAGE NoImplicitPrelude #-}",
    )
    if "import GHC.Generics (Generic)" not in imports:
        imports += "import GHC.Generics (Generic)\n"
    if any(
        port.protocol is InterfaceProtocol.READY_VALID for port in module.ports
    ):
        declarations = _ready_valid_declarations() + declarations
    if any(port.protocol is InterfaceProtocol.CREDIT for port in module.ports):
        declarations = _credit_declarations() + declarations

    domain = "ZLangSystem"
    input_types: list[str] = []
    input_names: list[str] = []
    input_annotations: list[str] = [
        f'PortName "{module.clock}"',
        f'PortName "{module.reset}"',
    ]
    output_types: list[str] = []
    output_values: list[str] = []
    output_annotations: list[str] = []
    bindings: list[str] = ["reset_active = unsafeToActiveHigh hasReset"]
    delay_nodes: dict[int, expr.Delay | expr.Pipeline] = {}
    for assignment in (*module.assignments, *module.next_assignments):
        _collect_delays(assignment.expression, delay_nodes)
    for local in module.locals:
        _collect_delays(local.expression, delay_nodes)
    for rule in module.rules:
        _collect_delays(rule.guard, delay_nodes)
        for action in rule.actions:
            _collect_delays(action.expression, delay_nodes)
            if action.activation is not None:
                _collect_delays(action.activation, delay_nodes)
    delay_names = {
        _leaf_key(value): (
            private_names.stage(_stage_prefix(value), value.instance, expr.sequential_stage_count(value))
        )
        for value in delay_nodes.values()
    }
    render_state = lambda value: _emit_signal_expression(value, delay_names)

    for port in module.ports:
        type_ = _emit_type(port.type)
        if port.protocol is InterfaceProtocol.WIRE:
            if port.direction is PortDirection.INPUT:
                input_types.append(f"Signal {domain} ({type_})")
                input_names.append(_clash_name(port.name))
                input_annotations.append(
                    _top_value_port_annotation(port.name, port.type)
                )
            else:
                output_types.append(f"Signal {domain} ({type_})")
                output_values.append(_clash_name(port.name))
                output_annotations.append(
                    _top_value_port_annotation(port.name, port.type)
                )
            continue
        if port.protocol is InterfaceProtocol.READY_VALID:
            if port.direction is PortDirection.INPUT:
                input_types.append(
                    f"Signal {domain} (ZLangReadyValidForward ({type_}))"
                )
                input_names.append(port.name)
                input_annotations.append(
                    _top_forward_port_annotation(port.name, port.type, "valid")
                )
                bindings.extend(
                    (
                        f"{port.name}_payload = zlangRvPayload <$> {port.name}",
                        f"{port.name}_valid = zlangRvValid <$> {port.name}",
                    )
                )
                output_types.append(f"Signal {domain} ZLangReadyValidBackward")
                output_values.append(
                    f"ZLangReadyValidBackward <$> {port.name}_ready"
                )
                output_annotations.append(f'PortName "{port.name}_ready"')
            else:
                backward = f"{port.name}_backward"
                input_types.append(f"Signal {domain} ZLangReadyValidBackward")
                input_names.append(backward)
                input_annotations.append(f'PortName "{port.name}_ready"')
                bindings.append(
                    f"{port.name}_ready = zlangRvReady <$> {backward}"
                )
                output_types.append(
                    f"Signal {domain} (ZLangReadyValidForward ({type_}))"
                )
                output_values.append(
                    f"ZLangReadyValidForward <$> {port.name}_payload "
                    f"<*> {port.name}_valid"
                )
                output_annotations.append(
                    _top_forward_port_annotation(port.name, port.type, "valid")
                )
            continue

        if port.direction is PortDirection.INPUT:
            input_types.append(f"Signal {domain} (ZLangCreditForward ({type_}))")
            input_names.append(port.name)
            input_annotations.append(
                _top_forward_port_annotation(port.name, port.type, "send")
            )
            bindings.extend(
                (
                    f"{port.name}_payload = zlangCreditPayload <$> {port.name}",
                    f"{port.name}_send = zlangCreditSend <$> {port.name}",
                )
            )
            output_types.append(f"Signal {domain} ZLangCreditReturn")
            output_values.append(f"ZLangCreditReturn <$> {port.name}_return")
            output_annotations.append(f'PortName "{port.name}_return"')
        else:
            return_input = f"{port.name}_return_input"
            input_types.append(f"Signal {domain} ZLangCreditReturn")
            input_names.append(return_input)
            input_annotations.append(f'PortName "{port.name}_return"')
            bindings.append(
                f"{port.name}_return = zlangCreditReturnPulse <$> {return_input}"
            )
            output_types.append(
                f"Signal {domain} (ZLangCreditForward ({type_}))"
            )
            output_values.append(
                f"ZLangCreditForward <$> {port.name}_payload "
                f"<*> {port.name}_send"
            )
            output_annotations.append(
                _top_forward_port_annotation(port.name, port.type, "send")
            )

    managed = set()
    for connection in module.connections:
        managed.update(_connection_assignment_keys(connection))
        bindings.extend(_emit_connection_bindings(connection))

    for port in module.ports:
        if port.protocol is InterfaceProtocol.READY_VALID:
            bindings.append(
                f"{port.name}_transfer = (\\valid ready -> valid .&. ready) "
                f"<$> {port.name}_valid <*> {port.name}_ready"
            )
        elif port.protocol is InterfaceProtocol.CREDIT:
            bindings.append(f"{port.name}_transfer = {port.name}_send")

    for local in module.locals:
        if local.compile_time:
            continue
        bindings.append(
            f"{_clash_name(local.name)} = {render_state(local.expression)}"
        )

    transition = module.resolved_transition
    if module.rules and transition is None:
        raise ClashEmissionError(
            f"connection module '{module.name}' lacks resolved transition IR"
        )
    unified_state = bool(
        transition is not None
        and (
            transition.resources
            or transition.action_groups
            or module.registers
            or module.next_assignments
            or module.rules
        )
    )
    scheduled_wire_outputs = {
        resource.name
        for resource in (transition.resources if transition is not None else ())
        if resource.kind is StateResourceKind.OUTPUT
    }
    if unified_state:
        bindings.extend(_emit_unified_schedule_bindings(module, render_state))
        bindings.extend(_emit_unified_register_bindings(module, render_state))
        bindings.extend(_emit_unified_output_bindings(
            module, render_state, include=scheduled_wire_outputs,
        ))
    for value in delay_nodes.values():
        previous = render_state(value.expression)
        for stage in range(1, expr.sequential_stage_count(value) + 1):
            name = private_names.stage(_stage_prefix(value), value.instance, stage)
            bindings.append(
                f"{name} = register {_zero_value(value.type)} ({previous})"
            )
            previous = name

    for assignment in module.assignments:
        key = (assignment.target.name, assignment.signal)
        if key in managed or assignment.target.name in scheduled_wire_outputs:
            continue
        bindings.append(
            f"{_assignment_name(assignment)} = "
            f"{render_state(assignment.expression)}"
        )

    for port, signal, token in formal_observations:
        if (
            port not in module.ports
            or port.protocol is not InterfaceProtocol.CREDIT
            or port.direction is not PortDirection.INPUT
        ):
            raise ClashEmissionError(
                "connection formal observation does not name a typed credit receiver"
            )
        if signal in {CreditSignal.SEND.value, CreditSignal.RETURN.value}:
            source_signal = f"{port.name}_{signal}"
            observation_type = "Bit"
        elif signal == "occupancy":
            matches = tuple(
                connection for connection in module.connections
                if connection.source.name == port.name
                and connection.source.protocol is InterfaceProtocol.CREDIT
                and connection.adapter is ConnectionAdapter.CREDIT_TO_READY_VALID
            )
            if len(matches) != 1:
                raise ClashEmissionError(
                    f"credit receiver '{port.name}' has no unique typed "
                    "credit-to-ready/valid occupancy owner"
                )
            connection = matches[0]
            source_signal = (
                f"{connection.source.name}_{connection.destination.name}_buffer_count"
            )
            observation_type = f"Unsigned {max(1, connection.buffer_depth.bit_length())}"
        else:
            raise ClashEmissionError(
                f"unsupported credit receiver formal observation '{signal}'"
            )
        output_types.append(f"Signal {domain} ({observation_type})")
        output_values.append(source_signal)
        output_annotations.append(f'PortName "{token}"')

    output_type = _emit_product(output_types)
    result = _emit_product(output_values)
    circuit_signature = " -> ".join((*input_types, output_type))
    top_signature = " -> ".join(
        (
            f"Clock {domain}",
            f"Reset {domain}",
            *input_types,
            output_type,
        )
    )
    circuit_arguments = " ".join(input_names)
    circuit_lhs = f"circuit {circuit_arguments}" if circuit_arguments else "circuit"
    top_arguments = " ".join((module.clock, module.reset, *input_names))
    application = f" {circuit_arguments}" if circuit_arguments else ""
    where_block = "\n".join(f"  {binding}" for binding in bindings)
    input_ports = ", ".join(input_annotations)
    output_port = (
        output_annotations[0]
        if len(output_annotations) == 1
        else f'PortProduct "" [{", ".join(output_annotations)}]'
    )

    top_name = f"{module.name}_formal" if formal_observations else module.name
    return f'''{extensions}
module {module.name} where

{imports}
{declarations}{_domain_declaration(module, domain)}

circuit :: HiddenClockResetEnable {domain} => {circuit_signature}
{circuit_lhs} = {result}
 where
{where_block}

topEntity :: {top_signature}
topEntity {top_arguments} = exposeClockResetEnable circuit {module.clock} {_top_reset_expression(module)} enableGen{application}

{{-# ANN topEntity
  (Synthesize
    {{ t_name = "{top_name}"
    , t_inputs = [{input_ports}]
    , t_output = {output_port}
    }}) #-}}
'''


def _emit_protocol_child_function(
    child: Module,
    component_name: str | None = None,
    recursive_catalog: _RecursiveProtocolCatalog | None = None,
    component_owner: _ProtocolComponentOwner | None = None,
    representative_path: tuple[str, ...] = (),
) -> str:
    """Emit the small reusable ready/valid child subset.

    The function deliberately uses the same forward/backward records as the
    standalone connection emitter.  A child returns its input-side backward
    signals followed by its output values; this keeps the physical handshake
    explicit when the parent connects children.
    """
    function_name = component_name or _protocol_child_name(child)
    if child.elastic_pipeline_regions:
        return _emit_elastic_pipeline_component(child, function_name)
    nested_csr = "\n".join(
        _emit_csr_child_function(
            item, _protocol_instance_child_name(child, instance.name, item)
        )
        for instance, item in zip(child.instances, child.children, strict=False)
        if item.csr_blocks
    )
    prefix = nested_csr + ("\n" if nested_csr else "")
    if child.csr_blocks:
        return _emit_csr_child_function(child, function_name)
    if child.hierarchical_connections:
        if (
            recursive_catalog is None
            or component_owner is None
            or not representative_path
        ):
            raise ClashEmissionError(
                f"nested ready/valid hierarchy child '{child.name}' requires "
                "the recursive closed component ABI"
            )
        return _emit_recursive_protocol_child_function(
            child,
            function_name,
            recursive_catalog,
            component_owner,
            representative_path,
        )
    rv_inputs = [p for p in child.inputs if p.protocol is InterfaceProtocol.READY_VALID]
    rv_outputs = [p for p in child.outputs if p.protocol is InterfaceProtocol.READY_VALID]
    wire_inputs = [p for p in child.inputs if p.protocol is InterfaceProtocol.WIRE]
    wire_outputs = [p for p in child.outputs if p.protocol is InterfaceProtocol.WIRE]
    if child.connections:
        # Reuse the already-tested FIFO/adapter implementation and retain its
        # stateful circuit body.  Only the module wrapper is removed; no RTL
        # name inference or placeholder replacement is involved.
        source = _emit_connection_module(child)
        start = source.index("circuit ::")
        end = source.index("\ntopEntity ::", start)
        body = source[start:end]
        return body.replace("circuit", function_name, 2)
    # A scheduled FIFO may coexist with registers, rules, and ready/valid
    # ports.  Reuse the complete storage transition emitter for this child
    # rather than selecting the legacy FIFO-only emitter or reconstructing a
    # partial protocol surrogate.  The shared typed circuit helper is used
    # directly; only its top-level wrapper is omitted because the parent owns
    # the physical hierarchy and clock/reset.
    if child.fifos and child.resolved_transition is not None:
        raw_name = f"{function_name}_raw"
        emission = _emit_storage_circuit(child, raw_name)
        nested_scalar_declarations = _emit_scalar_child_declarations(
            child, include_csr=False,
        )
        wire_inputs = [
            port for port in child.inputs
            if port.protocol is InterfaceProtocol.WIRE
        ]
        rv_inputs = [
            port for port in child.inputs
            if port.protocol is InterfaceProtocol.READY_VALID
        ]
        rv_outputs = [
            port for port in child.outputs
            if port.protocol is InterfaceProtocol.READY_VALID
        ]
        wire_outputs = [
            port for port in child.outputs
            if port.protocol is InterfaceProtocol.WIRE
        ]
        desired_args = [
            *(f"{_clash_name(port.name)}" for port in wire_inputs),
            *(f"{_clash_name(port.name)}" for port in rv_inputs),
            *(f"{_clash_name(port.name)}_backward" for port in rv_outputs),
        ]
        desired_types = [
            *(f"Signal ZLangSystem ({_emit_type(port.type)})" for port in wire_inputs),
            *(
                f"Signal ZLangSystem (ZLangReadyValidForward ({_emit_type(port.type)}))"
                for port in rv_inputs
            ),
            *("Signal ZLangSystem ZLangReadyValidBackward" for _ in rv_outputs),
        ]
        result_types = [
            *("Signal ZLangSystem ZLangReadyValidBackward" for _ in rv_inputs),
            *(
                f"Signal ZLangSystem (ZLangReadyValidForward ({_emit_type(port.type)}))"
                for port in rv_outputs
            ),
            *(f"Signal ZLangSystem ({_emit_type(port.type)})" for port in wire_outputs),
        ]
        # The helper retains the physical storage emitter's source-port order
        # for the raw function.  Adapt it to the stable hierarchy ABI above
        # without touching generated text.
        # The raw storage circuit follows physical source-port order.  Its
        # local binders deliberately retain storage-emitter spellings, while
        # the public hierarchy wrapper mangles Clash reserved identifiers.
        # Apply the wrapper arguments in raw port order; never refer to the
        # raw circuit's private binder names from the wrapper scope.
        raw_application_args = []
        for port in child.ports:
            if port.protocol is InterfaceProtocol.WIRE:
                if port.direction is PortDirection.INPUT:
                    raw_application_args.append(_clash_name(port.name))
                continue
            if port.direction is PortDirection.INPUT:
                raw_application_args.append(_clash_name(port.name))
            else:
                raw_application_args.append(
                    f"{_clash_name(port.name)}_backward"
                )
        wrapper = (
            f"{function_name} :: HiddenClockResetEnable ZLangSystem => "
            f"{' -> '.join((*desired_types, _emit_product(result_types)))}\n"
            f"{function_name} {' '.join(desired_args)} = "
            f"{raw_name} {' '.join(raw_application_args)}\n"
        )
        nested_prefix = (
            nested_scalar_declarations + "\n"
            if nested_scalar_declarations else ""
        )
        return prefix + nested_prefix + emission.circuit + "\n" + wrapper
    if _uses_bundled_component_abi(child):
        return _emit_bundled_protocol_child_function(child, function_name)
    if child.registers or child.rules or child.next_assignments or wire_inputs:
        return prefix + _emit_mixed_protocol_child_function(child, function_name)
    if not child.is_sequential or child.clock is None or child.reset is None:
        raise ClashEmissionError(
            f"protocol child '{child.name}' requires an explicit clock and reset"
        )
    if child.registers or child.rules or child.next_assignments:
        raise ClashEmissionError(
            f"stateful protocol child '{child.name}' requires a dedicated protocol state emitter"
        )
    args: list[str] = [p.name for p in rv_inputs]
    args.extend(f"{p.name}_backward" for p in rv_outputs)
    arg_types = [
        f"Signal ZLangSystem (ZLangReadyValidForward ({_emit_type(p.type)}))"
        for p in rv_inputs
    ] + [
        "Signal ZLangSystem ZLangReadyValidBackward" for _ in rv_outputs
    ]
    result_types = [
        "Signal ZLangSystem ZLangReadyValidBackward" for _ in rv_inputs
    ] + [
        f"Signal ZLangSystem (ZLangReadyValidForward ({_emit_type(p.type)}))"
        for p in rv_outputs
    ] + [f"Signal ZLangSystem ({_emit_type(p.type)})" for p in wire_outputs]
    if not result_types:
        raise ClashEmissionError(f"protocol child '{child.name}' has no emitted ports")
    signature = " -> ".join((*arg_types, _emit_product(result_types)))
    arguments = " ".join(args)
    bindings: list[str] = []
    for port in rv_inputs:
        bindings.extend(
            (
                f"{port.name}_payload = zlangRvPayload <$> {port.name}",
                f"{port.name}_valid = zlangRvValid <$> {port.name}",
            )
        )
    for port in rv_outputs:
        bindings.append(f"{port.name}_ready = zlangRvReady <$> {port.name}_backward")
    for port in rv_inputs:
        ready = next(
            (a.expression for a in child.assignments
             if a.target.name == port.name and a.signal is ReadyValidSignal.READY),
            expr.Constant(1, BitType()),
        )
        bindings.append(f"{port.name}_ready = {_emit_signal_expression(ready, {})}")
        bindings.append(
            f"{port.name}_transfer = (\\valid ready -> valid .&. ready) "
            f"<$> {port.name}_valid <*> {port.name}_ready"
        )
    output_values: list[str] = []
    for port in rv_inputs:
        ready = next(
            (a.expression for a in child.assignments
             if a.target.name == port.name and a.signal is ReadyValidSignal.READY),
            expr.Constant(1, BitType()),
        )
        output_values.append(f"ZLangReadyValidBackward <$> ({_emit_signal_expression(ready, {})})")
    for port in rv_outputs:
        payload = next(
            (a.expression for a in child.assignments
             if a.target.name == port.name and a.signal is ReadyValidSignal.PAYLOAD),
            expr.Constant(0, port.type),
        )
        valid = next(
            (a.expression for a in child.assignments
             if a.target.name == port.name and a.signal is ReadyValidSignal.VALID),
            expr.Constant(0, BitType()),
        )
        output_values.append(
            "ZLangReadyValidForward <$> "
            f"({_emit_signal_expression(payload, {})}) <*> "
            f"({_emit_signal_expression(valid, {})})"
        )
    for port in wire_outputs:
        assignment = next(
            (a.expression for a in child.assignments if a.target.name == port.name),
            expr.Constant(0, port.type),
        )
        output_values.append(_emit_signal_expression(assignment, {}))
    result = _emit_product(output_values)
    where_block = "\n".join(f"  {item}" for item in bindings)
    return prefix + f"""{function_name} :: HiddenClockResetEnable ZLangSystem => {signature}
{function_name} {arguments} = {result}
 where
{where_block}
"""


def _emit_recursive_protocol_child_function(
    child: Module,
    function_name: str,
    catalog: _RecursiveProtocolCatalog,
    component_owner: _ProtocolComponentOwner,
    representative_path: tuple[str, ...],
) -> str:
    """Emit one hierarchy as a closed reusable ready/valid component.

    External forward records, backward records, and scalar signals are the
    function's complete ABI.  Every nested application is selected from the
    typed parent/instance specialization map; the helper never captures the
    root circuit's signals or reconstructs an instance from source spelling.
    """

    private_names = _clash_module_names(child)
    if not child.is_sequential or child.clock is None or child.reset is None:
        raise ClashEmissionError(
            f"recursive ready/valid child '{child.name}' requires clock/reset"
        )
    if (
        child.aggregate_protocol_endpoints
        or child.aggregate_protocol_connections
        or child.request_responses
        or child.request_response_connections
    ):
        raise ClashEmissionError(
            f"recursive ready/valid child '{child.name}' supports ordinary "
            "ready/valid and wire ports only"
        )
    if child.connections:
        raise ClashEmissionError(
            f"recursive ready/valid child '{child.name}' cannot mix local "
            "protocol connections with hierarchical connections"
        )
    if child.memories or child.roms:
        raise ClashEmissionError(
            f"recursive ready/valid child '{child.name}' cannot own memory "
            "or ROM resources"
        )
    if any(not fifo.scheduled for fifo in child.fifos):
        raise ClashEmissionError(
            f"recursive ready/valid child '{child.name}' cannot mix legacy "
            "globally controlled FIFOs with scheduled state"
        )
    has_local_state = bool(
        child.registers
        or child.next_assignments
        or child.rules
        or child.fifos
    )
    if has_local_state and child.resolved_transition is None:
        raise ClashEmissionError(
            f"recursive ready/valid child '{child.name}' has local state "
            "without a resolved transition"
        )
    if any(
        port.protocol not in {
            InterfaceProtocol.WIRE,
            InterfaceProtocol.READY_VALID,
        }
        for port in child.ports
    ):
        raise ClashEmissionError(
            f"recursive ready/valid child '{child.name}' has an unsupported "
            "protocol port"
        )
    for connection in child.hierarchical_connections:
        if (
            connection.buffer_depth
            or connection.request_buffer_depth
            or connection.response_buffer_depth
            or connection.adapter is not None
            or connection.crossing is not None
        ):
            raise ClashEmissionError(
                f"recursive ready/valid child '{child.name}' supports direct "
                "unbuffered same-domain connections only"
            )
    if len(child.instances) != len(child.elaborated_instances):
        raise ClashEmissionError(
            f"recursive ready/valid child '{child.name}' has incomplete "
            "physical instance metadata"
        )

    rv_inputs = [
        port for port in child.inputs
        if port.protocol is InterfaceProtocol.READY_VALID
    ]
    rv_outputs = [
        port for port in child.outputs
        if port.protocol is InterfaceProtocol.READY_VALID
    ]
    wire_inputs = [
        port for port in child.inputs
        if port.protocol is InterfaceProtocol.WIRE
    ]
    wire_outputs = [
        port for port in child.outputs
        if port.protocol is InterfaceProtocol.WIRE
    ]

    def owned_signal(owner: str, port: str) -> str:
        return private_names.child_signal(owner, port)

    def forward(endpoint: ProtocolEndpoint) -> str:
        if endpoint.owner == child.name:
            return _clash_name(endpoint.name)
        return owned_signal(endpoint.owner, endpoint.name)

    def backward(endpoint: ProtocolEndpoint) -> str:
        if endpoint.owner == child.name:
            port = next(
                item for item in child.ports if item.name == endpoint.name
            )
            if port.direction is PortDirection.INPUT:
                return f"{_clash_name(port.name)}_backward_result"
            return f"{_clash_name(port.name)}_backward"
        return private_names.child_signal(endpoint.owner, endpoint.name, "ready")

    materialized_bindings, render_expression = _storage_materialization(child)
    bindings: list[str] = []
    for port in rv_inputs:
        name = _clash_name(port.name)
        bindings.extend(
            (
                f"{name}_payload = zlangRvPayload <$> {name}",
                f"{name}_valid = zlangRvValid <$> {name}",
                f"{name}_ready = zlangRvReady <$> {name}_backward_result",
                f"{name}_transfer = (\\valid ready -> valid .&. ready) "
                f"<$> {name}_valid <*> {name}_ready",
            )
        )
    for port in rv_outputs:
        name = _clash_name(port.name)
        bindings.append(
            f"{name}_ready = zlangRvReady <$> {name}_backward"
        )

    for instance in child.instances:
        try:
            nested_entry = catalog.child(representative_path, instance.name)
        except HierarchyError:
            raise ClashEmissionError(
                f"missing elaborated child module '{instance.module}'"
            ) from None
        nested = nested_entry.module
        incoming = [
            edge for edge in child.hierarchical_connections
            if edge.destination.owner == instance.name
        ]
        outgoing = [
            edge for edge in child.hierarchical_connections
            if edge.source.owner == instance.name
        ]
        arguments: list[str] = []
        ordered_inputs = [
            port for port in nested.inputs
            if port.protocol is InterfaceProtocol.WIRE
        ] + [
            port for port in nested.inputs
            if port.protocol is InterfaceProtocol.READY_VALID
        ]
        for port in ordered_inputs:
            edge = next(
                (
                    item for item in incoming
                    if item.destination.name == port.name
                ),
                None,
            )
            if port.protocol is InterfaceProtocol.READY_VALID:
                if edge is None:
                    raise ClashEmissionError(
                        f"unconnected protocol input "
                        f"'{instance.name}.{port.name}'"
                    )
                arguments.append(forward(edge.source))
                continue
            if edge is not None:
                arguments.append(forward(edge.source))
                continue
            binding = next(
                (
                    item.expression for item in child.instance_bindings
                    if item.instance == instance.name
                    and item.port == port.name
                ),
                None,
            )
            if binding is None:
                raise ClashEmissionError(
                    f"instance '{instance.name}' is missing scalar binding "
                    f"'{port.name}'"
                )
            arguments.append(render_expression(binding))
        for port in nested.outputs:
            if port.protocol is not InterfaceProtocol.READY_VALID:
                continue
            edge = next(
                (
                    item for item in outgoing
                    if item.source.name == port.name
                ),
                None,
            )
            if edge is None:
                raise ClashEmissionError(
                    f"unconnected protocol output "
                    f"'{instance.name}.{port.name}'"
                )
            arguments.append(backward(edge.destination))

        nested_function = catalog.get(
            catalog.application_key(
                representative_path,
                instance.name,
                component_owner,
            )
        )
        if nested_function is None:
            raise ClashEmissionError(
                f"missing specialization identity for physical instance "
                f"'{child.name}.{instance.name}'"
            )
        call = _closed_protocol_child_call(
            nested,
            nested_function,
            arguments,
            error=ClashEmissionError,
        )
        result_ports = [
            port for port in nested.inputs
            if port.protocol is InterfaceProtocol.READY_VALID
        ]
        result_ports += [
            port for port in nested.outputs
            if port.protocol is InterfaceProtocol.READY_VALID
        ]
        result_ports += [
            port for port in nested.outputs
            if port.protocol is InterfaceProtocol.WIRE
        ]
        if not result_ports:
            raise ClashEmissionError(
                f"protocol child '{nested.name}' has no emitted ports"
            )
        result_name = private_names.instance_helper(instance.name, "result")
        bindings.append(f"{result_name} = {call}")

        def projection(position: int) -> str:
            return _closed_protocol_child_projection(
                nested,
                result_ports[position],
                result_name,
                position,
                len(result_ports),
                nested_function,
            )

        for position, port in enumerate(result_ports):
            endpoint = ProtocolEndpoint(
                instance.name,
                port.name,
                port.direction,
                port.protocol,
                port.type,
            )
            if (
                port.protocol is InterfaceProtocol.READY_VALID
                and port.direction is PortDirection.INPUT
            ):
                bindings.append(
                    f"{backward(endpoint)} = {projection(position)}"
                )
            elif port.protocol is InterfaceProtocol.READY_VALID:
                bindings.append(
                    f"{forward(endpoint)} = {projection(position)}"
                )
            else:
                bindings.append(
                    f"{owned_signal(instance.name, port.name)} = "
                    f"{projection(position)}"
                )

    connected_inputs: set[str] = set()
    connected_outputs: set[str] = set()
    for connection in child.hierarchical_connections:
        bindings.append(
            f"{forward(connection.destination)} = "
            f"{forward(connection.source)}"
        )
        if connection.source.protocol is InterfaceProtocol.READY_VALID:
            bindings.append(
                f"{backward(connection.source)} = "
                f"{backward(connection.destination)}"
            )
        if connection.source.owner == child.name:
            connected_inputs.add(connection.source.name)
        if connection.destination.owner == child.name:
            connected_outputs.add(connection.destination.name)

    # A recursive wrapper is still one ordinary ZLang state owner.  Reuse the
    # already-resolved transition and the same scheduler/storage helpers used
    # by standalone storage circuits; nested components merely contribute
    # immutable pre-edge Signal leaves (including scalar child outputs).  This
    # deliberately adds neither a hierarchy scheduler nor cross-module atomic
    # actions.
    bindings.extend(materialized_bindings)
    materialized_names = {
        item.split(" = ", 1)[0] for item in materialized_bindings
        if " = " in item
    }
    for local in child.locals:
        local_name = _clash_name(local.name)
        if local.compile_time or local_name in materialized_names:
            continue
        bindings.append(
            f"{local_name} = {render_expression(local.expression)}"
        )
    if has_local_state:
        transition = child.resolved_transition
        assert transition is not None
        bindings.append("reset_active = unsafeToActiveHigh hasReset")
        bindings.extend(
            _emit_unified_schedule_bindings(child, render_expression)
        )
        for fifo in child.fifos:
            bindings.extend(
                _emit_scheduled_fifo_bindings(child, fifo, render_expression)
            )

        bindings.extend(
            _emit_unified_register_bindings(child, render_expression)
        )

    scheduled_wire_outputs = {
        resource.name
        for resource in (
            child.resolved_transition.resources
            if child.resolved_transition is not None else ()
        )
        if resource.kind is StateResourceKind.OUTPUT
    }
    if scheduled_wire_outputs:
        bindings.extend(
            _emit_unified_output_bindings(
                child,
                render_expression,
                include=scheduled_wire_outputs - connected_outputs,
            )
        )

    for port in rv_inputs:
        if port.name not in connected_inputs:
            raise ClashEmissionError(
                f"recursive ready/valid input '{child.name}.{port.name}' has "
                "no hierarchical consumer"
            )
    for port in rv_outputs:
        if port.name not in connected_outputs:
            raise ClashEmissionError(
                f"recursive ready/valid output '{child.name}.{port.name}' has "
                "no hierarchical producer"
            )
    for port in wire_outputs:
        if port.name in connected_outputs:
            continue
        if port.name in scheduled_wire_outputs:
            continue
        assignment = next(
            (
                item for item in child.assignments
                if item.target.name == port.name and item.signal is None
            ),
            None,
        )
        if assignment is None:
            raise ClashEmissionError(
                f"recursive scalar output '{child.name}.{port.name}' is not "
                "driven"
            )
        bindings.append(
            f"{_clash_name(port.name)} = "
            f"{render_expression(assignment.expression)}"
        )
    unsupported_assignments = [
        assignment for assignment in child.assignments
        if not any(
            assignment.target.name == port.name and assignment.signal is None
            for port in wire_outputs
        )
    ]
    if unsupported_assignments:
        raise ClashEmissionError(
            f"recursive ready/valid child '{child.name}' cannot mix local "
            "protocol assignments with hierarchical endpoint ownership"
        )

    argument_names = [
        *(_clash_name(port.name) for port in wire_inputs),
        *(_clash_name(port.name) for port in rv_inputs),
        *(
            f"{_clash_name(port.name)}_backward"
            for port in rv_outputs
        ),
    ]
    argument_types = [
        *(f"Signal ZLangSystem ({_emit_type(port.type)})" for port in wire_inputs),
        *(
            f"Signal ZLangSystem (ZLangReadyValidForward "
            f"({_emit_type(port.type)}))"
            for port in rv_inputs
        ),
        *(
            "Signal ZLangSystem ZLangReadyValidBackward"
            for _ in rv_outputs
        ),
    ]
    result_values = [
        *(f"{_clash_name(port.name)}_backward_result" for port in rv_inputs),
        *(_clash_name(port.name) for port in rv_outputs),
        *(_clash_name(port.name) for port in wire_outputs),
    ]
    result_types = [
        *(
            "Signal ZLangSystem ZLangReadyValidBackward"
            for _ in rv_inputs
        ),
        *(
            f"Signal ZLangSystem (ZLangReadyValidForward "
            f"({_emit_type(port.type)}))"
            for port in rv_outputs
        ),
        *(
            f"Signal ZLangSystem ({_emit_type(port.type)})"
            for port in wire_outputs
        ),
    ]
    if not result_types:
        raise ClashEmissionError(
            f"recursive protocol child '{child.name}' has no emitted ports"
        )
    signature = " -> ".join(
        (*argument_types, _emit_product(result_types))
    )
    arguments = " ".join(argument_names)
    result = _emit_product(result_values)
    where_block = "\n".join(f"  {binding}" for binding in bindings)
    return f"""{function_name} :: HiddenClockResetEnable ZLangSystem => {signature}
{function_name} {arguments} = {result}
 where
{where_block}
"""


def _emit_csr_child_function(
    child: Module, component_name: str | None = None,
) -> str:
    """Emit the canonical CSR access ABI as a closed hierarchical component."""
    private_names = _clash_module_names(child)
    function_name = component_name or _protocol_child_name(child)
    if child.csr_access is None or not child.csr_blocks:
        raise ClashEmissionError("CSR child requires typed CSR access metadata")
    inputs = list(child.inputs)
    outputs = list(child.outputs)
    args = " ".join(port.name for port in inputs)
    signature = " -> ".join((
        *(f"Signal ZLangSystem ({_emit_type(port.type)})" for port in inputs),
        _emit_product(tuple(
            f"Signal ZLangSystem ({_emit_type(port.type)})" for port in outputs
        )),
    ))
    bindings: list[str] = ["wdata_bits = pack <$> wdata"]
    words: list[tuple[int, str]] = []
    addresses: list[int] = []
    state_outputs: dict[str, str] = {}
    for block in child.csr_blocks:
        state_by_field = {item.csr_field_id: item for item in block.state_bindings}
        for register in block.registers:
            address = block.base_address + register.offset
            addresses.append(address)
            prefix = _csr_identifier(block.name, register.name)
            hit = f"{prefix}_write_hit"
            bindings.append(
                f"{hit} = (\\address writeRequest -> writeRequest == high && "
                f"address == ({address} :: Unsigned 32)) <$> addr <*> write"
            )
            readable: list[tuple[ir_csr.CsrField, str]] = []
            for field in register.fields:
                if field.access is ir_csr.CsrAccess.RESERVED:
                    continue
                name = _csr_identifier(block.name, register.name, field.name)
                if field.access is ir_csr.CsrAccess.READ_ONLY:
                    bindings.append(
                        f"{name} = pure ({field.reset} :: BitVector {field.width})"
                    )
                else:
                    value = f"{name}_write_value"
                    bindings.append(
                        f"{value} = slice d{field.msb} d{field.lsb} <$> wdata_bits"
                    )
                    bindings.append(
                        f"{name} = register ({field.reset} :: BitVector {field.width}) {name}_next"
                    )
                    if field.access is ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR:
                        update = "if writeHit then old .&. complement incoming else old"
                    elif field.access is ir_csr.CsrAccess.PULSE:
                        update = "if writeHit then incoming else 0"
                    else:
                        update = "if writeHit then incoming else old"
                    bindings.append(
                        f"{name}_next = (\\old writeHit incoming -> {update}) "
                        f"<$> {name} <*> {hit} <*> {value}"
                    )
                if field.access in {
                    ir_csr.CsrAccess.READ_WRITE,
                    ir_csr.CsrAccess.READ_ONLY,
                    ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR,
                }:
                    readable.append((field, name))
                state = state_by_field.get(field.identity)
                if state is not None:
                    state_outputs[ir_csr.csr_state_port_name(state)] = (
                        f"unpack <$> {name}" if not isinstance(field.type, BitsType)
                        else name
                    )
                    state_outputs[ir_csr.csr_write_hit_port_name(state)] = (
                        f"boolToBit <$> {hit}"
                    )
                    state_outputs[ir_csr.csr_write_value_port_name(state)] = (
                        f"unpack <$> {value}"
                        if not isinstance(field.type, BitsType) else value
                    )
            word = f"{prefix}_read_word"
            if readable:
                params = " ".join(f"field{i}" for i in range(len(readable)))
                parts = [
                    f"shiftL (resize field{i} :: BitVector 32) {field.lsb}"
                    for i, (field, _) in enumerate(readable)
                ]
                applications = "".join(
                    (" <$> " if i == 0 else " <*> ") + signal
                    for i, (_, signal) in enumerate(readable)
                )
                bindings.append(
                    f"{word} = (\\{params} -> {' .|. '.join(parts)}){applications}"
                )
            else:
                bindings.append(f"{word} = pure (0 :: BitVector 32)")
            words.append((address, word))
    params = " ".join(f"word{i}" for i in range(len(words)))
    cases = "; ".join(f"{address} -> word{i}" for i, (address, _) in enumerate(words))
    applications = "".join(f" <*> {word}" for _, word in words)
    bindings.append(
        f"rdata_bits = (\\address readRequest {params} -> if readRequest == low "
        f"then 0 else case address of {{ {cases}; _ -> 0 }}) <$> addr <*> read{applications}"
    )
    bindings.append("rdata = unpack <$> rdata_bits")
    ready_cases = "; ".join(f"{address} -> high" for address in addresses)
    bindings.append(
        "ready = (\\address readRequest writeRequest -> if readRequest == high "
        "|| writeRequest == high then case address of { "
        f"{ready_cases}; _ -> low }} else low) <$> addr <*> read <*> write"
    )
    delay_nodes: dict[int, expr.Delay | expr.Pipeline] = {}
    for assignment in (*child.assignments, *child.next_assignments):
        _collect_delays(assignment.expression, delay_nodes)
    for local in child.locals:
        _collect_delays(local.expression, delay_nodes)
    for rule in child.rules:
        _collect_delays(rule.guard, delay_nodes)
        for action in rule.actions:
            _collect_delays(action.expression, delay_nodes)
            if action.activation is not None:
                _collect_delays(action.activation, delay_nodes)
    delay_names = {
        _leaf_key(value): (
            private_names.stage(_stage_prefix(value), value.instance, expr.sequential_stage_count(value))
        )
        for value in delay_nodes.values()
    }
    render_state = lambda value: _emit_signal_expression(value, delay_names)
    for local in child.locals:
        if local.compile_time:
            continue
        bindings.append(
            f"{_clash_name(local.name)} = {render_state(local.expression)}"
        )
    transition = child.resolved_transition
    if child.rules and transition is None:
        raise ClashEmissionError(
            f"hierarchical CSR child '{child.name}' lacks resolved transition IR"
        )
    unified_state = bool(
        transition is not None
        and (
            transition.resources
            or transition.action_groups
            or child.registers
            or child.next_assignments
            or child.rules
        )
    )
    scheduled_wire_outputs = {
        resource.name
        for resource in (transition.resources if transition is not None else ())
        if resource.kind is StateResourceKind.OUTPUT
    }
    if unified_state:
        bindings.append("reset_active = unsafeToActiveHigh hasReset")
        bindings.extend(_emit_unified_schedule_bindings(child, render_state))
        bindings.extend(_emit_unified_register_bindings(child, render_state))
        bindings.extend(_emit_unified_output_bindings(
            child, render_state, include=scheduled_wire_outputs,
        ))
    direct_outputs = _scalar_output_assignments(child)
    for output in child.outputs:
        if (
            output.name in state_outputs
            or output.name in scheduled_wire_outputs
            or output.name in {"rdata", "ready"}
        ):
            continue
        value = direct_outputs.get(output.name)
        if value is not None:
            bindings.append(
                f"{_clash_name(output.name)} = {render_state(value)}"
            )
    for value in delay_nodes.values():
        previous = render_state(value.expression)
        for stage in range(1, expr.sequential_stage_count(value) + 1):
            name = private_names.stage(_stage_prefix(value), value.instance, stage)
            bindings.append(
                f"{name} = register {_zero_value(value.type)} ({previous})"
            )
            previous = name
    values = [
        state_outputs.get(port.name, port.name) for port in outputs
    ]
    return (
        f"{function_name} :: HiddenClockResetEnable ZLangSystem => {signature}\n"
        f"{function_name} {args} = {_emit_product(values)}\n where\n"
        + "\n".join(f"  {item}" for item in bindings) + "\n"
    )


def _emit_mixed_protocol_child_function(
    child: Module, component_name: str | None = None,
) -> str:
    """Emit one stateful function containing scalar and RV ports together."""
    private_names = _clash_module_names(child)
    function_name = component_name or _protocol_child_name(child)
    rv_inputs = [p for p in child.inputs if p.protocol is InterfaceProtocol.READY_VALID]
    rv_outputs = [p for p in child.outputs if p.protocol is InterfaceProtocol.READY_VALID]
    wire_inputs = [p for p in child.inputs if p.protocol is InterfaceProtocol.WIRE]
    wire_outputs = [p for p in child.outputs if p.protocol is InterfaceProtocol.WIRE]
    args = [f"mixed_{p.name}" for p in wire_inputs] + [f"mixed_{p.name}" for p in rv_inputs]
    args += [f"mixed_{p.name}_backward" for p in rv_outputs]
    arg_types = [f"Signal ZLangSystem ({_emit_type(p.type)})" for p in wire_inputs]
    arg_types += [f"Signal ZLangSystem (ZLangReadyValidForward ({_emit_type(p.type)}))" for p in rv_inputs]
    arg_types += ["Signal ZLangSystem ZLangReadyValidBackward" for _ in rv_outputs]
    result_types = ["Signal ZLangSystem ZLangReadyValidBackward" for _ in rv_inputs]
    result_types += [f"Signal ZLangSystem (ZLangReadyValidForward ({_emit_type(p.type)}))" for p in rv_outputs]
    result_types += [f"Signal ZLangSystem ({_emit_type(p.type)})" for p in wire_outputs]
    if not result_types:
        raise ClashEmissionError(f"protocol child '{child.name}' has no outputs")
    bindings: list[str] = []
    bindings.extend(
        f"{_clash_name(p.name)} = mixed_{p.name}"
        for p in (*wire_inputs, *rv_inputs)
    )
    bindings.extend(f"{p.name}_backward = mixed_{p.name}_backward" for p in rv_outputs)
    signal_names = {
        ("InputRef", p.name): f"mixed_{p.name}"
        for p in (*wire_inputs, *rv_inputs)
    }
    signal_names.update({
        ("ReadyValidRef", p.name, ReadyValidSignal.READY.value): f"mixed_{p.name}_backward"
        for p in rv_outputs
    })
    for port in rv_inputs:
        signal_names[("ReadyValidRef", port.name, ReadyValidSignal.PAYLOAD.value)] = (
            f"{port.name}_payload"
        )
        signal_names[("ReadyValidRef", port.name, ReadyValidSignal.VALID.value)] = (
            f"{port.name}_valid"
        )
    for port in rv_outputs:
        signal_names[("ReadyValidRef", port.name, ReadyValidSignal.READY.value)] = (
            f"{port.name}_ready"
        )
    for instance in child.instances:
        nested = next(
            (item for item in child.children if item.name == instance.module), None
        )
        if nested is None:
            raise ClashEmissionError(
                f"missing elaborated nested child module '{instance.module}'"
            )
        if not nested.csr_blocks:
            raise ClashEmissionError(
                "nested mixed hierarchy currently supports the canonical CSR bank"
            )
        by_port = {
            item.port: item.expression for item in child.instance_bindings
            if item.instance == instance.name
        }
        missing = [port.name for port in nested.inputs if port.name not in by_port]
        if missing:
            raise ClashEmissionError(
                f"instance '{instance.name}' is missing scalar binding '{missing[0]}'"
            )
        applications = [
            _emit_signal_expression(by_port[port.name], signal_names)
            for port in nested.inputs
        ]
        result = private_names.instance_helper(instance.name, "csr_result")
        bindings.append(
            f"{result} = {_protocol_instance_child_name(child, instance.name, nested)} "
            + " ".join(_clash_apply_arg(item) for item in applications)
        )
        count = len(nested.outputs)
        for index, port in enumerate(nested.outputs):
            if count == 1:
                projection = result
            elif count == 2:
                projection = f"{'fst' if index == 0 else 'snd'} {result}"
            else:
                pattern = ["_" for _ in range(count)]
                pattern[index] = f"nested{index}"
                projection = f"let ({','.join(pattern)}) = {result} in nested{index}"
            name = private_names.child_signal(instance.name, port.name)
            bindings.append(f"{name} = {projection}")
            signal_names[("InstanceOutputRef", instance.name, port.name)] = name
    # Locals are pure, ordered signal bindings.  Keep them inside the child
    # component and expose their names to every later expression; treating
    # them as free identifiers was the source of invalid Clash for stateful
    # mixed children.
    for local in child.locals:
        signal_names[("InputRef", local.name)] = local.name
        bindings.append(
            f"{local.name} = {_emit_signal_expression(local.expression, {**signal_names})}"
        )
    if child.rules:
        bindings.append("reset_active = unsafeToActiveHigh hasReset")
    for port in rv_inputs:
        bindings += [f"{port.name}_payload = zlangRvPayload <$> mixed_{port.name}",
                     f"{port.name}_valid = zlangRvValid <$> mixed_{port.name}"]
        ready_expr = next(
            (a.expression for a in child.assignments
             if a.target.name == port.name and a.signal is ReadyValidSignal.READY),
            expr.Constant(1, BitType()),
        )
        bindings.append(f"{port.name}_ready = {_emit_signal_expression(ready_expr, {**signal_names})}")
        bindings.append(
            f"{port.name}_transfer = (\\valid ready -> valid .&. ready) "
            f"<$> {port.name}_valid <*> {port.name}_ready"
        )
    for port in rv_outputs:
        bindings.append(f"{port.name}_ready = zlangRvReady <$> mixed_{port.name}_backward")
        payload_expr = next(
            (a.expression for a in child.assignments
             if a.target.name == port.name and a.signal is ReadyValidSignal.PAYLOAD),
            expr.Constant(0, port.type),
        )
        valid_expr = next(
            (a.expression for a in child.assignments
             if a.target.name == port.name and a.signal is ReadyValidSignal.VALID),
            expr.Constant(0, BitType()),
        )
        bindings.append(f"{port.name}_valid = {_emit_signal_expression(valid_expr, {**signal_names})}")
        bindings.append(
            f"{port.name}_transfer = (\\valid ready -> valid .&. ready) "
            f"<$> {port.name}_valid <*> {port.name}_ready"
        )
    for port in rv_inputs:
        signal_names[("ReadyValidRef", port.name, ReadyValidSignal.TRANSFER.value)] = (
            f"(({port.name}_valid) .&. ({port.name}_ready))"
        )
    for port in rv_outputs:
        signal_names[("ReadyValidRef", port.name, ReadyValidSignal.TRANSFER.value)] = (
            f"(({port.name}_valid) .&. ({port.name}_ready))"
        )
    delay_nodes: dict[int, expr.Delay | expr.Pipeline] = {}
    for assignment in (*child.assignments, *child.next_assignments):
        _collect_delays(assignment.expression, delay_nodes)
    for rule in child.rules:
        _collect_delays(rule.guard, delay_nodes)
        for action in rule.actions:
            _collect_delays(action.expression, delay_nodes)
            if action.activation is not None:
                _collect_delays(action.activation, delay_nodes)
    delay_names = {
        _leaf_key(d): private_names.stage(_stage_prefix(d), d.instance, expr.sequential_stage_count(d))
        for d in delay_nodes.values()
    }
    ordered_rules = _rule_schedule(child)
    transition_ir = child.resolved_transition
    if child.rules and transition_ir is None:
        raise ClashEmissionError(
            f"stateful protocol child '{child.name}' lacks resolved transition IR",
            code="ZL-BACKEND-CLASH-CONDITIONAL-ACTION",
        )
    # A ResolvedTransition is the authoritative scheduler even when all
    # effects are unconditionally active inside their rule.  The legacy
    # raw-guard path cannot preserve group-wide suppression through an OUTPUT
    # resource.
    unified_state = bool(
        transition_ir is not None
        and (
            transition_ir.resources
            or transition_ir.action_groups
            or child.registers
            or child.next_assignments
            or child.rules
        )
    )
    render_state = lambda value: _emit_signal_expression(
        value, {**signal_names, **delay_names}
    )
    if unified_state:
        bindings.extend(_emit_unified_schedule_bindings(child, render_state))
    else:
        for rule in ordered_rules:
            fire = private_names.rule(rule.name)
            raw = render_state(rule.guard)
            bindings.append(
                f"{fire} = (\\guard resetActive -> if resetActive then low else guard) "
                f"<$> {_clash_apply_arg(raw)} <*> reset_active"
            )
    next_by_register = {a.target.name: a.expression for a in child.next_assignments}
    if unified_state:
        bindings.extend(_emit_unified_register_bindings(
            child,
            render_state,
            parenthesize_next=not conditional_actions(transition_ir),
        ))
    else:
        for register in child.registers:
            bindings.append(f"{register.name} = register {_emit_expression(register.initial)} ({register.name}_next)")
            scheduled: expr.Expression = next_by_register.get(register.name, expr.RegisterRef(register.name, register.type))
            for rule in reversed(ordered_rules):
                for action in rule.actions:
                    if action.target.name == register.name:
                        scheduled = expr.Mux(expr.InputRef(private_names.rule(rule.name), BitType()), action.expression, scheduled, register.type)
            bindings.append(f"{register.name}_next = {render_state(scheduled)}")
    for delay in delay_nodes.values():
        previous = _emit_signal_expression(delay.expression, {**signal_names, **delay_names})
        for stage in range(1, expr.sequential_stage_count(delay) + 1):
            name = private_names.stage(_stage_prefix(delay), delay.instance, stage)
            bindings.append(f"{name} = register {_zero_value(delay.type)} ({previous})")
            previous = name
    scheduled_wire_outputs = {
        resource.name
        for resource in (
            transition_ir.resources if transition_ir is not None else ()
        )
        if resource.kind is StateResourceKind.OUTPUT
    }
    if unified_state and scheduled_wire_outputs:
        bindings.extend(
            _emit_unified_output_bindings(
                child,
                render_state,
                include=scheduled_wire_outputs,
            )
        )
    def assignment_for(name: str, signal: object = None) -> expr.Expression:
        for item in child.assignments:
            if item.target.name == name and item.signal is signal:
                return item.expression
        return expr.Constant(0, next(p.type for p in child.outputs if p.name == name))
    for port in rv_inputs:
        ready = next((a.expression for a in child.assignments if a.target.name == port.name and a.signal is ReadyValidSignal.READY), expr.Constant(1, BitType()))
        bindings.append(f"{port.name}_backward_value = ZLangReadyValidBackward <$> ({_emit_signal_expression(ready, {**signal_names, **delay_names})})")
    values: list[str] = []
    for port in rv_inputs:
        values.append(f"{port.name}_backward_value")
    for port in rv_outputs:
        payload = assignment_for(port.name, ReadyValidSignal.PAYLOAD)
        valid = assignment_for(port.name, ReadyValidSignal.VALID)
        values.append("ZLangReadyValidForward <$> (" + _emit_signal_expression(payload, {**signal_names, **delay_names}) + ") <*> (" + _emit_signal_expression(valid, {**signal_names, **delay_names}) + ")")
    for port in wire_outputs:
        if unified_state and port.name in scheduled_wire_outputs:
            values.append(_clash_name(port.name))
            continue
        scheduled = next((a.expression for a in child.assignments if a.target.name == port.name and a.signal is None), expr.Constant(0, port.type))
        values.append(render_state(scheduled))
    result = _emit_product(values)
    return f"""{function_name} :: HiddenClockResetEnable ZLangSystem => {' -> '.join((*arg_types, _emit_product(result_types)))}
{function_name} {' '.join(args)} = {result}
 where
""" + "\n".join(f"  {b}" for b in bindings) + "\n"


def _emit_bundled_protocol_child_function(
    child: Module, component_name: str | None = None,
) -> str:
    """Emit a closed mealy component with one bundled Signal input/output."""
    # Public routing selects the unified mixed-Signal ABI for these modules.
    # Retain a defensive capability gate for direct/internal callers so the
    # legacy pure transition can never erase an activation predicate.
    _reject_conditional_action_route(child, "bundled protocol child")
    function_name = component_name or _protocol_child_name(child)
    if child.clock is None or child.reset is None:
        raise ClashEmissionError("bundled stateful protocol children require clock/reset")
    if len(child.registers) != 1:
        raise ClashEmissionError(
            f"bundled protocol child '{child.name}' currently requires exactly one register"
        )
    if child.connections or child.children:
        raise ClashEmissionError("bundled mixed children cannot contain nested connections")
    rv_inputs = [p for p in child.inputs if p.protocol is InterfaceProtocol.READY_VALID]
    rv_outputs = [p for p in child.outputs if p.protocol is InterfaceProtocol.READY_VALID]
    wire_inputs = [p for p in child.inputs if p.protocol is InterfaceProtocol.WIRE]
    wire_outputs = [p for p in child.outputs if p.protocol is InterfaceProtocol.WIRE]
    input_type = _component_type_name(child, "Input", function_name)
    output_type = _component_type_name(child, "Output", function_name)
    input_fields: list[tuple[str, str]] = []
    for port in wire_inputs:
        input_fields.append((_component_accessor(child, port.name, component_name=function_name), _emit_type(port.type)))
    for port in rv_inputs:
        input_fields.append((_component_accessor(child, port.name, "Forward", function_name),
                             f"ZLangReadyValidForward ({_emit_type(port.type)})"))
    for port in rv_outputs:
        input_fields.append((_component_accessor(child, port.name, "Backward", function_name),
                             "ZLangReadyValidBackward"))
    output_fields: list[tuple[str, str]] = []
    for port in rv_inputs:
        output_fields.append((_component_accessor(child, port.name, "Backward", function_name),
                              "ZLangReadyValidBackward"))
    for port in rv_outputs:
        output_fields.append((_component_accessor(child, port.name, "Forward", function_name),
                              f"ZLangReadyValidForward ({_emit_type(port.type)})"))
    for port in wire_outputs:
        output_fields.append((_component_accessor(child, port.name, component_name=function_name), _emit_type(port.type)))
    if not input_fields or not output_fields:
        raise ClashEmissionError("bundled component ABI requires explicit inputs and outputs")
    def record(name: str, fields: list[tuple[str, str]]) -> str:
        rendered = "\n  , ".join(f"{field} :: {type_}" for field, type_ in fields)
        return f"data {name} = {name}\n  {{ {rendered}\n  }} deriving (Generic, NFDataX, Show, Eq)\n"
    component_input = "componentInput"
    register = child.registers[0]
    names: dict[tuple[object, ...], str] = {
        _leaf_key(expr.InputRef(p.name, p.type)): f"({_component_accessor(child, p.name, component_name=function_name)} {component_input})"
        for p in wire_inputs
    }
    for port in rv_inputs:
        forward = f"{_component_accessor(child, port.name, 'Forward', function_name)} {component_input}"
        for signal, accessor in (
            (ReadyValidSignal.PAYLOAD, "zlangRvPayload"),
            (ReadyValidSignal.VALID, "zlangRvValid"),
        ):
            names[("ReadyValidRef", port.name, signal.value)] = f"({accessor} ({forward}))"
    for port in rv_outputs:
        backward = f"{_component_accessor(child, port.name, 'Backward', function_name)} {component_input}"
        names[("ReadyValidRef", port.name, ReadyValidSignal.READY.value)] = f"(zlangRvReady ({backward}))"
    names[_leaf_key(expr.RegisterRef(register.name, register.type))] = register.name
    def assigned(port: object, signal: object = None) -> expr.Expression:
        return next(
            (a.expression for a in child.assignments
             if a.target.name == port.name and a.signal is signal),
            expr.Constant(0, BitType() if signal in (ReadyValidSignal.READY, ReadyValidSignal.VALID) else port.type),
        )
    for port in rv_inputs:
        forward = f"{_component_accessor(child, port.name, 'Forward', function_name)} {component_input}"
        ready = _emit_expression(assigned(port, ReadyValidSignal.READY), names)
        names[("ReadyValidRef", port.name, ReadyValidSignal.TRANSFER.value)] = (
            f"((zlangRvValid ({forward})) .&. ({ready}))"
        )
    for port in rv_outputs:
        backward = f"{_component_accessor(child, port.name, 'Backward', function_name)} {component_input}"
        valid = _emit_expression(assigned(port, ReadyValidSignal.VALID), names)
        names[("ReadyValidRef", port.name, ReadyValidSignal.TRANSFER.value)] = (
            f"(({valid}) .&. (zlangRvReady ({backward})))"
        )
    ordered_rules = _rule_schedule(child)
    next_by_register = {item.target.name: item.expression for item in child.next_assignments}
    scheduled: expr.Expression = next_by_register.get(
        register.name, expr.RegisterRef(register.name, register.type)
    )
    for rule in reversed(ordered_rules):
        action = next((a for a in rule.actions if a.target.name == register.name), None)
        if action is not None:
            scheduled = expr.Mux(rule.guard, action.expression, scheduled, register.type)
    next_value = _emit_expression(scheduled, names)
    output_values: list[str] = []
    for port in rv_inputs:
        output_values.append(
            f"ZLangReadyValidBackward ({_emit_expression(assigned(port, ReadyValidSignal.READY), names)})"
        )
    for port in rv_outputs:
        payload = _emit_expression(assigned(port, ReadyValidSignal.PAYLOAD), names)
        valid = _emit_expression(assigned(port, ReadyValidSignal.VALID), names)
        output_values.append(f"ZLangReadyValidForward ({payload}) ({valid})")
    for port in wire_outputs:
        output_values.append(_emit_expression(assigned(port), names))
    output_value = f"{output_type} {' '.join(f'({value})' for value in output_values)}"
    transition = f"{function_name}Transition"
    return (
        record(input_type, input_fields) + "\n" + record(output_type, output_fields) + "\n"
        f"{transition} :: {_emit_type(register.type)} -> {input_type} -> ({_emit_type(register.type)}, {output_type})\n"
        f"{transition} {register.name} {component_input} = ({next_value}, {output_value})\n\n"
        f"{function_name} :: HiddenClockResetEnable ZLangSystem => Signal ZLangSystem {input_type} -> Signal ZLangSystem {output_type}\n"
        f"{function_name} = mealy {transition} {_emit_expression(register.initial)}\n"
        f"{{-# NOINLINE {function_name} #-}}\n"
    )


def _request_response_child_formal_accessor(
    child: Module,
    local_semantic_id: str,
    component_name: str | None = None,
) -> str:
    """Name one typed formal-only field on the closed RR child product."""

    token = hashlib.sha256(local_semantic_id.encode()).hexdigest()[:16]
    return _component_accessor(
        child, f"formal{token}", component_name=component_name,
    )


def _request_response_component_output_name(instance_name: str, module: Module | None = None) -> str:
    """Return the shared physical binder for one RR child output product."""

    if module is not None:
        return _clash_module_names(module).instance_helper(instance_name, "component_output")
    return f"{_clash_instance_name(instance_name)}_component_output"


def _emit_conditional_request_response_child_function(
    child: Module,
    function_name: str,
    *,
    formal_observation_ids: tuple[str, ...] = (),
) -> str:
    """Emit the RR child ABI with the shared Signal-level state scheduler.

    The historical RR child is a pure ``mealy`` transition and therefore
    cannot consume the Signal-valued rule/action enables published by the
    authoritative ``ResolvedTransition`` lowering.  Keep that legacy text for
    unnested rules, while conditional effects use the same scheduler and state
    helpers as every other composed Clash route.  Transaction accounting stays
    in the hierarchical parent, exactly as it does for the legacy child ABI.
    """

    private_names = _clash_module_names(child)
    transition = child.resolved_transition
    assert transition is not None
    if len(child.request_responses) != 1:
        raise ClashEmissionError("request/response child must expose one interface")
    interface = child.request_responses[0]
    if (
        interface.max_outstanding <= 0
        or interface.ordering is not RequestResponseOrdering.IN_ORDER
    ):
        raise ClashEmissionError(
            "hierarchical request/response requires positive max_outstanding in_order"
        )

    wire_inputs = [
        port for port in child.inputs
        if port.protocol is InterfaceProtocol.WIRE
    ]
    wire_outputs = [
        port for port in child.outputs
        if port.protocol is InterfaceProtocol.WIRE
    ]
    input_type = _component_type_name(child, "Input", function_name)
    output_type = _component_type_name(child, "Output", function_name)
    interface_name = interface.name

    def field(channel: str, suffix: str) -> str:
        return _component_accessor(
            child, interface_name, channel.capitalize() + suffix, function_name,
        )

    req_forward = field("request", "Forward")
    req_backward = field("request", "Backward")
    rsp_forward = field("response", "Forward")
    rsp_backward = field("response", "Backward")
    input_fields = [
        (_component_accessor(child, port.name, component_name=function_name), _emit_type(port.type))
        for port in wire_inputs
    ]
    output_fields: list[tuple[str, str]] = []
    requester = interface.role is RequestResponseRole.REQUESTER
    if requester:
        input_fields.extend((
            (req_backward, "ZLangReadyValidBackward"),
            (
                rsp_forward,
                "ZLangReadyValidForward "
                f"({_emit_type(interface.response_type)})",
            ),
        ))
        output_fields.extend((
            (
                req_forward,
                "ZLangReadyValidForward "
                f"({_emit_type(interface.request_type)})",
            ),
            (rsp_backward, "ZLangReadyValidBackward"),
        ))
    else:
        input_fields.extend((
            (
                req_forward,
                "ZLangReadyValidForward "
                f"({_emit_type(interface.request_type)})",
            ),
            (rsp_backward, "ZLangReadyValidBackward"),
        ))
        output_fields.extend((
            (req_backward, "ZLangReadyValidBackward"),
            (
                rsp_forward,
                "ZLangReadyValidForward "
                f"({_emit_type(interface.response_type)})",
            ),
        ))
    output_fields.extend(
        (_component_accessor(child, port.name, component_name=function_name), _emit_type(port.type))
        for port in wire_outputs
    )

    registers_by_name = {register.name: register for register in child.registers}
    for local_id in formal_observation_ids:
        if local_id.startswith("register:"):
            register_name = local_id.split(":", 1)[1]
            register = registers_by_name.get(register_name)
            if register is None:
                raise ClashEmissionError(
                    f"request/response child '{child.name}' has no typed "
                    f"formal register observation '{local_id}'"
                )
            type_text = _emit_type(register.type)
        elif local_id.startswith("rule:"):
            if not any(
                rule_fire_observation_id(group.rule_name) == local_id
                for group in transition.action_groups
            ):
                raise ClashEmissionError(
                    f"request/response child '{child.name}' has no typed "
                    f"formal rule observation '{local_id}'"
                )
            type_text = "Bit"
        else:
            raise ClashEmissionError(
                "request/response child formal product supports only existing "
                "register and rule-fire observations"
            )
        output_fields.append((
            _request_response_child_formal_accessor(
                child, local_id, function_name,
            ),
            type_text,
        ))
    if not output_fields:
        raise ClashEmissionError("request/response child has no outputs")

    def record(type_name: str, fields: list[tuple[str, str]]) -> str:
        rendered = "\n  , ".join(
            f"{name} :: {type_text}" for name, type_text in fields
        )
        return (
            f"data {type_name} = {type_name}\n"
            f"  {{ {rendered}\n"
            "  } deriving (Generic, NFDataX, Show, Eq)\n"
        )

    component_input = "componentInput"
    delay_nodes: dict[int, expr.Delay | expr.Pipeline] = {}
    for assignment in (*child.assignments, *child.next_assignments):
        _collect_delays(assignment.expression, delay_nodes)
    for local in child.locals:
        _collect_delays(local.expression, delay_nodes)
    for rule in child.rules:
        _collect_delays(rule.guard, delay_nodes)
        for action in rule.actions:
            _collect_delays(action.expression, delay_nodes)
            if action.activation is not None:
                _collect_delays(action.activation, delay_nodes)
    value_names = {
        _leaf_key(expr.InputRef(port.name, port.type)): (
            f"{_component_accessor(child, port.name, component_name=function_name)} <$> {component_input}"
        )
        for port in wire_inputs
    }
    value_names.update({
        _leaf_key(value): (
            private_names.stage(_stage_prefix(value), value.instance, expr.sequential_stage_count(value))
        )
        for value in delay_nodes.values()
    })
    render_state = lambda value: _emit_signal_expression(value, value_names)
    port_value_names = {
        port.name: _clash_name(port.name) for port in child.ports
    }
    bindings: list[str] = ["reset_active = unsafeToActiveHigh hasReset"]

    if requester:
        bindings.extend((
            f"{interface_name}_request_ready = zlangRvReady <$> "
            f"({req_backward} <$> {component_input})",
            f"{interface_name}_response_payload = zlangRvPayload <$> "
            f"({rsp_forward} <$> {component_input})",
            f"{interface_name}_response_valid = zlangRvValid <$> "
            f"({rsp_forward} <$> {component_input})",
        ))
    else:
        bindings.extend((
            f"{interface_name}_request_payload = zlangRvPayload <$> "
            f"({req_forward} <$> {component_input})",
            f"{interface_name}_request_valid = zlangRvValid <$> "
            f"({req_forward} <$> {component_input})",
            f"{interface_name}_response_ready = zlangRvReady <$> "
            f"({rsp_backward} <$> {component_input})",
        ))

    scheduled_wire_outputs = {
        resource.name
        for resource in transition.resources
        if resource.kind is StateResourceKind.OUTPUT
    }
    for local in child.locals:
        if local.compile_time:
            continue
        bindings.append(
            f"{_clash_name(local.name)} = {render_state(local.expression)}"
        )
    for assignment in child.assignments:
        if (
            assignment.channel is None
            and assignment.signal is None
            and assignment.target.name in scheduled_wire_outputs
        ):
            continue
        bindings.append(
            f"{_request_response_assignment_name(assignment, port_value_names)} = "
            f"{render_state(assignment.expression)}"
        )

    # The standalone RR helper adds an outstanding-transaction state machine.
    # A hierarchical descriptor already owns that ledger, so expose the raw
    # requested fields here while retaining the canonical expression names.
    if requester:
        bindings.extend((
            f"{interface_name}_request_valid = "
            f"{interface_name}_request_valid_request",
            f"{interface_name}_response_ready = "
            f"{interface_name}_response_ready_request",
        ))
    else:
        bindings.extend((
            f"{interface_name}_request_ready = "
            f"{interface_name}_request_ready_request",
            f"{interface_name}_response_valid = "
            f"{interface_name}_response_valid_request",
        ))
    bindings.extend((
        f"{interface_name}_request_transfer = "
        f"(\\valid ready -> valid .&. ready) <$> "
        f"{interface_name}_request_valid <*> {interface_name}_request_ready",
        f"{interface_name}_response_transfer = "
        f"(\\valid ready -> valid .&. ready) <$> "
        f"{interface_name}_response_valid <*> {interface_name}_response_ready",
    ))

    bindings.extend(_emit_unified_schedule_bindings(child, render_state))
    for fifo in child.fifos:
        bindings.extend(
            _emit_scheduled_fifo_bindings(child, fifo, render_state)
            if fifo.scheduled
            else _emit_declared_fifo_bindings(fifo, render_state)
        )
    for memory in child.memories:
        bindings.extend(
            _emit_scheduled_memory_bindings(child, memory, render_state)
            if memory.scheduled
            else _emit_memory_bindings(memory, render_state)
        )
    if child.roms:
        bindings.append("zlang_rom_reset_hold = register True (pure False)")
    for rom in child.roms:
        bindings.extend(_emit_rom_bindings(rom, render_state))
    bindings.extend(_emit_unified_register_bindings(child, render_state))
    bindings.extend(_emit_unified_output_bindings(
        child,
        render_state,
        include=scheduled_wire_outputs,
        output_names=port_value_names,
    ))
    for value in delay_nodes.values():
        previous = render_state(value.expression)
        for stage in range(1, expr.sequential_stage_count(value) + 1):
            stage_name = (
                private_names.stage(_stage_prefix(value), value.instance, stage)
            )
            bindings.append(
                f"{stage_name} = register {_zero_value(value.type)} ({previous})"
            )
            previous = stage_name

    if requester:
        output_values = [
            "ZLangReadyValidForward <$> "
            f"{interface_name}_request_payload <*> "
            f"{interface_name}_request_valid",
            "ZLangReadyValidBackward <$> "
            f"{interface_name}_response_ready",
        ]
    else:
        output_values = [
            "ZLangReadyValidBackward <$> "
            f"{interface_name}_request_ready",
            "ZLangReadyValidForward <$> "
            f"{interface_name}_response_payload <*> "
            f"{interface_name}_response_valid",
        ]
    output_values.extend(_clash_name(port.name) for port in wire_outputs)
    for local_id in formal_observation_ids:
        output_values.append(
            _clash_name(local_id.split(":", 1)[1])
            if local_id.startswith("register:")
            else private_names.rule(local_id.split(':', 1)[1])
        )
    constructor = (
        f"{output_type} <$> "
        + " <*> ".join(f"({_clash_apply_arg(value)})" for value in output_values)
    )
    where_block = "\n".join(f"  {binding}" for binding in bindings)
    return (
        record(input_type, input_fields)
        + record(output_type, output_fields)
        + f"{function_name} :: HiddenClockResetEnable ZLangSystem => "
        f"Signal ZLangSystem {input_type} -> Signal ZLangSystem {output_type}\n"
        f"{function_name} {component_input} = {constructor}\n"
        " where\n"
        f"{where_block}\n"
        f"{{-# NOINLINE {function_name} #-}}\n"
    )


def _emit_request_response_child_function(
    child: Module,
    component_name: str | None = None,
    *,
    formal_observation_ids: tuple[str, ...] = (),
) -> str:
    """Emit a closed bundled ABI for one in-order request/response child.

    Hierarchical transaction accounting is owned by the parent connection;
    every peer handshake and scalar dependency crosses the record boundary.
    """
    function_name = component_name or _protocol_child_name(child)
    if len(child.request_responses) != 1:
        raise ClashEmissionError("request/response child must expose one interface")
    interface = child.request_responses[0]
    if interface.max_outstanding <= 0 or interface.ordering is not RequestResponseOrdering.IN_ORDER:
        raise ClashEmissionError(
            "hierarchical request/response requires positive max_outstanding in_order"
        )
    if (
        child.resolved_transition is not None
        and conditional_actions(child.resolved_transition)
    ):
        return _emit_conditional_request_response_child_function(
            child,
            function_name,
            formal_observation_ids=formal_observation_ids,
        )
    wire_inputs = [p for p in child.inputs if p.protocol is InterfaceProtocol.WIRE]
    wire_outputs = [p for p in child.outputs if p.protocol is InterfaceProtocol.WIRE]
    input_type = _component_type_name(child, "Input", function_name)
    output_type = _component_type_name(child, "Output", function_name)
    name = interface.name
    def field(channel: str, suffix: str) -> str:
        return f"{_component_accessor(child, name, channel.capitalize() + suffix, function_name)}"
    req_forward = field("request", "Forward")
    req_backward = field("request", "Backward")
    rsp_forward = field("response", "Forward")
    rsp_backward = field("response", "Backward")
    input_fields = [(_component_accessor(child, p.name, component_name=function_name), _emit_type(p.type)) for p in wire_inputs]
    output_fields: list[tuple[str, str]] = []
    requester = interface.role is RequestResponseRole.REQUESTER
    if requester:
        input_fields += [
            (req_backward, "ZLangReadyValidBackward"),
            (rsp_forward, f"ZLangReadyValidForward ({_emit_type(interface.response_type)})"),
        ]
        output_fields += [
            (req_forward, f"ZLangReadyValidForward ({_emit_type(interface.request_type)})"),
            (rsp_backward, "ZLangReadyValidBackward"),
        ]
    else:
        input_fields += [
            (req_forward, f"ZLangReadyValidForward ({_emit_type(interface.request_type)})"),
            (rsp_backward, "ZLangReadyValidBackward"),
        ]
        output_fields += [
            (req_backward, "ZLangReadyValidBackward"),
            (rsp_forward, f"ZLangReadyValidForward ({_emit_type(interface.response_type)})"),
        ]
    output_fields += [(_component_accessor(child, p.name, component_name=function_name), _emit_type(p.type)) for p in wire_outputs]
    state_register = child.registers[0] if child.registers else None
    formal_field_types: list[tuple[str, str]] = []
    for local_id in formal_observation_ids:
        if local_id.startswith("register:"):
            register_name = local_id.split(":", 1)[1]
            if state_register is None or state_register.name != register_name:
                raise ClashEmissionError(
                    f"request/response child '{child.name}' has no typed "
                    f"formal register observation '{local_id}'"
                )
            type_text = _emit_type(state_register.type)
        elif local_id.startswith("rule:"):
            if child.resolved_transition is None or not any(
                rule_fire_observation_id(item.rule_name) == local_id
                for item in child.resolved_transition.action_groups
            ):
                raise ClashEmissionError(
                    f"request/response child '{child.name}' has no typed "
                    f"formal rule observation '{local_id}'"
                )
            type_text = "Bit"
        else:
            raise ClashEmissionError(
                "request/response child formal product supports only existing "
                "register and rule-fire observations"
            )
        formal_field_types.append((
            _request_response_child_formal_accessor(
                child, local_id, component_name,
            ),
            type_text,
        ))
    output_fields += formal_field_types
    if not output_fields:
        raise ClashEmissionError("request/response child has no outputs")
    def record(type_name: str, fields: list[tuple[str, str]]) -> str:
        rendered = "\n  , ".join(f"{n} :: {t}" for n, t in fields)
        return f"data {type_name} = {type_name}\n  {{ {rendered}\n  }} deriving (Generic, NFDataX, Show, Eq)\n"
    component_input = "componentInput"
    names = {
        _leaf_key(expr.InputRef(p.name, p.type)): f"({_component_accessor(child, p.name, component_name=function_name)} {component_input})"
        for p in wire_inputs
    }
    if len(child.registers) > 1:
        raise ClashEmissionError(
            f"request/response child '{child.name}' currently supports one state register"
        )
    if state_register is not None:
        names[_leaf_key(expr.RegisterRef(state_register.name, state_register.type))] = "state"
    # Raw peer values and requested owned fields.
    if requester:
        names[(_request_response_ref_key(name, RequestResponseChannel.REQUEST, ReadyValidSignal.READY))] = f"(zlangRvReady ({req_backward} {component_input}))"
        names[(_request_response_ref_key(name, RequestResponseChannel.RESPONSE, ReadyValidSignal.PAYLOAD))] = f"(zlangRvPayload ({rsp_forward} {component_input}))"
        names[(_request_response_ref_key(name, RequestResponseChannel.RESPONSE, ReadyValidSignal.VALID))] = f"(zlangRvValid ({rsp_forward} {component_input}))"
    else:
        names[(_request_response_ref_key(name, RequestResponseChannel.REQUEST, ReadyValidSignal.PAYLOAD))] = f"(zlangRvPayload ({req_forward} {component_input}))"
        names[(_request_response_ref_key(name, RequestResponseChannel.REQUEST, ReadyValidSignal.VALID))] = f"(zlangRvValid ({req_forward} {component_input}))"
        names[(_request_response_ref_key(name, RequestResponseChannel.RESPONSE, ReadyValidSignal.READY))] = f"(zlangRvReady ({rsp_backward} {component_input}))"
    def assigned(channel: RequestResponseChannel, signal: ReadyValidSignal, fallback: HardwareType) -> expr.Expression:
        return next((a.expression for a in child.assignments if a.channel is channel and a.signal is signal), expr.Constant(0, fallback))
    if requester:
        req_payload = _emit_expression(assigned(RequestResponseChannel.REQUEST, ReadyValidSignal.PAYLOAD, interface.request_type), names)
        req_requested = _emit_expression(assigned(RequestResponseChannel.REQUEST, ReadyValidSignal.VALID, BitType()), names)
        rsp_requested = _emit_expression(assigned(RequestResponseChannel.RESPONSE, ReadyValidSignal.READY, BitType()), names)
        req_ready = f"zlangRvReady ({req_backward} {component_input})"
        rsp_valid = f"zlangRvValid ({rsp_forward} {component_input})"
        rsp_payload = f"zlangRvPayload ({rsp_forward} {component_input})"
        names[(_request_response_ref_key(name, RequestResponseChannel.REQUEST, ReadyValidSignal.TRANSFER))] = f"(({req_requested}) .&. ({req_ready}))"
        names[(_request_response_ref_key(name, RequestResponseChannel.RESPONSE, ReadyValidSignal.TRANSFER))] = f"(({rsp_valid}) .&. ({rsp_requested}))"
        req_transfer = f"(({req_requested}) .&. ({req_ready}))"
        rsp_transfer = f"(({rsp_valid}) .&. ({rsp_requested}))"
        out_values = [
            f"ZLangReadyValidForward ({req_payload}) ({req_requested})",
            f"ZLangReadyValidBackward ({rsp_requested})",
        ]
        next_count = "()"
    else:
        req_payload = f"zlangRvPayload ({req_forward} {component_input})"
        req_valid = f"zlangRvValid ({req_forward} {component_input})"
        req_ready_requested = _emit_expression(
            assigned(RequestResponseChannel.REQUEST, ReadyValidSignal.READY, BitType()),
            names,
        )
        names[(_request_response_ref_key(name, RequestResponseChannel.REQUEST, ReadyValidSignal.TRANSFER))] = f"(({req_valid}) .&. ({req_ready_requested}))"
        rsp_payload = _emit_expression(assigned(RequestResponseChannel.RESPONSE, ReadyValidSignal.PAYLOAD, interface.response_type), names)
        rsp_requested = _emit_expression(assigned(RequestResponseChannel.RESPONSE, ReadyValidSignal.VALID, BitType()), names)
        rsp_ready = f"zlangRvReady ({rsp_backward} {component_input})"
        names[(_request_response_ref_key(name, RequestResponseChannel.RESPONSE, ReadyValidSignal.VALID))] = f"({rsp_requested})"
        names[(_request_response_ref_key(name, RequestResponseChannel.RESPONSE, ReadyValidSignal.TRANSFER))] = f"(({rsp_requested}) .&. ({rsp_ready}))"
        req_transfer = f"(({req_valid}) .&. ({req_ready_requested}))"
        rsp_transfer = f"(({rsp_requested}) .&. ({rsp_ready}))"
        out_values = [
            f"ZLangReadyValidBackward ({req_ready_requested})",
            f"ZLangReadyValidForward ({rsp_payload}) ({rsp_requested})",
        ]
        next_count = "()"
    for p in wire_outputs:
        assignment = next((a.expression for a in child.assignments if a.target.name == p.name and a.signal is None), expr.Constant(0, p.type))
        out_values.append(_emit_expression(assignment, names))
    accepted_rule_values: dict[str, str] = {}
    formal_rule_observations = any(
        item.startswith("rule:") for item in formal_observation_ids
    )
    if formal_rule_observations:
        transition_ir = child.resolved_transition
        if transition_ir is None or any(
            item.kind is not StateResourceKind.REGISTER
            for item in transition_ir.resources
        ):
            raise ClashEmissionError(
                "request/response child rule observations require the existing "
                "register-only resolved transition"
        )
        groups = ordered_state_groups(transition_ir)
        activation_predicates = conditional_activation_predicates(transition_ir)
        rendered_guards = {
            group.rule_name: _emit_expression(group.guard, names)
            for group in groups
        }
        rendered_activations = tuple(
            _emit_expression(activation, names)
            for activation in activation_predicates
        )
        for selected in groups:
            clauses: list[str] = []
            for region in selection_regions(
                transition_ir, selected.rule_name,
            ):
                guard_values = region[:len(groups)]
                activation_values = region[len(groups):]
                terms = [
                    f"({rendered_guards[group.rule_name]}) == "
                    f"{'high' if value else 'low'}"
                    for group, value in zip(groups, guard_values, strict=True)
                    if value is not None
                ]
                terms.extend(
                    f"({rendered_activation}) == "
                    f"{'high' if value else 'low'}"
                    for rendered_activation, value in zip(
                        rendered_activations, activation_values, strict=True
                    )
                    if value is not None
                )
                clauses.append("(" + " && ".join(terms) + ")")
            condition = " || ".join(clauses) if clauses else "False"
            accepted_rule_values[
                rule_fire_observation_id(selected.rule_name)
            ] = f"(if {condition} then high else low)"
    for local_id in formal_observation_ids:
        if local_id.startswith("register:"):
            out_values.append("state")
        else:
            out_values.append(accepted_rule_values[local_id])
    state_type = _emit_type(state_register.type) if state_register is not None else "()"
    if state_register is not None:
        scheduled: expr.Expression = next(
            (item.expression for item in child.next_assignments
             if item.target.name == state_register.name),
            expr.RegisterRef(state_register.name, state_register.type),
        )
        for rule in reversed(_rule_schedule(child)):
            action = next((item for item in rule.actions
                           if item.target.name == state_register.name), None)
            if action is not None:
                selector = (
                    expr.InputRef(f"rule_{rule.name}_fire", BitType())
                    if formal_rule_observations else rule.guard
                )
                if formal_rule_observations:
                    names[_leaf_key(selector)] = accepted_rule_values[
                        rule_fire_observation_id(rule.name)
                    ]
                scheduled = expr.Mux(
                    selector, action.expression, scheduled, state_register.type,
                )
        next_state = _emit_expression(scheduled, names)
        initial_state = _emit_expression(state_register.initial, names)
    else:
        next_state = "()"
        initial_state = "()"
    transition = f"{function_name}Transition"
    input_expr = "(" + ", ".join("_" for _ in input_fields) + ")" if False else component_input
    # Transaction accounting belongs to the parent connection descriptor.  The
    # child ABI is deliberately stateless here: keeping a private count would
    # duplicate (and potentially disagree with) the parent's accepted-request
    # ledger, especially when directional buffering is present.
    return (
        record(input_type, input_fields) + record(output_type, output_fields) +
        f"{transition} :: {state_type} -> {input_type} -> ({state_type}, {output_type})\n"
        f"{transition} state {input_expr} = ({next_state}, {output_type} "
        + " ".join(f"({v})" for v in out_values) + ")\n\n"
        f"{function_name} :: HiddenClockResetEnable ZLangSystem => Signal ZLangSystem {input_type} -> Signal ZLangSystem {output_type}\n"
        f"{function_name} = mealy {transition} {initial_state}\n"
        f"{{-# NOINLINE {function_name} #-}}\n"
    )


def _request_response_ref_key(interface: str, channel: RequestResponseChannel, signal: ReadyValidSignal) -> tuple[object, ...]:
    return ("RequestResponseRef", interface, channel.value, signal.value)


def _top_leaf_signal(leaf: object) -> str:
    return f"top_{leaf.external_name}"


def _top_payload_leaf_paths(
    type_: HardwareType,
    path: tuple[str, ...],
) -> tuple[tuple[str, ...], ...]:
    """Return the public leaf paths below one aggregate payload type.

    Public vectors preserve their dimensions rather than adding source-path
    indices.  Descend through their element type once; reconstruction below
    maps the resulting component arrays back into the original AoS value.
    """

    if isinstance(type_, StructType):
        return tuple(
            leaf_path
            for field in type_.fields
            for leaf_path in _top_payload_leaf_paths(
                field.type, path + (field.name,)
            )
        )
    if isinstance(type_, TupleType):
        return tuple(
            leaf_path
            for index, element in enumerate(type_.elements)
            for leaf_path in _top_payload_leaf_paths(
                element, path + (f"item{index}",)
            )
        )
    if isinstance(type_, VecType):
        return _top_payload_leaf_paths(type_.element_type, path)
    return (path,)


def _top_payload_reconstructed_value(
    type_: HardwareType,
    path: tuple[str, ...],
    values: dict[tuple[str, ...], str],
) -> str:
    """Rebuild one pure aggregate value from public SoA leaf values."""

    if isinstance(type_, StructType):
        fields = tuple(
            _top_payload_reconstructed_value(
                field.type, path + (field.name,), values
            )
            for field in type_.fields
        )
        return (
            f"({_clash_struct_name(type_.name)} "
            + " ".join(f"({field})" for field in fields)
            + ")"
        )
    if isinstance(type_, TupleType):
        return "(" + ", ".join(
            _top_payload_reconstructed_value(
                element, path + (f"item{index}",), values
            )
            for index, element in enumerate(type_.elements)
        ) + ")"
    if isinstance(type_, VecType):
        leaf_paths = _top_payload_leaf_paths(type_.element_type, path)
        element_names = tuple(
            f"zlangPayloadElement{index}" for index in range(len(leaf_paths))
        )
        bundled = values[leaf_paths[0]]
        pattern = element_names[0]
        for leaf_path, element_name in zip(
            leaf_paths[1:], element_names[1:], strict=True
        ):
            bundled = f"(zip ({bundled}) ({values[leaf_path]}))"
            pattern = f"({pattern}, {element_name})"
        element_values = dict(zip(leaf_paths, element_names, strict=True))
        element = _top_payload_reconstructed_value(
            type_.element_type, path, element_values
        )
        return f"(map (\\{pattern} -> {element}) ({bundled}))"
    return values[path]


def _top_payload_signal(
    type_: HardwareType,
    leaves: dict[tuple[str, ...], object],
    path: tuple[str, ...],
) -> str:
    """Pack recursively flattened top payload leaves into a Signal value."""

    if not _top_payload_needs_soa_bridge(type_):
        return _top_payload_legacy_signal(type_, leaves, path)

    leaf_paths = _top_payload_leaf_paths(type_, path)
    missing = tuple(leaf_path for leaf_path in leaf_paths if leaf_path not in leaves)
    if missing:
        raise ClashEmissionError(
            f"missing top aggregate payload leaf '{'.'.join(missing[0])}'"
        )
    # Scalars and vectors whose element tree contains no split aggregate are
    # already one correctly typed public Signal.
    if leaf_paths == (path,) and not isinstance(type_, (StructType, TupleType)):
        return _top_leaf_signal(leaves[path])
    binder_names = tuple(
        f"zlangPayloadLeaf{index}" for index in range(len(leaf_paths))
    )
    binders = dict(zip(leaf_paths, binder_names, strict=True))
    value = _top_payload_reconstructed_value(type_, path, binders)
    applications = "".join(
        (" <$> " if index == 0 else " <*> ")
        + _top_leaf_signal(leaves[leaf_path])
        for index, leaf_path in enumerate(leaf_paths)
    )
    return f"(\\{' '.join(binder_names)} -> {value}){applications}"


def _top_payload_needs_soa_bridge(type_: HardwareType) -> bool:
    """Keep historical struct-only text unless a new typed bridge is needed."""

    if isinstance(type_, TupleType):
        return True
    if isinstance(type_, VecType):
        return isinstance(type_.element_type, (StructType, TupleType)) or (
            _top_payload_needs_soa_bridge(type_.element_type)
        )
    if isinstance(type_, StructType):
        return any(_top_payload_needs_soa_bridge(field.type) for field in type_.fields)
    return False


def _top_payload_legacy_signal(
    type_: HardwareType,
    leaves: dict[tuple[str, ...], object],
    path: tuple[str, ...],
) -> str:
    """Render the pre-tuple aggregate form byte-for-byte for existing designs."""

    if isinstance(type_, StructType):
        fields = [
            _top_payload_legacy_signal(field.type, leaves, path + (field.name,))
            for field in type_.fields
        ]
        return f"{_clash_struct_name(type_.name)} <$> " + " <*> ".join(fields)
    leaf = leaves.get(path)
    if leaf is None:
        raise ClashEmissionError(
            f"missing top aggregate payload leaf '{'.'.join(path)}'"
        )
    return _top_leaf_signal(leaf)


def _top_forward_signal(
    member: object, leaves: dict[tuple[str, ...], object], endpoint: object,
) -> str:
    payload = _top_payload_signal(member.payload_type, leaves, (endpoint.name, member.name, "payload"))
    valid = _top_leaf_signal(leaves[(endpoint.name, member.name, "valid")])
    return f"ZLangReadyValidForward <$> ({payload}) <*> {valid}"


def _top_backward_signal(member: object, leaves: dict[tuple[str, ...], object], endpoint: object) -> str:
    ready = _top_leaf_signal(leaves[(endpoint.name, member.name, "ready")])
    return f"ZLangReadyValidBackward <$> {ready}"


def _top_payload_projected_value(
    value: str,
    type_: HardwareType,
    path: tuple[str, ...],
    depth: int = 0,
) -> str:
    if not path:
        return value
    if isinstance(type_, VecType):
        binder = f"zlangPayloadItem{depth}"
        projected = _top_payload_projected_value(
            binder, type_.element_type, path, depth + 1
        )
        return f"(map (\\{binder} -> {projected}) ({value}))"
    field_name, *rest = path
    if isinstance(type_, StructType):
        field = next(
            (item for item in type_.fields if item.name == field_name), None
        )
        if field is None:
            raise ClashEmissionError(
                f"unknown top aggregate payload field '{field_name}'"
            )
        selected = f"({_field_accessor(type_.name, field.name)} ({value}))"
        return _top_payload_projected_value(
            selected, field.type, tuple(rest), depth
        )
    if isinstance(type_, TupleType):
        match = re.fullmatch(r"item([0-9]+)", field_name)
        if match is None or int(match.group(1)) >= len(type_.elements):
            raise ClashEmissionError(
                f"invalid top tuple payload component '{field_name}'"
            )
        index = int(match.group(1))
        binders = ", ".join(
            f"zlangTuple{item}" for item in range(len(type_.elements))
        )
        selected = (
            f"((\\({binders}) -> zlangTuple{index}) ({value}))"
        )
        return _top_payload_projected_value(
            selected, type_.elements[index], tuple(rest), depth
        )
    raise ClashEmissionError(
        f"invalid top aggregate payload path '{'.'.join(path)}'"
    )


def _top_payload_projection(
    signal: str,
    type_: HardwareType,
    path: tuple[str, ...],
) -> str:
    if not _top_payload_needs_soa_bridge(type_):
        return _top_payload_legacy_projection(signal, type_, path)
    if not path:
        return signal
    binder = "zlangPayloadValue"
    projected = _top_payload_projected_value(binder, type_, path)
    return f"((\\{binder} -> {projected}) <$> {signal})"


def _top_payload_legacy_projection(
    signal: str,
    type_: HardwareType,
    path: tuple[str, ...],
) -> str:
    """Render the established struct projection when no SoA bridge is needed."""

    rendered = signal
    current = type_
    for field_name in path:
        if not isinstance(current, StructType):
            raise ClashEmissionError(
                f"invalid top aggregate payload path '{'.'.join(path)}'"
            )
        field = next(
            (item for item in current.fields if item.name == field_name), None
        )
        if field is None:
            raise ClashEmissionError(
                f"unknown top aggregate payload field '{field_name}'"
            )
        rendered = f"({_field_accessor(current.name, field.name)} <$> {rendered})"
        current = field.type
    return rendered


def _emit_hierarchical_ordinary_protocol_module(module: Module) -> str:
    """Emit a top with ordinary scalar and ready/valid hierarchy ports.

    Aggregate top ABI emission has a deliberately flattened interface.  M40
    also permits a plain ``rv<T>`` top port, however, and that shape must keep
    the same physical forward/backward relationship when it delegates to a
    child.  Keep this path explicit so the child instance receives typed
    forward values and backward records, while the top-level ABI exposes
    ``*_payload``, ``*_valid`` and ``*_ready`` exactly once.
    """
    private_names = _clash_module_names(module)
    if not module.is_sequential or module.clock is None or module.reset is None:
        raise ClashEmissionError(
            "hierarchical protocol composition requires clock/reset"
        )

    rv_inputs = [
        port for port in module.inputs
        if port.protocol is InterfaceProtocol.READY_VALID
    ]
    rv_outputs = [
        port for port in module.outputs
        if port.protocol is InterfaceProtocol.READY_VALID
    ]
    wire_inputs = [
        port for port in module.inputs
        if port.protocol is InterfaceProtocol.WIRE
    ]
    wire_outputs = [
        port for port in module.outputs
        if port.protocol is InterfaceProtocol.WIRE
    ]
    recursive_catalog = _recursive_protocol_components(
        module,
        error=ClashEmissionError,
    )

    def top_forward(port: Port) -> str:
        if port.direction is PortDirection.INPUT:
            return f"parent_{_clash_name(port.name)}"
        return _clash_name(port.name)

    def top_backward(port: Port) -> str:
        if port.direction is PortDirection.INPUT:
            return f"{_clash_name(port.name)}_ready"
        return f"{_clash_name(port.name)}_backward"

    def forward(endpoint: ProtocolEndpoint) -> str:
        if endpoint.owner == module.name:
            port = next(
                item for item in module.ports if item.name == endpoint.name
            )
            return top_forward(port)
        return private_names.child_signal(endpoint.owner, endpoint.name)

    def backward(endpoint: ProtocolEndpoint) -> str:
        if endpoint.owner == module.name:
            port = next(
                item for item in module.ports if item.name == endpoint.name
            )
            return top_backward(port)
        return private_names.child_signal(endpoint.owner, endpoint.name, "ready")

    # Scalar bindings are rendered through typed leaf identities.  This keeps
    # the parent argument name independent from source spelling and avoids
    # any generated-text substitution.
    parent_signal_names = {
        _leaf_key(expr.InputRef(port.name, port.type)):
        f"parent_{_clash_name(port.name)}"
        for port in wire_inputs
    }
    parent_signal_names.update(_clash_child_leaf_names(module, private_names))

    delay_nodes: dict[int, expr.Delay | expr.Pipeline] = {}
    for assignment in (*module.assignments, *module.next_assignments):
        _collect_delays(assignment.expression, delay_nodes)
    for binding in module.instance_bindings:
        _collect_delays(binding.expression, delay_nodes)
    for local in module.locals:
        _collect_delays(local.expression, delay_nodes)
    for rule in module.rules:
        _collect_delays(rule.guard, delay_nodes)
        for action in rule.actions:
            _collect_delays(action.expression, delay_nodes)
            if action.activation is not None:
                _collect_delays(action.activation, delay_nodes)
    state_names = {
        **parent_signal_names,
        **{
            _leaf_key(value): (
                private_names.stage(_stage_prefix(value), value.instance, expr.sequential_stage_count(value))
            )
            for value in delay_nodes.values()
        },
    }
    render_state = lambda value: _emit_signal_expression(value, state_names)
    bindings: list[str] = []
    top_input_ready: dict[str, str] = {}
    top_output_forward: dict[str, str] = {}

    for index, instance in enumerate(module.instances):
        try:
            child_entry = recursive_catalog.child(
                recursive_catalog.hierarchy.root_path,
                instance.name,
            )
        except HierarchyError:
            raise ClashEmissionError(
                f"missing elaborated child module '{instance.module}'"
            ) from None
        child = child_entry.module
        incoming = [
            edge for edge in module.hierarchical_connections
            if edge.destination.owner == instance.name
        ]
        outgoing = [
            edge for edge in module.hierarchical_connections
            if edge.source.owner == instance.name
        ]
        args: list[str] = []
        # Child component functions expose the closed hierarchy ABI in a
        # stable order: scalar inputs first, then forward ready/valid inputs,
        # followed by backward ready records for ready/valid outputs.  The
        # source port declaration order is not that ABI (a mixed child may
        # declare an RV input before a scalar twiddle/vector input), so build
        # arguments by protocol class rather than relying on declaration
        # order.
        ordered_inputs = [
            port for port in child.inputs
            if port.protocol is InterfaceProtocol.WIRE
        ] + [
            port for port in child.inputs
            if port.protocol is InterfaceProtocol.READY_VALID
        ]
        for port in ordered_inputs:
            if port.protocol is InterfaceProtocol.READY_VALID:
                edge = next(
                    (item for item in incoming if item.destination.name == port.name),
                    None,
                )
                if edge is None:
                    raise ClashEmissionError(
                        f"unconnected protocol input '{instance.name}.{port.name}'"
                    )
                args.append(forward(edge.source))
            else:
                edge = next(
                    (item for item in incoming if item.destination.name == port.name),
                    None,
                )
                if edge is not None:
                    args.append(forward(edge.source))
                    continue
                binding = next(
                    (
                        item.expression for item in module.instance_bindings
                        if item.instance == instance.name and item.port == port.name
                    ),
                    None,
                )
                if binding is None:
                    raise ClashEmissionError(
                        f"instance '{instance.name}' is missing scalar binding '{port.name}'"
                    )
                args.append(_emit_signal_expression(binding, parent_signal_names))
        for port in child.outputs:
            if port.protocol is not InterfaceProtocol.READY_VALID:
                continue
            edge = next(
                (item for item in outgoing if item.source.name == port.name),
                None,
            )
            if edge is None:
                raise ClashEmissionError(
                    f"unconnected protocol output '{instance.name}.{port.name}'"
                )
            args.append(backward(edge.destination))

        call = recursive_catalog.get(
            recursive_catalog.application_key(
                recursive_catalog.hierarchy.root_path,
                instance.name,
                recursive_catalog.root_owner,
            )
        )
        if call is None:
            raise ClashEmissionError(
                f"missing specialization identity for physical instance "
                f"'{module.name}.{instance.name}'"
            )
        component_function = call
        call = _closed_protocol_child_call(
            child,
            component_function,
            args,
            error=ClashEmissionError,
        )
        result_ports = [
            port for port in child.inputs
            if port.protocol is InterfaceProtocol.READY_VALID
        ]
        result_ports += [
            port for port in child.outputs
            if port.protocol is InterfaceProtocol.READY_VALID
        ]
        result_ports += [
            port for port in child.outputs
            if port.protocol is InterfaceProtocol.WIRE
        ]
        if not result_ports:
            raise ClashEmissionError(
                f"protocol child '{child.name}' has no emitted ports"
            )
        result = private_names.instance_helper(instance.name, "result")
        bindings.append(f"{result} = {call}")

        def projection(position: int) -> str:
            return _closed_protocol_child_projection(
                child,
                result_ports[position],
                result,
                position,
                len(result_ports),
                component_function,
            )

        for position, port in enumerate(result_ports):
            if port.protocol is InterfaceProtocol.READY_VALID and port.direction is PortDirection.INPUT:
                bindings.append(
                    f"{backward(ProtocolEndpoint(instance.name, port.name, PortDirection.INPUT, port.protocol, port.type))} = {projection(position)}"
                )
            elif port.protocol is InterfaceProtocol.READY_VALID and port.direction is PortDirection.OUTPUT:
                bindings.append(
                    f"{forward(ProtocolEndpoint(instance.name, port.name, PortDirection.OUTPUT, port.protocol, port.type))} = {projection(position)}"
                )
            elif port.protocol is InterfaceProtocol.WIRE:
                bindings.append(
                    f"{private_names.child_signal(instance.name, port.name)} = {projection(position)}"
                )

    # Typed hierarchical connections provide both physical directions.  A
    # top input's ready is a Bit output, so unwrap the child's backward record;
    # all child-to-child and child-to-top-output ready paths remain records.
    for connection in module.hierarchical_connections:
        if connection.source.protocol is InterfaceProtocol.WIRE:
            bindings.append(
                f"{forward(connection.destination)} = {forward(connection.source)}"
            )
            continue
        bindings.append(
            f"{forward(connection.destination)} = {forward(connection.source)}"
        )
        source_back = backward(connection.source)
        destination_back = backward(connection.destination)
        if connection.source.owner == module.name and connection.source.direction is PortDirection.INPUT:
            bindings.append(
                f"{source_back} = zlangRvReady <$> {destination_back}"
            )
            top_input_ready[connection.source.name] = source_back
        else:
            bindings.append(f"{source_back} = {destination_back}")
        if connection.destination.owner == module.name and connection.destination.direction is PortDirection.OUTPUT:
            top_output_forward[connection.destination.name] = forward(connection.source)

    transition_ir = module.resolved_transition
    unified_state = bool(
        transition_ir is not None
        and (
            transition_ir.resources
            or transition_ir.action_groups
            or module.registers
            or module.next_assignments
            or module.rules
        )
    )
    has_local_storage = bool(module.fifos or module.memories or module.roms)
    scheduled_wire_outputs: set[str] = set()
    if unified_state or has_local_storage:
        missing_input = next(
            (port.name for port in rv_inputs if port.name not in top_input_ready),
            None,
        )
        if missing_input is not None:
            raise ClashEmissionError(
                f"top ready/valid input '{missing_input}' has no hierarchical driver"
            )
        missing_output = next(
            (
                port.name for port in rv_outputs
                if port.name not in top_output_forward
            ),
            None,
        )
        if missing_output is not None:
            raise ClashEmissionError(
                f"top ready/valid output '{missing_output}' has no hierarchical driver"
            )
        bindings.append("reset_active = unsafeToActiveHigh hasReset")
        for local in module.locals:
            if local.compile_time:
                continue
            bindings.append(
                f"{_clash_name(local.name)} = {render_state(local.expression)}"
            )
        for port in rv_inputs:
            name = _clash_name(port.name)
            source = top_forward(port)
            ready = top_input_ready[port.name]
            bindings.extend((
                f"{name}_payload = zlangRvPayload <$> {source}",
                f"{name}_valid = zlangRvValid <$> {source}",
                f"{name}_transfer = (\\valid ready -> valid .&. ready) "
                f"<$> {name}_valid <*> {ready}",
            ))
        for port in rv_outputs:
            name = _clash_name(port.name)
            source = top_output_forward[port.name]
            bindings.extend((
                f"{name}_payload = zlangRvPayload <$> {source}",
                f"{name}_valid = zlangRvValid <$> {source}",
                f"{name}_ready = zlangRvReady <$> {top_backward(port)}",
                f"{name}_transfer = (\\valid ready -> valid .&. ready) "
                f"<$> {name}_valid <*> {name}_ready",
            ))

    if unified_state:
        bindings.extend(_emit_unified_schedule_bindings(module, render_state))
        scheduled_wire_outputs = {
            resource.name
            for resource in transition_ir.resources
            if resource.kind is StateResourceKind.OUTPUT
        }
    for fifo in module.fifos:
        bindings.extend(
            _emit_scheduled_fifo_bindings(module, fifo, render_state)
            if fifo.scheduled
            else _emit_declared_fifo_bindings(fifo, render_state)
        )
    for memory in module.memories:
        bindings.extend(
            _emit_scheduled_memory_bindings(module, memory, render_state)
            if memory.scheduled
            else _emit_memory_bindings(memory, render_state)
        )
    if module.roms:
        bindings.append("zlang_rom_reset_hold = register True (pure False)")
    for rom in module.roms:
        bindings.extend(_emit_rom_bindings(rom, render_state))
    if unified_state:
        bindings.extend(_emit_unified_register_bindings(module, render_state))
        bindings.extend(_emit_unified_output_bindings(
            module, render_state, include=scheduled_wire_outputs,
        ))
    if unified_state or has_local_storage:
        for value in delay_nodes.values():
            previous = render_state(value.expression)
            for stage in range(1, expr.sequential_stage_count(value) + 1):
                name = private_names.stage(_stage_prefix(value), value.instance, stage)
                bindings.append(
                    f"{name} = register {_zero_value(value.type)} ({previous})"
                )
                previous = name

    for port in rv_inputs:
        if port.name not in top_input_ready:
            raise ClashEmissionError(
                f"top ready/valid input '{port.name}' has no hierarchical driver"
            )
    for port in rv_outputs:
        if port.name not in top_output_forward:
            raise ClashEmissionError(
                f"top ready/valid output '{port.name}' has no hierarchical driver"
            )

    for port in wire_outputs:
        if port.name in scheduled_wire_outputs:
            continue
        assignment = next(
            (item for item in module.assignments if item.target.name == port.name),
            None,
        )
        if assignment is None:
            raise ClashEmissionError(f"top scalar output '{port.name}' is not driven")
        bindings.append(
            f"{_clash_name(port.name)} = "
            f"{render_state(assignment.expression)}"
        )

    output_values: list[str] = [
        top_input_ready[port.name] for port in rv_inputs
    ]
    output_types: list[str] = [
        f"Signal ZLangSystem Bit" for _ in rv_inputs
    ]
    output_annotations: list[str] = [
        f'PortName "{port.name}_ready"' for port in rv_inputs
    ]
    for port in rv_outputs:
        output_values.append(top_output_forward[port.name])
        output_types.append(
            f"Signal ZLangSystem (ZLangReadyValidForward ({_emit_type(port.type)}))"
        )
        output_annotations.append(
            _top_forward_port_annotation(port.name, port.type, "valid")
        )
    for port in wire_outputs:
        output_values.append(_clash_name(port.name))
        output_types.append(f"Signal ZLangSystem ({_emit_type(port.type)})")
        output_annotations.append(
            _top_value_port_annotation(port.name, port.type)
        )

    input_types: list[str] = [
        f"Signal ZLangSystem ({_emit_type(port.type)})" for port in wire_inputs
    ]
    input_types += [
        f"Signal ZLangSystem (ZLangReadyValidForward ({_emit_type(port.type)}))"
        for port in rv_inputs
    ]
    input_types += [
        "Signal ZLangSystem ZLangReadyValidBackward" for _ in rv_outputs
    ]
    output_type = _emit_product(output_types)
    circuit_signature = " -> ".join((*input_types, output_type))
    circuit_arguments = " ".join(
        [
            *(f"parent_{_clash_name(port.name)}" for port in wire_inputs),
            *(f"parent_{_clash_name(port.name)}" for port in rv_inputs),
            *(f"{_clash_name(port.name)}_backward" for port in rv_outputs),
        ]
    )
    circuit_lhs = f"circuit {circuit_arguments}" if circuit_arguments else "circuit"
    top_signature = " -> ".join(
        [f"Clock ZLangSystem", f"Reset ZLangSystem", *input_types, output_type]
    )
    top_arguments = " ".join(
        [module.clock, module.reset, circuit_arguments]
    )
    input_annotations = [
        f'PortName "{module.clock}"', f'PortName "{module.reset}"',
        *(
            _top_value_port_annotation(port.name, port.type)
            for port in wire_inputs
        ),
        *(
            _top_forward_port_annotation(port.name, port.type, "valid")
            for port in rv_inputs
        ),
        *(f'PortName "{port.name}_ready"' for port in rv_outputs),
    ]
    output_annotation = (
        output_annotations[0]
        if len(output_annotations) == 1
        else f'PortProduct "" [{", ".join(output_annotations)}]'
    )
    extensions, imports, declarations = _emit_prelude(module)
    declarations = _ready_valid_declarations() + declarations
    declarations += _domain_declaration(module, "ZLangSystem") + "\n"
    component_definitions: list[str] = []
    for component in recursive_catalog.components:
        definition = _emit_protocol_child_function(
            component.child,
            component.component_name,
            recursive_catalog,
            component.owner,
            component.representative_path,
        )
        pragma = f"{{-# NOINLINE {component.component_name} #-}}"
        if pragma not in definition:
            definition = definition.rstrip() + "\n" + pragma + "\n"
        component_definitions.append(definition)
    child_text = "\n".join(component_definitions)
    where_block = "\n".join(f"  {item}" for item in bindings)
    output_value = _emit_product(output_values)
    return f'''{extensions}
module {module.name} where

{imports}
{declarations}
{child_text}
circuit :: HiddenClockResetEnable ZLangSystem => {circuit_signature}
{circuit_lhs} = {output_value}
 where
{where_block}

topEntity :: {top_signature}
topEntity {top_arguments} = exposeClockResetEnable circuit {module.clock} {_top_reset_expression(module)} enableGen {circuit_arguments}

{{-# ANN topEntity
  (Synthesize
    {{ t_name = "{module.name}"
    , t_inputs = [{', '.join(input_annotations)}]
    , t_output = {output_annotation}
    }}) #-}}
'''


def _emit_hierarchical_protocol_module(module: Module) -> str:
    """Emit a preserved child hierarchy for the initial RV composition slice."""
    private_names = _clash_module_names(module)
    if not module.is_sequential or module.clock is None or module.reset is None:
        raise ClashEmissionError("hierarchical protocol composition requires clock/reset")
    if (
        not module.aggregate_protocol_endpoints
        and any(port.protocol is InterfaceProtocol.READY_VALID for port in module.ports)
        and not any(
            connection.buffer_depth
            or connection.request_buffer_depth
            or connection.response_buffer_depth
            for connection in module.hierarchical_connections
        )
    ):
        return _emit_hierarchical_ordinary_protocol_module(module)
    top_abi = module.top_aggregate_abi
    top_leaves = {
        leaf.member_path: leaf for leaf in top_abi.leaves
    }
    delegated_children: dict[tuple[str, str], object] = {}
    for connection in module.aggregate_protocol_connections:
        if not connection.delegation:
            continue
        top_name = connection.source
        child_name, child_endpoint = connection.destination.split(".", 1)
        top_endpoint = next((item for item in module.aggregate_protocol_endpoints if item.name == top_name), None)
        instance_decl = next((item for item in module.instances if item.name == child_name), None)
        child = (
            _elaborated_child_for_instance(module, child_name)
            if instance_decl is not None else None
        )
        child_aggregate = next((item for item in child.aggregate_protocol_endpoints if item.name == child_endpoint), None) if child else None
        if top_endpoint is None or child is None or child_aggregate is None:
            raise ClashEmissionError("invalid top aggregate delegation in elaborated IR")
        for member in top_endpoint.members:
            delegated_children[(child_name, f"{child_endpoint}__{member.name}")] = (top_endpoint, member, child_aggregate)

    delay_nodes: dict[int, expr.Delay | expr.Pipeline] = {}
    for assignment in (*module.assignments, *module.next_assignments):
        _collect_delays(assignment.expression, delay_nodes)
    for binding in module.instance_bindings:
        _collect_delays(binding.expression, delay_nodes)
    for local in module.locals:
        _collect_delays(local.expression, delay_nodes)
    for rule in module.rules:
        _collect_delays(rule.guard, delay_nodes)
        for action in rule.actions:
            _collect_delays(action.expression, delay_nodes)
            if action.activation is not None:
                _collect_delays(action.activation, delay_nodes)
    delay_names = {
        _leaf_key(value): (
            private_names.stage(_stage_prefix(value), value.instance, expr.sequential_stage_count(value))
        )
        for value in delay_nodes.values()
    }
    parent_signal_names = {
        _leaf_key(expr.InputRef(port.name, port.type)):
        f"parent_{_clash_name(port.name)}"
        for port in module.inputs
        if "__" not in port.name
    }
    parent_signal_names.update(_clash_child_leaf_names(module, private_names))
    state_names = {**parent_signal_names, **delay_names}
    render_state = lambda value: _emit_signal_expression(value, state_names)
    bindings: list[str] = []
    # Immutable source locals are part of the closed child component scope.
    # Emit them before port equations so source-defined bridges do not leave
    # free names in generated Clash.
    for local in module.locals:
        if local.compile_time:
            continue
        bindings.append(
            f"{_clash_name(local.name)} = {render_state(local.expression)}"
        )
    if (
        module.registers
        or module.rules
        or module.fifos
        or module.memories
        or module.roms
    ):
        bindings.append("reset_active = unsafeToActiveHigh hasReset")
    ordered_rules = _rule_schedule(module)
    transition_ir = module.resolved_transition
    if module.rules and transition_ir is None:
        raise ClashEmissionError(
            f"hierarchical protocol module '{module.name}' lacks resolved "
            "transition IR",
            code="ZL-BACKEND-CLASH-CONDITIONAL-ACTION",
        )
    unified_state = bool(
        transition_ir is not None
        and (
            transition_ir.resources
            or transition_ir.action_groups
            or module.registers
            or module.next_assignments
            or module.rules
        )
    )
    if unified_state:
        bindings.extend(_emit_unified_schedule_bindings(module, render_state))
        bindings.extend(_emit_unified_register_bindings(module, render_state))
    else:
        for rule in ordered_rules:
            raw = render_state(rule.guard)
            bindings.append(
                f"{private_names.rule(rule.name)} = (\\guard resetActive -> if resetActive then low else guard) "
                f"<$> {_clash_apply_arg(raw)} <*> reset_active"
            )
        next_by_register = {item.target.name: item.expression for item in module.next_assignments}
        for register in module.registers:
            bindings.append(
                f"{register.name} = register {_emit_expression(register.initial)} ({register.name}_next)"
            )
            scheduled: expr.Expression = next_by_register.get(
                register.name, expr.RegisterRef(register.name, register.type)
            )
            for rule in reversed(ordered_rules):
                for action in rule.actions:
                    if action.target.name == register.name:
                        scheduled = expr.Mux(
                            expr.InputRef(private_names.rule(rule.name), BitType()),
                            action.expression, scheduled, register.type
                        )
            bindings.append(
                f"{register.name}_next = {render_state(scheduled)}"
            )
    for fifo in module.fifos:
        bindings.extend(
            _emit_scheduled_fifo_bindings(module, fifo, render_state)
            if fifo.scheduled
            else _emit_declared_fifo_bindings(fifo, render_state)
        )
    for memory in module.memories:
        bindings.extend(
            _emit_scheduled_memory_bindings(module, memory, render_state)
            if memory.scheduled
            else _emit_memory_bindings(memory, render_state)
        )
    if module.roms:
        bindings.append("zlang_rom_reset_hold = register True (pure False)")
    for rom in module.roms:
        bindings.extend(_emit_rom_bindings(rom, render_state))
    scheduled_wire_outputs = {
        resource.name
        for resource in (
            transition_ir.resources if transition_ir is not None else ()
        )
        if resource.kind is StateResourceKind.OUTPUT
    }
    if unified_state and scheduled_wire_outputs:
        bindings.extend(_emit_unified_output_bindings(
            module, render_state, include=scheduled_wire_outputs,
        ))
    for value in delay_nodes.values():
        previous = render_state(value.expression)
        for stage in range(1, expr.sequential_stage_count(value) + 1):
            name = private_names.stage(_stage_prefix(value), value.instance, stage)
            bindings.append(
                f"{name} = register {_zero_value(value.type)} ({previous})"
            )
            previous = name

    def emit_delegated_outputs(instance_name: str, child: Module) -> None:
        for port in child.ports:
            delegated_info = delegated_children.get((instance_name, port.name))
            if delegated_info is None:
                continue
            endpoint, member, _child_endpoint = delegated_info
            top_path = (endpoint.name, member.name)
            if member.protocol is InterfaceProtocol.READY_VALID:
                if port.direction is PortDirection.INPUT:
                    ready_signal = private_names.child_signal(instance_name, port.name, "ready")
                    bindings.append(
                        f"{_top_leaf_signal(top_leaves[top_path + ('ready',)])} = "
                        f"zlangRvReady <$> {ready_signal}"
                    )
                else:
                    forward_signal = private_names.child_signal(instance_name, port.name)
                    for leaf in top_abi.leaves:
                        if leaf.member_path[:2] != top_path:
                            continue
                        if leaf.signal_kind == "valid":
                            bindings.append(
                                f"{_top_leaf_signal(leaf)} = zlangRvValid <$> {forward_signal}"
                            )
                        elif leaf.signal_kind == "payload":
                            field_path = leaf.member_path[3:]
                            bindings.append(
                                f"{_top_leaf_signal(leaf)} = "
                                f"{_top_payload_projection(f'(zlangRvPayload <$> {forward_signal})', member.payload_type, field_path)}"
                            )
            elif member.protocol is InterfaceProtocol.WIRE and port.direction is PortDirection.OUTPUT:
                leaf = top_leaves[top_path]
                bindings.append(f"{_top_leaf_signal(leaf)} = {private_names.child_signal(instance_name, port.name)}")
    extensions, imports, declarations = _emit_prelude(module)
    declarations = _ready_valid_declarations() + declarations
    declarations += _domain_declaration(module, "ZLangSystem") + "\n\n"
    child_text = "\n".join(
        _emit_request_response_child_function(child, component_name)
        if child.request_responses
        else _emit_protocol_child_function(child, component_name)
        for child, component_name in _specialized_protocol_children(module)
    )
    if any(
        connection.buffer_depth
        or connection.request_buffer_depth
        or connection.response_buffer_depth
        for connection in module.hierarchical_connections
    ) or module.request_response_connections:
        if not any(
            binding.startswith("reset_active =") for binding in bindings
        ):
            bindings.append("reset_active = unsafeToActiveHigh hasReset")
    top_protocol_inputs: dict[str, str] = {}
    top_protocol_backwards: dict[str, str] = {}
    for endpoint in module.aggregate_protocol_endpoints:
        for member in endpoint.members:
            path = (endpoint.name, member.name)
            member_leaves = {
                leaf.member_path: leaf for leaf in top_abi.leaves
                if leaf.member_path[:2] == path
            }
            synthetic = f"{endpoint.name}__{member.name}"
            if member.protocol is InterfaceProtocol.READY_VALID:
                if endpoint.role != member.source_role:
                    top_protocol_inputs[synthetic] = _top_forward_signal(member, member_leaves, endpoint)
                else:
                    top_protocol_backwards[synthetic] = _top_backward_signal(member, member_leaves, endpoint)
            elif endpoint.role != member.source_role:
                top_protocol_inputs[synthetic] = _top_leaf_signal(member_leaves[path])
    def hierarchy_forward(endpoint: ProtocolEndpoint) -> str:
        """Return the value/forward signal named by a typed hierarchy edge."""

        if endpoint.owner == module.name:
            port = next(
                item for item in module.ports if item.name == endpoint.name
            )
            if port.direction is PortDirection.INPUT:
                return f"parent_{_clash_name(port.name)}"
            return _clash_name(port.name)
        return private_names.child_signal(endpoint.owner, endpoint.name)

    def hierarchy_backward(endpoint: ProtocolEndpoint) -> str:
        """Return the ready signal/record named by a typed hierarchy edge."""

        if endpoint.owner == module.name:
            port = next(
                item for item in module.ports if item.name == endpoint.name
            )
            if port.direction is PortDirection.INPUT:
                return f"{_clash_name(port.name)}_ready"
            return f"{_clash_name(port.name)}_backward"
        return private_names.child_signal(endpoint.owner, endpoint.name, "ready")

    def rr_endpoint_name(endpoint: object, channel: str) -> str:
        return private_names.child_signal(endpoint.owner, f"{endpoint.name}_{channel}")

    rr_trackers: dict[int, tuple[str, int]] = {}
    for descriptor in module.request_response_connections:
        request = descriptor.request.source
        tracker = (
            f"rr_{private_names.instance(request.owner)}_"
            f"{_clash_name(request.name)}_"
            f"{private_names.instance(descriptor.response.destination.owner)}_"
            "outstanding"
        )
        width = max(1, descriptor.max_outstanding.bit_length())
        rr_trackers[id(descriptor.request)] = (tracker, descriptor.max_outstanding)
        rr_trackers[id(descriptor.response)] = (tracker, descriptor.max_outstanding)
        bindings.extend((
            f"{tracker} = register (0 :: Unsigned {width}) {tracker}_next",
            f"{tracker}_next = (\\count requestAccept responseConsume -> case (requestAccept == high, responseConsume == high) of {{ (True, False) -> if count < {descriptor.max_outstanding} then count + 1 else count; (False, True) -> if count > 0 then count - 1 else count; _ -> count }}) <$> {tracker} <*> {tracker}_request_accept <*> {tracker}_response_consume",
        ))
        request_edge = descriptor.request
        response_edge = descriptor.response
        request_prefix = (
            f"{rr_endpoint_name(request_edge.source, 'request')}_"
            f"{rr_endpoint_name(request_edge.destination, 'request')}_buffer"
        )
        response_prefix = (
            f"{rr_endpoint_name(response_edge.source, 'response')}_"
            f"{rr_endpoint_name(response_edge.destination, 'response')}_buffer"
        )
        request_occupancy = (
            f"{request_prefix}_count" if request_edge.request_buffer_depth else
            f"(0 :: Unsigned {width}) <$ {tracker}"
        )
        response_occupancy = (
            f"{response_prefix}_count" if response_edge.response_buffer_depth else
            f"(0 :: Unsigned {width}) <$ {tracker}"
        )
        response_occupancy_width = (
            max(1, response_edge.response_buffer_depth.bit_length())
            if response_edge.response_buffer_depth else width
        )
        response_for_ledger = (
            "resize responses" if response_occupancy_width != width else "responses"
        )
        bindings.extend((
            f"{tracker}_request_occupancy = {request_occupancy}",
            f"{tracker}_response_occupancy = {response_occupancy}",
            f"{tracker}_waiting_response = (\\outstanding responses -> outstanding - {response_for_ledger}) <$> {tracker} <*> {tracker}_response_occupancy",
        ))
    for instance in module.instances:
        child = _elaborated_child_for_instance(module, instance.name)
        if child is None:
            raise ClashEmissionError(f"missing elaborated child module '{instance.module}'")
        child_function = _protocol_instance_child_name(module, instance.name, child)
        incoming = [c for c in module.hierarchical_connections if c.destination.owner == instance.name]
        outgoing = [c for c in module.hierarchical_connections if c.source.owner == instance.name]
        if child.request_responses:
            if len(child.request_responses) != 1:
                raise ClashEmissionError("request/response hierarchy supports one child interface")
            interface = child.request_responses[0]
            name = interface.name
            wire_inputs = [p for p in child.inputs if p.protocol is InterfaceProtocol.WIRE]
            wire_outputs = [p for p in child.outputs if p.protocol is InterfaceProtocol.WIRE]
            scalar_args: list[str] = []
            for port in wire_inputs:
                bound = next((b.expression for b in module.instance_bindings
                              if b.instance == instance.name and b.port == port.name), None)
                if bound is None:
                    raise ClashEmissionError(f"instance '{instance.name}' is missing scalar binding '{port.name}'")
                scalar_args.append(
                    _emit_signal_expression(bound, parent_signal_names)
                )
            if interface.role is RequestResponseRole.REQUESTER:
                rr_args = [
                    private_names.child_signal(instance.name, f"{name}_request_ready"),
                    private_names.child_signal(instance.name, f"{name}_response"),
                ]
            else:
                rr_args = [
                    private_names.child_signal(instance.name, f"{name}_request"),
                    private_names.child_signal(instance.name, f"{name}_response_ready"),
                ]
            args = scalar_args + rr_args
            input_type = _component_type_name(child, "Input", child_function)
            output_type = _component_type_name(child, "Output", child_function)
            component_input = private_names.instance_helper(instance.name, "component_input")
            component_output = private_names.instance_helper(instance.name, "component_output")
            bindings.append(
                f"{component_input} = {input_type} <$> "
                + " <*> ".join(_clash_apply_arg(arg) for arg in args)
            )
            bindings.append(
                f"{component_output} = "
                f"{child_function} "
                f"{component_input}"
            )
            output_fields: list[tuple[str, str]] = [
                (
                    private_names.child_signal(instance.name, p.name),
                    _component_accessor(child, p.name, component_name=child_function),
                )
                for p in wire_outputs
            ]
            if interface.role is RequestResponseRole.REQUESTER:
                output_fields += [
                    (private_names.child_signal(instance.name, f"{name}_request"), _component_accessor(child, name, "RequestForward", child_function)),
                    (private_names.child_signal(instance.name, f"{name}_response_ready"), _component_accessor(child, name, "ResponseBackward", child_function)),
                ]
            else:
                output_fields += [
                    (private_names.child_signal(instance.name, f"{name}_request_ready"), _component_accessor(child, name, "RequestBackward", child_function)),
                    (private_names.child_signal(instance.name, f"{name}_response"), _component_accessor(child, name, "ResponseForward", child_function)),
                ]
            for lhs, accessor in output_fields:
                bindings.append(f"{lhs} = {accessor} <$> {component_output}")
            continue
        args: list[str] = []
        ordered_inputs = [p for p in child.inputs if p.protocol is InterfaceProtocol.WIRE]
        ordered_inputs += [p for p in child.inputs if p.protocol is InterfaceProtocol.READY_VALID]
        for port in ordered_inputs:
            if port.protocol is InterfaceProtocol.READY_VALID:
                edge = next((c for c in incoming if c.destination.name == port.name), None)
                delegated_input = top_protocol_inputs.get(port.name)
                if delegated_input is not None and (instance.name, port.name) in delegated_children:
                    args.append(delegated_input)
                elif edge is None:
                    raise ClashEmissionError(f"unconnected protocol input '{instance.name}.{port.name}'")
                else:
                    args.append(hierarchy_forward(edge.source))
            elif port.protocol is InterfaceProtocol.WIRE:
                edge = next((c for c in incoming if c.destination.name == port.name), None)
                if edge is not None:
                    args.append(hierarchy_forward(edge.source))
                    continue
                delegated_input = top_protocol_inputs.get(port.name)
                if delegated_input is not None and (instance.name, port.name) in delegated_children:
                    args.append(delegated_input)
                    continue
                bound = next((b.expression for b in module.instance_bindings
                              if b.instance == instance.name and b.port == port.name), None)
                if bound is None:
                    raise ClashEmissionError(f"instance '{instance.name}' is missing scalar binding '{port.name}'")
                args.append(
                    _emit_signal_expression(bound, parent_signal_names)
                )
        for port in child.outputs:
            if port.protocol is InterfaceProtocol.READY_VALID:
                edge = next((c for c in outgoing if c.source.name == port.name), None)
                delegated_backward = top_protocol_backwards.get(port.name)
                if delegated_backward is not None and (instance.name, port.name) in delegated_children:
                    args.append(delegated_backward)
                elif edge is None:
                    raise ClashEmissionError(f"unconnected protocol output '{instance.name}.{port.name}'")
                else:
                    args.append(private_names.child_signal(instance.name, port.name, "ready"))
        if _uses_bundled_component_abi(child):
            input_type = _component_type_name(child, "Input", child_function)
            component_input = private_names.instance_helper(instance.name, "component_input")
            component_output = private_names.instance_helper(instance.name, "component_output")
            constructor = f"{input_type} <$> " + " <*> ".join(_clash_apply_arg(arg) for arg in args)
            bindings.append(f"{component_input} = {constructor}")
            bindings.append(
                f"{component_output} = "
                f"{child_function} "
                f"{component_input}"
            )
            for port in child.inputs:
                if port.protocol is InterfaceProtocol.READY_VALID:
                    accessor = _component_accessor(child, port.name, "Backward", child_function)
                    bindings.append(
                        f"{private_names.child_signal(instance.name, port.name, 'ready')} = {accessor} <$> {component_output}"
                    )
            for port in child.outputs:
                suffix = "Forward" if port.protocol is InterfaceProtocol.READY_VALID else ""
                accessor = _component_accessor(child, port.name, suffix, child_function)
                bindings.append(
                    f"{private_names.child_signal(instance.name, port.name)} = {accessor} <$> {component_output}"
                )
            emit_delegated_outputs(instance.name, child)
            continue
        call = child_function + (
            " " + " ".join(_clash_apply_arg(arg) for arg in args) if args else ""
        )
        result_ports = [p for p in child.inputs if p.protocol is InterfaceProtocol.READY_VALID]
        result_ports += [p for p in child.outputs if p.protocol is InterfaceProtocol.READY_VALID]
        result_ports += [p for p in child.outputs if p.protocol is InterfaceProtocol.WIRE]
        result_count = len(result_ports)
        if result_count == 1:
            result = private_names.child_signal(instance.name, child.outputs[0].name)
            bindings.append(f"{result} = {call}")
        else:
            result = private_names.instance_helper(instance.name, "result")
            bindings.append(f"{result} = {call}")
        def projection(index: int) -> str:
            if result_count == 1:
                return result
            if result_count == 2 and index == 0:
                return f"fst {result}"
            if result_count == 2 and index == 1:
                return f"snd {result}"
            patterns = ["_" for _ in range(result_count)]
            patterns[index] = f"x{index}"
            return f"let ({','.join(patterns)}) = {result} in x{index}"
        for index, port in enumerate(result_ports):
            if port.protocol is InterfaceProtocol.READY_VALID and port.direction is PortDirection.INPUT:
                bindings.append(f"{private_names.child_signal(instance.name, port.name, 'ready')} = {projection(index)}")
        for index, port in enumerate(result_ports):
            if port.protocol is InterfaceProtocol.READY_VALID and port.direction is PortDirection.OUTPUT:
                lhs = private_names.child_signal(instance.name, port.name)
                rhs = projection(index)
                if lhs != rhs:
                    bindings.append(f"{lhs} = {rhs}")
        for index, port in enumerate(result_ports):
            if port.protocol is InterfaceProtocol.WIRE:
                bindings.append(f"{private_names.child_signal(instance.name, port.name)} = {projection(index)}")
        emit_delegated_outputs(instance.name, child)
    # Physical connection equations are intentionally explicit: forward
    # payload/valid travel downstream and ready travels upstream.  A direct
    # buffered edge uses the same FIFO equations as an explicit FIFO child.
    for connection in module.hierarchical_connections:
        if connection.source.channel is not None:
            channel = connection.source.channel.value
            source_name = rr_endpoint_name(connection.source, channel)
            destination_name = rr_endpoint_name(connection.destination, channel)
            depth = (
                connection.request_buffer_depth
                if channel == RequestResponseChannel.REQUEST.value
                else connection.response_buffer_depth
            )
            tracker_info = rr_trackers.get(id(connection))
            tracker = tracker_info[0] if tracker_info else None
            maximum = tracker_info[1] if tracker_info else None
            if depth:
                prefix = f"{source_name}_{destination_name}_buffer"
                bindings.extend(_emit_fifo_bindings(
                    prefix, source_name, connection.source.payload_type,
                    depth, accept_credit=False,
                ))
                if channel == RequestResponseChannel.REQUEST.value and tracker is not None:
                    bindings.extend((
                        f"{source_name}_payload = zlangRvPayload <$> {source_name}",
                        f"{source_name}_valid = zlangRvValid <$> {source_name}",
                        f"{source_name}_ready_bit = (\\count resetActive -> if resetActive || count >= {depth} then low else high) <$> {prefix}_count <*> reset_active",
                        f"{source_name}_ready = ZLangReadyValidBackward <$> {source_name}_ready_bit",
                        f"{prefix}_enqueue = (\\valid ready -> valid .&. ready) <$> {source_name}_valid <*> {source_name}_ready_bit",
                        f"{destination_name}_payload = head <$> {prefix}_slots",
                        f"{destination_name}_valid = (\\count resetActive -> if resetActive || count == 0 then low else high) <$> {prefix}_count <*> reset_active",
                        f"{destination_name}_ready_bit = (\\ready count -> if count >= {maximum} then low else ready) <$> (zlangRvReady <$> {destination_name}_ready) <*> {tracker}",
                        f"{prefix}_dequeue = (\\valid ready -> valid .&. ready) <$> {destination_name}_valid <*> {destination_name}_ready_bit",
                        f"{destination_name} = ZLangReadyValidForward <$> {destination_name}_payload <*> {destination_name}_valid",
                        f"{tracker}_request_accept = {prefix}_dequeue",
                    ))
                elif channel == RequestResponseChannel.RESPONSE.value and tracker is not None:
                    bindings.extend((
                        f"{source_name}_payload = zlangRvPayload <$> {source_name}",
                        f"{source_name}_valid = zlangRvValid <$> {source_name}",
                        f"{source_name}_ready_bit = (\\count outstanding resetActive -> if resetActive || count >= {depth} || outstanding == 0 then low else high) <$> {prefix}_count <*> {tracker} <*> reset_active",
                        f"{source_name}_ready = ZLangReadyValidBackward <$> {source_name}_ready_bit",
                        f"{prefix}_enqueue = (\\valid ready -> valid .&. ready) <$> {source_name}_valid <*> {source_name}_ready_bit",
                        f"{destination_name}_payload = head <$> {prefix}_slots",
                        f"{destination_name}_valid = (\\count resetActive -> if resetActive || count == 0 then low else high) <$> {prefix}_count <*> reset_active",
                        f"{destination_name}_ready_bit = (\\ready outstanding -> if outstanding == 0 then low else ready) <$> (zlangRvReady <$> {destination_name}_ready) <*> {tracker}",
                        f"{prefix}_dequeue = (\\valid ready -> valid .&. ready) <$> {destination_name}_valid <*> {destination_name}_ready_bit",
                        f"{destination_name} = ZLangReadyValidForward <$> {destination_name}_payload <*> {destination_name}_valid",
                        f"{tracker}_response_consume = {prefix}_dequeue",
                    ))
                else:
                    bindings.extend((
                    f"{source_name}_payload = zlangRvPayload <$> {source_name}",
                    f"{source_name}_valid = zlangRvValid <$> {source_name}",
                    f"{source_name}_ready_bit = (\\count resetActive -> if resetActive || count >= {depth} then low else high) <$> {prefix}_count <*> reset_active",
                    f"{source_name}_ready = ZLangReadyValidBackward <$> {source_name}_ready_bit",
                    f"{prefix}_enqueue = (\\valid ready -> valid .&. ready) <$> {source_name}_valid <*> {source_name}_ready_bit",
                    f"{destination_name}_payload = head <$> {prefix}_slots",
                    f"{destination_name}_valid = (\\count resetActive -> if resetActive || count == 0 then low else high) <$> {prefix}_count <*> reset_active",
                    f"{destination_name}_ready_bit = zlangRvReady <$> {destination_name}_ready",
                    f"{prefix}_dequeue = (\\valid ready -> valid .&. ready) <$> {destination_name}_valid <*> {destination_name}_ready_bit",
                    f"{destination_name} = ZLangReadyValidForward <$> {destination_name}_payload <*> {destination_name}_valid",
                    ))
            else:
                if tracker is not None and channel == RequestResponseChannel.REQUEST.value:
                    bindings.extend((
                        f"{source_name}_ready_bit = (\\ready count resetActive -> if resetActive || count >= {maximum} then low else ready) <$> (zlangRvReady <$> {destination_name}_ready) <*> {tracker} <*> reset_active",
                        f"{source_name}_ready = ZLangReadyValidBackward <$> {source_name}_ready_bit",
                        f"{destination_name} = (\\forward count resetActive -> ZLangReadyValidForward (zlangRvPayload forward) (if resetActive || count >= {maximum} then low else zlangRvValid forward)) <$> {source_name} <*> {tracker} <*> reset_active",
                        f"{tracker}_request_accept = (\\valid ready -> valid .&. ready) <$> (zlangRvValid <$> {source_name}) <*> {source_name}_ready_bit",
                    ))
                elif tracker is not None and channel == RequestResponseChannel.RESPONSE.value:
                    bindings.extend((
                        f"{source_name}_ready_bit = (\\ready count requestAccept resetActive -> if resetActive || (count == 0 && requestAccept == low) then low else ready) <$> (zlangRvReady <$> {destination_name}_ready) <*> {tracker} <*> {tracker}_request_accept <*> reset_active",
                        f"{source_name}_ready = ZLangReadyValidBackward <$> {source_name}_ready_bit",
                        f"{destination_name} = (\\forward count requestAccept resetActive -> ZLangReadyValidForward (zlangRvPayload forward) (if resetActive || (count == 0 && requestAccept == low) then low else zlangRvValid forward)) <$> {source_name} <*> {tracker} <*> {tracker}_request_accept <*> reset_active",
                        f"{tracker}_response_consume = (\\valid ready -> valid .&. ready) <$> (zlangRvValid <$> {source_name}) <*> {source_name}_ready_bit",
                    ))
                else:
                    bindings.extend((
                        f"{destination_name} = {source_name}",
                        f"{source_name}_ready = {destination_name}_ready",
                    ))
            continue
        source_name = hierarchy_forward(connection.source)
        destination_name = hierarchy_forward(connection.destination)
        if connection.source.protocol is InterfaceProtocol.WIRE:
            bindings.append(f"{destination_name} = {source_name}")
            continue
        source_ready = hierarchy_backward(connection.source)
        destination_backward = hierarchy_backward(connection.destination)
        destination_ready = f"{destination_name}_ready_bit"
        bindings.append(
            f"{destination_ready} = zlangRvReady <$> {destination_backward}"
        )
        if connection.buffer_depth:
            prefix = f"{source_name}_{destination_name}_buffer"
            bindings.extend(
                _emit_fifo_bindings(
                    prefix,
                    source_name,
                    connection.source.payload_type,
                    connection.buffer_depth,
                    accept_credit=False,
                )
            )
            bindings.extend(
                (
                    f"{source_name}_payload = zlangRvPayload <$> {source_name}",
                    f"{source_name}_valid = zlangRvValid <$> {source_name}",
                    f"{source_ready}_bit = (\\count dequeued resetActive -> if resetActive || (count >= {connection.buffer_depth} && dequeued == low) then low else high) <$> {prefix}_count <*> {prefix}_dequeue <*> reset_active",
                    f"{source_ready} = {destination_backward}",
                    f"{prefix}_enqueue = (\\count valid ready -> if count == 0 then valid .&. ready else low) <$> {prefix}_count <*> {source_name}_valid <*> {source_ready}_bit",
                    f"{destination_name}_payload = head <$> {prefix}_slots",
                    f"{destination_name}_valid = (\\count resetActive -> if resetActive || count == 0 then low else high) <$> {prefix}_count <*> reset_active",
                    f"{prefix}_dequeue = (\\valid ready -> valid .&. ready) <$> {destination_name}_valid <*> {destination_ready}",
                    f"{destination_name} = ZLangReadyValidForward <$> {destination_name}_payload <*> {destination_name}_valid",
                )
            )
        else:
            bindings.append(f"{destination_name} = {source_name}")
            if (
                connection.source.owner == module.name
                and connection.source.direction is PortDirection.INPUT
            ):
                bindings.append(
                    f"{source_ready} = zlangRvReady <$> {destination_backward}"
                )
            else:
                bindings.append(f"{source_ready} = {destination_backward}")
    # Materialize top-owned inputs and direct top assignments before building
    # the flat external signature.  Aggregate leaves are never inferred from
    # child names; all paths come from TopAggregateABI.
    aggregate_ports = {port.name: port for port in module.ports if "__" in port.name}
    for name, value in top_protocol_inputs.items():
        bindings.append(f"{name} = {value}")
        port = aggregate_ports.get(name)
        if port is not None and port.protocol is InterfaceProtocol.READY_VALID:
            # Expression IR addresses ready/valid fields by their canonical
            # synthetic semantic port.  Keep those projections explicit even
            # when the external aggregate ABI flattened the payload members.
            bindings.extend((
                f"{name}_payload = zlangRvPayload <$> {name}",
                f"{name}_valid = zlangRvValid <$> {name}",
            ))
    for name, value in top_protocol_backwards.items():
        bindings.append(f"{name}_backward = {value}")
        port = aggregate_ports.get(name)
        if port is not None and port.protocol is InterfaceProtocol.READY_VALID:
            bindings.append(
                f"{name}_ready = zlangRvReady <$> {name}_backward"
            )

    for leaf in top_abi.outputs:
        if any(binding.startswith(f"{_top_leaf_signal(leaf)} =") for binding in bindings):
            continue
        endpoint_name, member_name = leaf.member_path[:2]
        port_name = f"{endpoint_name}__{member_name}"
        endpoint = next(item for item in module.aggregate_protocol_endpoints if item.name == endpoint_name)
        member = next(item for item in endpoint.members if item.name == member_name)
        if (
            unified_state
            and leaf.signal_kind == "wire"
            and port_name in scheduled_wire_outputs
        ):
            bindings.append(
                f"{_top_leaf_signal(leaf)} = {_clash_name(port_name)}"
            )
            continue
        assignment = next(
            (item for item in module.assignments
             if item.target.name == port_name and item.signal is (
                 ReadyValidSignal.PAYLOAD if leaf.signal_kind == "payload" else
                 ReadyValidSignal.VALID if leaf.signal_kind == "valid" else
                 ReadyValidSignal.READY if leaf.signal_kind == "ready" else None
             )),
            None,
        )
        if assignment is None and leaf.signal_kind == "wire":
            assignment = next((item for item in module.assignments if item.target.name == port_name and item.signal is None), None)
        if assignment is None:
            raise ClashEmissionError(f"top aggregate output '{leaf.leaf_semantic_id}' is not driven")
        rendered = render_state(assignment.expression)
        if leaf.signal_kind == "payload":
            rendered = _top_payload_projection(rendered, member.payload_type, leaf.member_path[3:])
        bindings.append(f"{_top_leaf_signal(leaf)} = {rendered}")

    ordinary_inputs = tuple(port for port in module.inputs if "__" not in port.name)
    ordinary_wire_inputs = tuple(
        port for port in ordinary_inputs
        if port.protocol is InterfaceProtocol.WIRE
    )
    ordinary_rv_inputs = tuple(
        port for port in ordinary_inputs
        if port.protocol is InterfaceProtocol.READY_VALID
    )
    ordinary_outputs = tuple(port for port in module.outputs if "__" not in port.name)
    output_values: list[str] = [
        f"{_clash_name(port.name)}_ready" for port in ordinary_rv_inputs
    ]
    output_types: list[str] = [
        "Signal ZLangSystem Bit" for _ in ordinary_rv_inputs
    ]
    output_annotations: list[str] = [
        f'PortName "{port.name}_ready"' for port in ordinary_rv_inputs
    ]
    protocol_output_arguments: list[str] = []
    protocol_output_signatures: list[str] = []
    for output in ordinary_outputs:
        if output.protocol is InterfaceProtocol.READY_VALID:
            output_name = _clash_name(output.name)
            bindings.append(
                f"{output_name}_ready = "
                f"zlangRvReady <$> {output_name}_backward"
            )
            protocol_output_arguments.append(f"{output_name}_backward")
            protocol_output_signatures.append("Signal ZLangSystem ZLangReadyValidBackward")
    for output in ordinary_outputs:
        output_name = _clash_name(output.name)
        if output.protocol is InterfaceProtocol.READY_VALID:
            output_values.append(output_name)
            output_types.append(f"Signal ZLangSystem (ZLangReadyValidForward ({_emit_type(output.type)}))")
            output_annotations.append(
                _top_forward_port_annotation(output.name, output.type, "valid")
            )
            continue
        if unified_state and output.name in scheduled_wire_outputs:
            output_values.append(output_name)
            output_types.append(
                f"Signal ZLangSystem ({_emit_type(output.type)})"
            )
            output_annotations.append(
                _top_value_port_annotation(output.name, output.type)
            )
            continue
        assignment = next((item for item in module.assignments if item.target.name == output.name), None)
        if assignment is None:
            raise ClashEmissionError(f"top scalar output '{output.name}' is not driven")
        bindings.append(
            f"{output_name} = "
            f"{render_state(assignment.expression)}"
        )
        output_values.append(output_name)
        output_types.append(f"Signal ZLangSystem ({_emit_type(output.type)})")
        output_annotations.append(
            _top_value_port_annotation(output.name, output.type)
        )
    for leaf in top_abi.outputs:
        output_values.append(_top_leaf_signal(leaf))
        output_types.append(f"Signal ZLangSystem ({_emit_type(leaf.canonical_type)})")
        output_annotations.append(f'PortName "{leaf.external_name}"')
    if not output_values:
        raise ClashEmissionError("top aggregate hierarchy has no externally visible outputs")
    aggregate_input_leaves = top_abi.inputs
    input_signature = [
        f"Signal ZLangSystem ({_emit_type(port.type)})"
        for port in ordinary_wire_inputs
    ]
    input_signature += [
        f"Signal ZLangSystem (ZLangReadyValidForward ({_emit_type(port.type)}))"
        for port in ordinary_rv_inputs
    ]
    input_signature += [f"Signal ZLangSystem ({_emit_type(leaf.canonical_type)})" for leaf in aggregate_input_leaves]
    input_signature += protocol_output_signatures
    output_type = _emit_product(output_types)
    output_value = _emit_product(output_values)
    circuit_signature = " -> ".join((*input_signature, output_type))
    aggregate_arguments = [_top_leaf_signal(leaf) for leaf in aggregate_input_leaves]
    circuit_arguments = " ".join((
        *(f"parent_{_clash_name(p.name)}" for p in ordinary_wire_inputs),
        *(f"parent_{_clash_name(p.name)}" for p in ordinary_rv_inputs),
        *aggregate_arguments,
        *protocol_output_arguments,
    ))
    top_arguments = " ".join((
        module.clock,
        module.reset,
        *(f"parent_{_clash_name(p.name)}" for p in ordinary_wire_inputs),
        *(f"parent_{_clash_name(p.name)}" for p in ordinary_rv_inputs),
        *aggregate_arguments,
        *protocol_output_arguments,
    ))
    input_annotations = [
        f'PortName "{module.clock}"',
        f'PortName "{module.reset}"',
        *(
            _top_value_port_annotation(port.name, port.type)
            for port in ordinary_wire_inputs
        ),
    ]
    input_annotations += [
        _top_forward_port_annotation(port.name, port.type, "valid")
        for port in ordinary_rv_inputs
    ]
    input_annotations += [f'PortName "{leaf.external_name}"' for leaf in aggregate_input_leaves]
    input_annotations += [f'PortName "{output.name}_ready"' for output in ordinary_outputs if output.protocol is InterfaceProtocol.READY_VALID]
    output_annotation = output_annotations[0] if len(output_annotations) == 1 else f'PortProduct "" [{", ".join(output_annotations)}]'
    where_block = "\n".join(f"  {b}" for b in bindings)
    return f"""{extensions}
module {module.name} where

{imports}
{declarations}{child_text}
circuit :: HiddenClockResetEnable ZLangSystem => {circuit_signature}
circuit {circuit_arguments} = {output_value}
 where
{where_block}

topEntity :: Clock ZLangSystem -> Reset ZLangSystem -> {circuit_signature}
topEntity {top_arguments} = exposeClockResetEnable circuit {module.clock} {_top_reset_expression(module)} enableGen {circuit_arguments}

{{-# ANN topEntity
  (Synthesize
    {{ t_name = "{module.name}"
    , t_inputs = [{', '.join(input_annotations)}]
    , t_output = {output_annotation}
    }}) #-}}
"""


def _connection_assignment_keys(connection: object) -> tuple[tuple[str, object], ...]:
    source = connection.source
    destination = connection.destination
    if source.protocol is InterfaceProtocol.WIRE:
        return ((destination.name, None),)
    source_signal = (
        ReadyValidSignal.READY
        if source.protocol is InterfaceProtocol.READY_VALID
        else CreditSignal.RETURN
    )
    destination_signals = (
        (ReadyValidSignal.PAYLOAD, ReadyValidSignal.VALID)
        if destination.protocol is InterfaceProtocol.READY_VALID
        else (CreditSignal.PAYLOAD, CreditSignal.SEND)
    )
    return (
        (source.name, source_signal),
        *((destination.name, signal) for signal in destination_signals),
    )


def _emit_connection_bindings(connection: object) -> tuple[str, ...]:
    source = connection.source
    destination = connection.destination
    if connection.adapter is ConnectionAdapter.READY_VALID_TO_CREDIT:
        if destination.capacity is None:
            raise ClashEmissionError(
                f"credit interface '{destination.name}' has no capacity"
            )
        width = max(1, destination.capacity.bit_length())
        count_type = f"Unsigned {width}"
        initial = f"({destination.capacity} :: {count_type})"
        return (
            f"{destination.name}_credits = register {initial} {destination.name}_credits_next",
            f"{source.name}_ready = (\\credits resetActive -> if resetActive || credits == 0 then low else high) <$> {destination.name}_credits <*> reset_active",
            f"{destination.name}_payload = {source.name}_payload",
            f"{destination.name}_send = (\\valid ready -> valid .&. ready) <$> {source.name}_valid <*> {source.name}_ready",
            f"{destination.name}_credits_next = (\\credits sent returned -> case (sent == high, returned == high) of {{ (True, False) -> credits - 1; (False, True) -> if credits < {initial} then credits + 1 else credits; _ -> credits }}) <$> {destination.name}_credits <*> {destination.name}_send <*> {destination.name}_return",
        )
    if connection.adapter is ConnectionAdapter.CREDIT_TO_READY_VALID:
        prefix = f"{source.name}_{destination.name}_buffer"
        bindings = list(
            _emit_fifo_bindings(
                prefix,
                source.name,
                source.type,
                connection.buffer_depth,
                accept_credit=True,
            )
        )
        bindings.extend(
            (
                f"{destination.name}_payload = head <$> {prefix}_slots",
                f"{destination.name}_valid = (\\count resetActive -> if resetActive || count == 0 then low else high) <$> {prefix}_count <*> reset_active",
                f"{prefix}_dequeue = (\\valid ready -> valid .&. ready) <$> {destination.name}_valid <*> {destination.name}_ready",
                f"{source.name}_return = {prefix}_dequeue",
            )
        )
        return tuple(bindings)
    if not connection.buffer_depth:
        if source.protocol is InterfaceProtocol.WIRE:
            return (f"{destination.name} = {source.name}",)
        if source.protocol is InterfaceProtocol.READY_VALID:
            return (
                f"{destination.name}_payload = {source.name}_payload",
                f"{destination.name}_valid = {source.name}_valid",
                f"{source.name}_ready = {destination.name}_ready",
            )
        return (
            f"{destination.name}_payload = {source.name}_payload",
            f"{destination.name}_send = {source.name}_send",
            f"{source.name}_return = {destination.name}_return",
        )

    prefix = f"{source.name}_{destination.name}_buffer"
    bindings = list(
        _emit_fifo_bindings(
            prefix,
            source.name,
            source.type,
            connection.buffer_depth,
            accept_credit=False,
        )
    )
    bindings.extend(
        (
            f"{source.name}_ready = (\\count dequeued resetActive -> if resetActive || (count >= {connection.buffer_depth} && dequeued == low) then low else high) <$> {prefix}_count <*> {prefix}_dequeue <*> reset_active",
            f"{prefix}_enqueue = (\\valid ready -> valid .&. ready) <$> {source.name}_valid <*> {source.name}_ready",
            f"{destination.name}_payload = head <$> {prefix}_slots",
            f"{destination.name}_valid = (\\count resetActive -> if resetActive || count == 0 then low else high) <$> {prefix}_count <*> reset_active",
            f"{prefix}_dequeue = (\\valid ready -> valid .&. ready) <$> {destination.name}_valid <*> {destination.name}_ready",
        )
    )
    return tuple(bindings)


def _emit_fifo_bindings(
    prefix: str,
    source_name: str,
    payload_type: HardwareType,
    depth: int,
    *,
    accept_credit: bool,
) -> tuple[str, ...]:
    width = max(1, depth.bit_length())
    count_type = f"Unsigned {width}"
    if accept_credit:
        enqueue = (
            f"{prefix}_enqueue = (\\sent count dequeued resetActive -> if not resetActive && sent == high && (count < {depth} || dequeued == high) then high else low) "
            f"<$> {source_name}_send <*> {prefix}_count <*> {prefix}_dequeue <*> reset_active"
        )
    else:
        enqueue = ""
    common = (
        f"{prefix}_count = register (0 :: {count_type}) {prefix}_count_next",
        f"{prefix}_slots = register (repeat (deepErrorX \"empty connection buffer\") :: Vec {depth} ({_emit_type(payload_type)})) {prefix}_slots_next",
        enqueue,
        f"{prefix}_count_next = (\\count enqueued dequeued -> case (enqueued == high, dequeued == high) of {{ (True, False) -> count + 1; (False, True) -> count - 1; _ -> count }}) <$> {prefix}_count <*> {prefix}_enqueue <*> {prefix}_dequeue",
        f"{prefix}_slots_next = (\\slots count enqueued dequeued payload -> case (enqueued == high, dequeued == high) of {{ (True, False) -> replace (fromIntegral count) payload slots; (False, True) -> slots <<+ deepErrorX \"empty connection buffer\"; (True, True) -> replace (fromIntegral (count - 1)) payload (slots <<+ deepErrorX \"empty connection buffer\"); _ -> slots }}) <$> {prefix}_slots <*> {prefix}_count <*> {prefix}_enqueue <*> {prefix}_dequeue <*> {source_name}_payload",
    )
    return tuple(binding for binding in common if binding)


def _emit_vc_credit_module(module: Module) -> str:
    """Emit independent sender/receiver state for every virtual channel."""

    if module.clock is None or module.reset is None:
        raise ClashEmissionError(
            "virtual-channel credit interfaces require one clock and reset"
        )
    if (
        module.arbiters
        or module.connections
        or module.registers
        or module.rules
        or module.fifos
        or module.memories
        or module.csr_blocks
        or module.request_responses
    ):
        raise ClashEmissionError(
            "virtual-channel credit modules cannot yet mix other stateful forms"
        )
    if any(
        port.protocol not in {InterfaceProtocol.WIRE, InterfaceProtocol.VC_CREDIT}
        for port in module.ports
    ):
        raise ClashEmissionError(
            "virtual-channel credit modules cannot mix other protocols"
        )
    staged: dict[int, expr.Delay | expr.Pipeline] = {}
    for assignment in module.assignments:
        _collect_delays(assignment.expression, staged)
    if staged:
        raise ClashEmissionError(
            "staged virtual-channel credit expressions are not implemented"
        )

    extensions, imports, declarations = _emit_prelude(module)
    if "{-# LANGUAGE DeriveAnyClass #-}" not in extensions:
        extensions = extensions.replace(
            "{-# LANGUAGE NoImplicitPrelude #-}",
            "{-# LANGUAGE DeriveAnyClass #-}\n"
            "{-# LANGUAGE DeriveGeneric #-}\n"
            "{-# LANGUAGE NoImplicitPrelude #-}",
        )
    extensions = extensions.replace(
        "{-# LANGUAGE NoImplicitPrelude #-}",
        "{-# LANGUAGE OverloadedStrings #-}\n"
        "{-# LANGUAGE TemplateHaskell #-}\n"
        "{-# LANGUAGE NoImplicitPrelude #-}",
    )
    if "import GHC.Generics (Generic)" not in imports:
        imports += "import GHC.Generics (Generic)\n"
    imports += "import qualified Clash.Verification as Verification\n"
    declarations = _vc_credit_declarations() + declarations
    domain = "ZLangSystem"
    input_types: list[str] = []
    input_names: list[str] = []
    input_annotations = [
        f'PortName "{module.clock}"',
        f'PortName "{module.reset}"',
    ]
    output_types: list[str] = []
    output_values: list[str] = []
    output_annotations: list[str] = []
    bindings: list[str] = ["reset_active = unsafeToActiveHigh hasReset"]

    for port in module.ports:
        payload_type = _emit_type(port.type)
        if port.protocol is InterfaceProtocol.WIRE:
            if port.direction is PortDirection.INPUT:
                input_types.append(f"Signal {domain} ({payload_type})")
                input_names.append(port.name)
                input_annotations.append(
                    _top_value_port_annotation(port.name, port.type)
                )
            else:
                output_types.append(f"Signal {domain} ({payload_type})")
                output_values.append(port.name)
                output_annotations.append(
                    _top_value_port_annotation(port.name, port.type)
                )
            continue
        if port.capacity is None or port.virtual_channels is None:
            raise ClashEmissionError(
                f"virtual-channel credit interface '{port.name}' has no bounds"
            )
        vc_width = max(1, (port.virtual_channels - 1).bit_length())
        vc_type = f"Unsigned {vc_width}"
        if port.direction is PortDirection.INPUT:
            input_types.append(
                f"Signal {domain} (ZLangVcCreditForward ({vc_type}) ({payload_type}))"
            )
            input_names.append(port.name)
            input_annotations.append(
                _top_forward_port_annotation(
                    port.name, port.type, "vc", "send"
                )
            )
            bindings.extend(
                (
                    f"{port.name}_payload = zlangVcCreditPayload <$> {port.name}",
                    f"{port.name}_vc = zlangVcCreditVc <$> {port.name}",
                    f"{port.name}_send = zlangVcCreditSend <$> {port.name}",
                )
            )
            output_types.append(
                f"Signal {domain} (ZLangVcCreditReturn ({vc_type}))"
            )
            output_values.append(
                f"ZLangVcCreditReturn <$> {port.name}_return_checked <*> {port.name}_return_vc"
            )
            output_annotations.append(
                f'PortProduct "{port.name}" [PortName "return", PortName "return_vc"]'
            )
        else:
            input_name = f"{port.name}_return_input"
            input_types.append(
                f"Signal {domain} (ZLangVcCreditReturn ({vc_type}))"
            )
            input_names.append(input_name)
            input_annotations.append(
                f'PortProduct "{port.name}" [PortName "return", PortName "return_vc"]'
            )
            bindings.extend(
                (
                    f"{port.name}_return = zlangVcCreditReturnPulse <$> {input_name}",
                    f"{port.name}_return_vc = zlangVcCreditReturnVc <$> {input_name}",
                )
            )
            output_types.append(
                f"Signal {domain} (ZLangVcCreditForward ({vc_type}) ({payload_type}))"
            )
            output_values.append(
                f"ZLangVcCreditForward <$> {port.name}_payload <*> {port.name}_vc <*> {port.name}_send_checked"
            )
            output_annotations.append(
                _top_forward_port_annotation(
                    port.name, port.type, "vc", "send"
                )
            )

    for assignment in module.assignments:
        bindings.append(
            f"{_vc_credit_assignment_name(assignment)} = "
            f"{_emit_signal_expression(assignment.expression, {})}"
        )
    for port in module.ports:
        if port.protocol is not InterfaceProtocol.VC_CREDIT:
            continue
        assert port.capacity is not None and port.virtual_channels is not None
        if port.direction is PortDirection.OUTPUT:
            bindings.extend(
                _emit_vc_credit_sender_bindings(
                    port.name, port.virtual_channels, port.capacity
                )
            )
        else:
            bindings.extend(
                _emit_vc_credit_receiver_bindings(
                    port.name, port.virtual_channels, port.capacity
                )
            )

    circuit_signature = " -> ".join(
        (*input_types, _emit_product(output_types))
    )
    top_signature = " -> ".join(
        (
            f"Clock {domain}",
            f"Reset {domain}",
            *input_types,
            _emit_product(output_types),
        )
    )
    arguments = " ".join(input_names)
    circuit_lhs = f"circuit {arguments}" if arguments else "circuit"
    application = f" {arguments}" if arguments else ""
    result = _emit_product(output_values)
    where_block = "\n".join(f"  {binding}" for binding in bindings)
    output_annotation = (
        output_annotations[0]
        if len(output_annotations) == 1
        else f'PortProduct "" [{", ".join(output_annotations)}]'
    )

    return f'''{extensions}module {module.name} where

{imports}
{declarations}{_domain_declaration(module, domain)}

circuit :: HiddenClockResetEnable {domain} => {circuit_signature}
{circuit_lhs} = {result}
 where
{where_block}

topEntity :: {top_signature}
topEntity {module.clock} {module.reset}{application} = exposeClockResetEnable circuit {module.clock} {_top_reset_expression(module)} enableGen{application}

{{-# ANN topEntity
  (Synthesize
    {{ t_name = "{module.name}"
    , t_inputs = [{", ".join(input_annotations)}]
    , t_output = {output_annotation}
    }}) #-}}
'''


def _vc_credit_declarations() -> str:
    return '''data ZLangVcCreditForward vc a = ZLangVcCreditForward
  { zlangVcCreditPayload :: a
  , zlangVcCreditVc :: vc
  , zlangVcCreditSend :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

data ZLangVcCreditReturn vc = ZLangVcCreditReturn
  { zlangVcCreditReturnPulse :: Bit
  , zlangVcCreditReturnVc :: vc
  } deriving (Generic, NFDataX, Show, Eq)

'''


def _emit_vc_credit_sender_bindings(
    name: str, virtual_channels: int, capacity: int
) -> tuple[str, ...]:
    width = max(1, capacity.bit_length())
    count_type = f"Unsigned {width}"
    initial = f"({capacity} :: {count_type})"
    counters = [f"{name}_credits_{index}" for index in range(virtual_channels)]
    parameters = [f"credits_{index}" for index in range(virtual_channels)]
    cases = "; ".join(
        f"{index} -> {parameters[index]} > 0"
        for index in range(virtual_channels)
    )
    bindings = [
        *(
            f"{counter} = register {initial} {counter}_next"
            for counter in counters
        ),
        f"{name}_credits = bundle ({' :> '.join(counters)} :> Nil)",
        f"{name}_can_send = (\\vc {' '.join(parameters)} -> case vc of {{ {cases}; _ -> False }}) <$> {name}_vc"
        + "".join(f" <*> {counter}" for counter in counters),
        f"{name}_send = (\\requested allowed resetActive -> if not resetActive && requested == high && allowed then high else low) <$> {name}_send_request <*> {name}_can_send <*> reset_active",
        f"{name}_transfer = {name}_send",
    ]
    for index, counter in enumerate(counters):
        bindings.extend(
            (
                f"{name}_sent_{index} = (\\sent vc -> sent == high && vc == {index}) <$> {name}_send <*> {name}_vc",
                f"{name}_returned_{index} = (\\returned vc -> returned == high && vc == {index}) <$> {name}_return <*> {name}_return_vc",
                f"{name}_no_overflow_{index} = (\\returned sent credits resetActive -> resetActive || not returned || sent || credits < {initial}) <$> {name}_returned_{index} <*> {name}_sent_{index} <*> {counter} <*> reset_active",
                f"{counter}_next = (\\credits sent returned -> case (sent, returned) of {{ (True, False) -> if credits > 0 then credits - 1 else credits; (False, True) -> if credits < {initial} then credits + 1 else credits; _ -> credits }}) <$> {counter} <*> {name}_sent_{index} <*> {name}_returned_{index}",
            )
        )
    checked = f"{name}_send"
    for index in reversed(range(virtual_channels)):
        checked = (
            f'Verification.checkI "{name}_vc_{index}_no_overflow" '
            f"Verification.AutoRenderAs (Verification.assert "
            f"{name}_no_overflow_{index}) $ {checked}"
        )
    bindings.append(f"{name}_send_checked = {checked}")
    return tuple(bindings)


def _emit_vc_credit_receiver_bindings(
    name: str, virtual_channels: int, capacity: int
) -> tuple[str, ...]:
    width = max(1, capacity.bit_length())
    count_type = f"Unsigned {width}"
    maximum = f"({capacity} :: {count_type})"
    counters = [f"{name}_occupancy_{index}" for index in range(virtual_channels)]
    bindings = [
        *(
            f"{counter} = register (0 :: {count_type}) {counter}_next"
            for counter in counters
        ),
        f"{name}_return = (\\requested resetActive -> if resetActive then low else requested) <$> {name}_return_request <*> reset_active",
        f"{name}_transfer = (\\sent resetActive -> if resetActive then low else sent) <$> {name}_send <*> reset_active",
    ]
    for index, counter in enumerate(counters):
        bindings.extend(
            (
                f"{name}_sent_{index} = (\\sent vc -> sent == high && vc == {index}) <$> {name}_transfer <*> {name}_vc",
                f"{name}_returned_{index} = (\\returned vc -> returned == high && vc == {index}) <$> {name}_return <*> {name}_return_vc",
                f"{name}_no_underflow_{index} = (\\returned sent occupancy resetActive -> resetActive || not returned || sent || occupancy > 0) <$> {name}_returned_{index} <*> {name}_sent_{index} <*> {counter} <*> reset_active",
                f"{name}_no_overflow_{index} = (\\sent returned occupancy resetActive -> resetActive || not sent || returned || occupancy < {maximum}) <$> {name}_sent_{index} <*> {name}_returned_{index} <*> {counter} <*> reset_active",
                f"{counter}_next = (\\occupancy sent returned -> case (sent, returned) of {{ (True, False) -> if occupancy < {maximum} then occupancy + 1 else occupancy; (False, True) -> if occupancy > 0 then occupancy - 1 else occupancy; _ -> occupancy }}) <$> {counter} <*> {name}_sent_{index} <*> {name}_returned_{index}",
            )
        )
    checked = f"{name}_return"
    for index in reversed(range(virtual_channels)):
        checked = (
            f'Verification.checkI "{name}_vc_{index}_no_underflow" '
            f"Verification.AutoRenderAs (Verification.assert "
            f"{name}_no_underflow_{index}) . "
            f'Verification.checkI "{name}_vc_{index}_no_overflow" '
            f"Verification.AutoRenderAs (Verification.assert "
            f"{name}_no_overflow_{index}) $ {checked}"
        )
    bindings.append(f"{name}_return_checked = {checked}")
    return tuple(bindings)


def _vc_credit_assignment_name(assignment: object) -> str:
    signal = assignment.signal
    suffix = signal.value if signal is not None else ""
    if signal in {
        VirtualChannelCreditSignal.SEND,
        VirtualChannelCreditSignal.RETURN,
    }:
        suffix += "_request"
    return assignment.target.name if signal is None else f"{assignment.target.name}_{suffix}"


def _emit_credit_module(
    module: Module,
    *,
    formal_observations: tuple[tuple[Port, str, str], ...] = (),
) -> str:
    if not module.is_sequential or module.clock is None or module.reset is None:
        raise ClashEmissionError("credit interfaces require a module clock and reset")
    if module.registers or module.next_assignments:
        raise ClashEmissionError(
            "credit interface modules with user registers are not implemented"
        )
    staged: dict[int, expr.Delay | expr.Pipeline] = {}
    for assignment in module.assignments:
        _collect_delays(assignment.expression, staged)
    if staged:
        raise ClashEmissionError(
            "delay and pipeline expressions in credit interface modules are not implemented"
        )

    extensions, imports, declarations = _emit_prelude(module)
    if "{-# LANGUAGE DeriveAnyClass #-}" not in extensions:
        extensions = extensions.replace(
            "{-# LANGUAGE NoImplicitPrelude #-}",
            "{-# LANGUAGE DeriveAnyClass #-}\n"
            "{-# LANGUAGE DeriveGeneric #-}\n"
            "{-# LANGUAGE NoImplicitPrelude #-}",
        )
    extensions = extensions.replace(
        "{-# LANGUAGE NoImplicitPrelude #-}",
        "{-# LANGUAGE OverloadedStrings #-}\n"
        "{-# LANGUAGE TemplateHaskell #-}\n"
        "{-# LANGUAGE NoImplicitPrelude #-}",
    )
    if "import GHC.Generics (Generic)" not in imports:
        imports += "import GHC.Generics (Generic)\n"
    imports += "import qualified Clash.Verification as Verification\n"
    declarations = _credit_declarations() + declarations
    domain = "ZLangSystem"

    input_types: list[str] = []
    input_names: list[str] = []
    input_annotations: list[str] = [
        f'PortName "{module.clock}"',
        f'PortName "{module.reset}"',
    ]
    output_types: list[str] = []
    output_values: list[str] = []
    output_annotations: list[str] = []
    bindings: list[str] = ["reset_active = unsafeToActiveHigh hasReset"]

    for port in module.ports:
        type_ = _emit_type(port.type)
        if port.protocol is InterfaceProtocol.WIRE:
            if port.direction is PortDirection.INPUT:
                input_types.append(f"Signal {domain} ({type_})")
                input_names.append(_clash_name(port.name))
                input_annotations.append(
                    _top_value_port_annotation(port.name, port.type)
                )
            else:
                output_types.append(f"Signal {domain} ({type_})")
                output_values.append(_clash_name(port.name))
                output_annotations.append(
                    _top_value_port_annotation(port.name, port.type)
                )
            continue

        if port.capacity is None:
            raise ClashEmissionError(
                f"credit interface '{port.name}' has no capacity"
            )
        if port.direction is PortDirection.INPUT:
            input_types.append(f"Signal {domain} (ZLangCreditForward ({type_}))")
            input_names.append(port.name)
            input_annotations.append(
                _top_forward_port_annotation(port.name, port.type, "send")
            )
            bindings.extend(
                (
                    f"{port.name}_payload = zlangCreditPayload <$> {port.name}",
                    f"{port.name}_send = zlangCreditSend <$> {port.name}",
                )
            )
            output_types.append(f"Signal {domain} ZLangCreditReturn")
            output_values.append(
                f"ZLangCreditReturn <$> {port.name}_return_checked"
            )
            output_annotations.append(f'PortName "{port.name}_return"')
        else:
            input_types.append(f"Signal {domain} ZLangCreditReturn")
            input_name = f"{port.name}_return_input"
            input_names.append(input_name)
            input_annotations.append(f'PortName "{port.name}_return"')
            bindings.append(
                f"{port.name}_return = zlangCreditReturnPulse <$> {input_name}"
            )
            output_types.append(
                f"Signal {domain} (ZLangCreditForward ({type_}))"
            )
            output_values.append(
                f"ZLangCreditForward <$> {port.name}_payload "
                f"<*> {port.name}_send_checked"
            )
            output_annotations.append(
                _top_forward_port_annotation(port.name, port.type, "send")
            )

    for assignment in module.assignments:
        bindings.append(
            f"{_credit_assignment_name(assignment)} = "
            f"{_emit_signal_expression(assignment.expression, {})}"
        )

    for port in module.ports:
        if port.protocol is not InterfaceProtocol.CREDIT:
            continue
        if port.capacity is None:
            raise ClashEmissionError(
                f"credit interface '{port.name}' has no capacity"
            )
        if port.direction is PortDirection.OUTPUT:
            bindings.extend(_emit_credit_sender_bindings(port.name, port.capacity))
        else:
            bindings.extend(_emit_credit_receiver_bindings(port.name, port.capacity))

    # Formal-only emission extends the ordinary closed credit component with
    # explicit typed observation leaves. Each projection carries the exact
    # semantic Port selected by recursive elaboration; no generated RTL name
    # is inspected to discover either ownership or meaning.
    for port, signal, token in formal_observations:
        if port not in module.ports or port.protocol is not InterfaceProtocol.CREDIT:
            raise ClashEmissionError(
                "credit formal observation does not name a typed module endpoint"
            )
        if port.direction is not PortDirection.INPUT:
            raise ClashEmissionError(
                f"credit formal receiver observation '{port.name}.{signal}' "
                "does not name a receiver endpoint"
            )
        if signal == "occupancy":
            if port.capacity is None:
                raise ClashEmissionError(
                    f"credit interface '{port.name}' has no capacity"
                )
            observation_type = f"Unsigned {max(1, port.capacity.bit_length())}"
        elif signal in {CreditSignal.SEND.value, CreditSignal.RETURN.value}:
            observation_type = "Bit"
        else:
            raise ClashEmissionError(
                f"unsupported credit receiver formal observation '{signal}'"
            )
        output_types.append(f"Signal {domain} ({observation_type})")
        output_values.append(f"{port.name}_{signal}")
        output_annotations.append(f'PortName "{token}"')

    output_type = _emit_product(output_types)
    result = _emit_product(output_values)
    circuit_signature = " -> ".join((*input_types, output_type))
    top_signature = " -> ".join(
        (
            f"Clock {domain}",
            f"Reset {domain}",
            *input_types,
            output_type,
        )
    )
    circuit_arguments = " ".join(input_names)
    circuit_lhs = f"circuit {circuit_arguments}" if circuit_arguments else "circuit"
    top_arguments = " ".join((module.clock, module.reset, *input_names))
    application = f" {circuit_arguments}" if circuit_arguments else ""
    where_block = "\n".join(f"  {binding}" for binding in bindings)
    input_ports = ", ".join(input_annotations)
    output_port = (
        output_annotations[0]
        if len(output_annotations) == 1
        else f'PortProduct "" [{", ".join(output_annotations)}]'
    )

    top_name = f"{module.name}_formal" if formal_observations else module.name
    return f'''{extensions}
module {module.name} where

{imports}
{declarations}{_domain_declaration(module, domain)}

circuit :: HiddenClockResetEnable {domain} => {circuit_signature}
{circuit_lhs} = {result}
 where
{where_block}

topEntity :: {top_signature}
topEntity {top_arguments} = exposeClockResetEnable circuit {module.clock} {_top_reset_expression(module)} enableGen{application}

{{-# ANN topEntity
  (Synthesize
    {{ t_name = "{top_name}"
    , t_inputs = [{input_ports}]
    , t_output = {output_port}
    }}) #-}}
'''


def _credit_declarations() -> str:
    return '''data ZLangCreditForward a = ZLangCreditForward
  { zlangCreditPayload :: a
  , zlangCreditSend :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

data ZLangCreditReturn = ZLangCreditReturn
  { zlangCreditReturnPulse :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

'''


def _emit_credit_sender_bindings(name: str, capacity: int) -> tuple[str, ...]:
    width = max(1, capacity.bit_length())
    count_type = f"Unsigned {width}"
    initial = f"({capacity} :: {count_type})"
    return (
        f"{name}_credits = register {initial} {name}_credits_next",
        f"{name}_send = (\\request credits resetActive -> if resetActive || credits == 0 then low else request) <$> {name}_send_request <*> {name}_credits <*> reset_active",
        f"{name}_transfer = {name}_send",
        f"{name}_no_underflow = (\\sent credits -> sent == low || credits > 0) <$> {name}_send <*> {name}_credits",
        f"{name}_no_overflow = (\\returned sent credits resetActive -> resetActive || returned == low || sent == high || credits < {initial}) <$> {name}_return <*> {name}_send <*> {name}_credits <*> reset_active",
        f"{name}_credits_next = (\\credits sent returned -> case (sent == high, returned == high) of {{ (True, False) -> credits - 1; (False, True) -> if credits < {initial} then credits + 1 else credits; _ -> credits }}) <$> {name}_credits <*> {name}_send <*> {name}_return",
        f"{name}_send_checked = Verification.checkI \"{name}_no_underflow\" Verification.AutoRenderAs (Verification.assert {name}_no_underflow) . Verification.checkI \"{name}_no_overflow\" Verification.AutoRenderAs (Verification.assert {name}_no_overflow) $ {name}_send",
    )


def _emit_credit_receiver_bindings(name: str, capacity: int) -> tuple[str, ...]:
    width = max(1, capacity.bit_length())
    count_type = f"Unsigned {width}"
    maximum = f"({capacity} :: {count_type})"
    zero = f"(0 :: {count_type})"
    return (
        f"{name}_occupancy = register {zero} {name}_occupancy_next",
        f"{name}_return = (\\requested resetActive -> if resetActive then low else requested) <$> {name}_return_request <*> reset_active",
        f"{name}_transfer = (\\sent resetActive -> if resetActive then low else sent) <$> {name}_send <*> reset_active",
        f"{name}_no_underflow = (\\returned sent occupancy resetActive -> resetActive || returned == low || sent == high || occupancy > 0) <$> {name}_return <*> {name}_send <*> {name}_occupancy <*> reset_active",
        f"{name}_no_overflow = (\\sent returned occupancy resetActive -> resetActive || sent == low || returned == high || occupancy < {maximum}) <$> {name}_send <*> {name}_return <*> {name}_occupancy <*> reset_active",
        f"{name}_occupancy_next = (\\occupancy sent returned -> case (sent == high, returned == high) of {{ (True, False) -> if occupancy < {maximum} then occupancy + 1 else occupancy; (False, True) -> if occupancy > 0 then occupancy - 1 else occupancy; _ -> occupancy }}) <$> {name}_occupancy <*> {name}_send <*> {name}_return",
        f"{name}_return_checked = Verification.checkI \"{name}_no_underflow\" Verification.AutoRenderAs (Verification.assert {name}_no_underflow) . Verification.checkI \"{name}_no_overflow\" Verification.AutoRenderAs (Verification.assert {name}_no_overflow) $ {name}_return",
    )


def _credit_assignment_name(assignment: object) -> str:
    target = assignment.target
    signal = assignment.signal
    suffix = signal.value if signal is not None else ""
    if signal in {CreditSignal.SEND, CreditSignal.RETURN}:
        suffix += "_request"
    return target.name if signal is None else f"{target.name}_{suffix}"


def _emit_request_response_module(module: Module) -> str:
    private_names = _clash_module_names(module)
    if not module.is_sequential or module.clock is None or module.reset is None:
        raise ClashEmissionError(
            "request/response interfaces require a module clock and reset"
        )
    staged: dict[int, expr.Delay | expr.Pipeline] = {}
    for assignment in (*module.assignments, *module.next_assignments):
        _collect_delays(assignment.expression, staged)
    for local in module.locals:
        _collect_delays(local.expression, staged)
    for rule in module.rules:
        _collect_delays(rule.guard, staged)
        for action in rule.actions:
            _collect_delays(action.expression, staged)
            if action.activation is not None:
                _collect_delays(action.activation, staged)

    extensions, imports, declarations = _emit_prelude(module)
    if "{-# LANGUAGE DeriveAnyClass #-}" not in extensions:
        extensions = extensions.replace(
            "{-# LANGUAGE NoImplicitPrelude #-}",
            "{-# LANGUAGE DeriveAnyClass #-}\n"
            "{-# LANGUAGE DeriveGeneric #-}\n"
            "{-# LANGUAGE NoImplicitPrelude #-}",
        )
    extensions = extensions.replace(
        "{-# LANGUAGE NoImplicitPrelude #-}",
        "{-# LANGUAGE FlexibleContexts #-}\n"
        "{-# LANGUAGE OverloadedStrings #-}\n"
        "{-# LANGUAGE TemplateHaskell #-}\n"
        "{-# LANGUAGE NoImplicitPrelude #-}",
    )
    if "import GHC.Generics (Generic)" not in imports:
        imports += "import GHC.Generics (Generic)\n"
    imports += "import qualified Clash.Verification as Verification\n"
    declarations = _ready_valid_declarations() + declarations
    if any(
        interface.ordering is RequestResponseOrdering.OUT_OF_ORDER
        for interface in module.request_responses
    ):
        declarations += _request_response_id_helpers()
    domain = "ZLangSystem"

    input_types: list[str] = []
    input_names: list[str] = []
    input_annotations: list[str] = [
        f'PortName "{module.clock}"',
        f'PortName "{module.reset}"',
    ]
    output_types: list[str] = []
    output_values: list[str] = []
    output_annotations: list[str] = []
    bindings: list[str] = ["reset_active = unsafeToActiveHigh hasReset"]
    struct_accessors = frozenset(
        _field_accessor(struct.name, field.name)
        for struct in module.structs
        for field in struct.fields
    )
    port_value_names = {
        port.name: _request_response_value_name(port.name, struct_accessors)
        for port in module.ports
    }
    value_names = {
        _leaf_key(expr.InputRef(port.name, port.type)):
            port_value_names[port.name]
        for port in module.inputs
    }
    value_names.update({
        _leaf_key(value): (
            private_names.stage(_stage_prefix(value), value.instance, expr.sequential_stage_count(value))
        )
        for value in staged.values()
    })
    render_state = lambda value: _emit_signal_expression(value, value_names)

    for port in module.ports:
        type_ = _emit_type(port.type)
        if port.direction is PortDirection.INPUT:
            input_types.append(f"Signal {domain} ({type_})")
            input_names.append(port_value_names[port.name])
            input_annotations.append(
                _top_value_port_annotation(port.name, port.type)
            )
        else:
            output_types.append(f"Signal {domain} ({type_})")
            output_values.append(port_value_names[port.name])
            output_annotations.append(
                _top_value_port_annotation(port.name, port.type)
            )

    for interface in module.request_responses:
        request_type = _emit_type(interface.request_type)
        response_type = _emit_type(interface.response_type)
        requester = interface.role is RequestResponseRole.REQUESTER
        if requester:
            request_input = f"{interface.name}_request_backward"
            response_input = f"{interface.name}_response_forward"
            input_types.extend((
                f"Signal {domain} ZLangReadyValidBackward",
                f"Signal {domain} (ZLangReadyValidForward ({response_type}))",
            ))
            input_names.extend((request_input, response_input))
            input_annotations.extend((
                f'PortName "{interface.name}_request_ready"',
                _top_forward_port_annotation(
                    f"{interface.name}_response",
                    interface.response_type,
                    "valid",
                ),
            ))
            bindings.extend((
                f"{interface.name}_request_ready = zlangRvReady <$> {request_input}",
                f"{interface.name}_response_payload = zlangRvPayload <$> {response_input}",
                f"{interface.name}_response_valid = zlangRvValid <$> {response_input}",
            ))
            output_types.extend((
                f"Signal {domain} (ZLangReadyValidForward ({request_type}))",
                f"Signal {domain} ZLangReadyValidBackward",
            ))
            output_values.extend((
                f"ZLangReadyValidForward <$> {interface.name}_request_payload "
                f"<*> {interface.name}_request_valid_checked",
                f"ZLangReadyValidBackward <$> {interface.name}_response_ready_checked",
            ))
            output_annotations.extend((
                _top_forward_port_annotation(
                    f"{interface.name}_request",
                    interface.request_type,
                    "valid",
                ),
                f'PortName "{interface.name}_response_ready"',
            ))
        else:
            request_input = f"{interface.name}_request_forward"
            response_input = f"{interface.name}_response_backward"
            input_types.extend((
                f"Signal {domain} (ZLangReadyValidForward ({request_type}))",
                f"Signal {domain} ZLangReadyValidBackward",
            ))
            input_names.extend((request_input, response_input))
            input_annotations.extend((
                _top_forward_port_annotation(
                    f"{interface.name}_request",
                    interface.request_type,
                    "valid",
                ),
                f'PortName "{interface.name}_response_ready"',
            ))
            bindings.extend((
                f"{interface.name}_request_payload = zlangRvPayload <$> {request_input}",
                f"{interface.name}_request_valid = zlangRvValid <$> {request_input}",
                f"{interface.name}_response_ready = zlangRvReady <$> {response_input}",
            ))
            output_types.extend((
                f"Signal {domain} ZLangReadyValidBackward",
                f"Signal {domain} (ZLangReadyValidForward ({response_type}))",
            ))
            output_values.extend((
                f"ZLangReadyValidBackward <$> {interface.name}_request_ready_checked",
                f"ZLangReadyValidForward <$> {interface.name}_response_payload "
                f"<*> {interface.name}_response_valid_checked",
            ))
            output_annotations.extend((
                f'PortName "{interface.name}_request_ready"',
                _top_forward_port_annotation(
                    f"{interface.name}_response",
                    interface.response_type,
                    "valid",
                ),
            ))

    transition = module.resolved_transition
    if module.rules and transition is None:
        raise ClashEmissionError(
            f"request/response module '{module.name}' lacks resolved transition IR"
        )
    unified_state = bool(
        transition is not None
        and (
            transition.resources
            or transition.action_groups
            or module.registers
            or module.next_assignments
            or module.rules
        )
    )
    scheduled_wire_outputs = {
        resource.name
        for resource in (transition.resources if transition is not None else ())
        if resource.kind is StateResourceKind.OUTPUT
    }
    for local in module.locals:
        if local.compile_time:
            continue
        bindings.append(
            f"{_clash_name(local.name)} = {render_state(local.expression)}"
        )
    for assignment in module.assignments:
        if (
            assignment.channel is None
            and assignment.signal is None
            and assignment.target.name in scheduled_wire_outputs
        ):
            continue
        bindings.append(
            f"{_request_response_assignment_name(assignment, port_value_names)} = "
            f"{render_state(assignment.expression)}"
        )
    for interface in module.request_responses:
        bindings.extend(_emit_request_response_bindings(interface))
    if unified_state:
        bindings.extend(_emit_unified_schedule_bindings(module, render_state))
        bindings.extend(_emit_unified_register_bindings(module, render_state))
        bindings.extend(_emit_unified_output_bindings(
            module,
            render_state,
            include=scheduled_wire_outputs,
            output_names=port_value_names,
        ))
    for value in staged.values():
        previous = render_state(value.expression)
        for stage in range(1, expr.sequential_stage_count(value) + 1):
            name = private_names.stage(_stage_prefix(value), value.instance, stage)
            bindings.append(
                f"{name} = register {_zero_value(value.type)} ({previous})"
            )
            previous = name

    output_type = _emit_product(output_types)
    result = _emit_product(output_values)
    circuit_signature = " -> ".join((*input_types, output_type))
    top_signature = " -> ".join(
        (
            f"Clock {domain}",
            f"Reset {domain}",
            *input_types,
            output_type,
        )
    )
    circuit_arguments = " ".join(input_names)
    circuit_lhs = f"circuit {circuit_arguments}" if circuit_arguments else "circuit"
    top_arguments = " ".join((module.clock, module.reset, *input_names))
    application = f" {circuit_arguments}" if circuit_arguments else ""
    where_block = "\n".join(f"  {binding}" for binding in bindings)
    input_ports = ", ".join(input_annotations)
    output_port = (
        output_annotations[0]
        if len(output_annotations) == 1
        else f'PortProduct "" [{", ".join(output_annotations)}]'
    )

    return f'''{extensions}
module {module.name} where

{imports}
{declarations}{_domain_declaration(module, domain)}

circuit :: HiddenClockResetEnable {domain} => {circuit_signature}
{circuit_lhs} = {result}
 where
{where_block}

topEntity :: {top_signature}
topEntity {top_arguments} = exposeClockResetEnable circuit {module.clock} {_top_reset_expression(module)} enableGen{application}

{{-# ANN topEntity
  (Synthesize
    {{ t_name = "{module.name}"
    , t_inputs = [{input_ports}]
    , t_output = {output_port}
    }}) #-}}
'''


def _request_response_value_name(
    name: str,
    struct_accessors: frozenset[str],
) -> str:
    """Keep standalone endpoint wires disjoint from struct accessors."""

    candidate = _clash_name(name)
    return f"zlang_port_{candidate}" if candidate in struct_accessors else candidate


def _request_response_assignment_name(
    assignment: object,
    port_value_names: dict[str, str],
) -> str:
    if assignment.channel is None or assignment.signal is None:
        return port_value_names[assignment.target.name]
    suffix = assignment.signal.value
    requester = assignment.target.role is RequestResponseRole.REQUESTER
    gated_request = (
        (requester
         and assignment.channel is RequestResponseChannel.REQUEST
         and assignment.signal is ReadyValidSignal.VALID)
        or
        (requester
         and assignment.channel is RequestResponseChannel.RESPONSE
         and assignment.signal is ReadyValidSignal.READY)
        or
        (not requester
         and assignment.channel is RequestResponseChannel.REQUEST
         and assignment.signal is ReadyValidSignal.READY)
        or
        (not requester
         and assignment.channel is RequestResponseChannel.RESPONSE
         and assignment.signal is ReadyValidSignal.VALID)
    )
    if gated_request:
        suffix += "_request"
    return f"{assignment.target.name}_{assignment.channel.value}_{suffix}"


def _emit_request_response_bindings(interface: object) -> tuple[str, ...]:
    name = interface.name
    maximum = interface.max_outstanding
    width = max(1, maximum.bit_length())
    count_type = f"Unsigned {width}"
    limit = f"({maximum} :: {count_type})"
    common = [
        f"{name}_outstanding = register (0 :: {count_type}) {name}_outstanding_next",
    ]
    requester = interface.role is RequestResponseRole.REQUESTER
    if not requester and interface.ordering is RequestResponseOrdering.OUT_OF_ORDER:
        raise ClashEmissionError(
            "standalone out-of-order responder emission is not implemented"
        )
    if interface.ordering is RequestResponseOrdering.OUT_OF_ORDER:
        if interface.match_by is None or interface.id_type is None:
            raise ClashEmissionError(
                f"out-of-order interface '{name}' has no typed match field"
            )
        request_accessor = _field_accessor(
            interface.request_type.name, interface.match_by
        )
        response_accessor = _field_accessor(
            interface.response_type.name, interface.match_by
        )
        id_type = _emit_type(interface.id_type)
        id_zero = _zero_value(interface.id_type)
        common.extend(
            (
                f"{name}_request_id = {request_accessor} <$> {name}_request_payload",
                f"{name}_response_id = {response_accessor} <$> {name}_response_payload",
                f"{name}_ids_valid = register (repeat low :: Vec {maximum} Bit) {name}_ids_valid_next",
                f"{name}_ids = register (repeat {id_zero} :: Vec {maximum} ({id_type})) {name}_ids_next",
                f"{name}_duplicate = (\\requested ready count identifier valids ids resetActive -> not resetActive && requested == high && ready == high && count < {limit} && zlangContainsId identifier valids ids) <$> {name}_request_valid_request <*> {name}_request_ready <*> {name}_outstanding <*> {name}_request_id <*> {name}_ids_valid <*> {name}_ids <*> reset_active",
                f"{name}_missing = (\\requested valid count identifier valids ids resetActive -> not resetActive && requested == high && valid == high && count > 0 && not (zlangContainsId identifier valids ids)) <$> {name}_response_ready_request <*> {name}_response_valid <*> {name}_outstanding <*> {name}_response_id <*> {name}_ids_valid <*> {name}_ids <*> reset_active",
            )
        )
        request_gate = " || duplicate"
        response_gate = " || missing"
        request_args = f" <*> {name}_duplicate"
        response_args = f" <*> {name}_missing"
    else:
        request_gate = ""
        response_gate = ""
        request_args = ""
        response_args = ""
    if requester:
        common.extend((
            f"{name}_request_valid = (\\requested count resetActive{' duplicate' if request_args else ''} -> if resetActive || count >= {limit}{request_gate} then low else requested) <$> {name}_request_valid_request <*> {name}_outstanding <*> reset_active{request_args}",
            f"{name}_request_transfer = (\\valid ready -> valid .&. ready) <$> {name}_request_valid <*> {name}_request_ready",
        ))
        if interface.ordering is RequestResponseOrdering.OUT_OF_ORDER:
            # Preserve the established OOO representative byte-for-byte; this
            # bounded slice changes only standalone in-order roles.
            common.append(
                f"{name}_response_ready = (\\requested count resetActive{' missing' if response_args else ''} -> if resetActive || count == 0{response_gate} then low else requested) <$> {name}_response_ready_request <*> {name}_outstanding <*> reset_active{response_args}"
            )
        else:
            # A zero-latency response is legal when its request transfers in
            # this same cycle.  The count still records only committed prior
            # cycles and therefore cannot be the sole admission predicate.
            common.append(
                f"{name}_response_ready = (\\requested count requestTransfer resetActive -> if resetActive || (count == 0 && requestTransfer == low) then low else requested) <$> {name}_response_ready_request <*> {name}_outstanding <*> {name}_request_transfer <*> reset_active"
            )
        common.append(
            f"{name}_response_transfer = (\\valid ready -> valid .&. ready) <$> {name}_response_valid <*> {name}_response_ready"
        )
    else:
        common.extend((
            f"{name}_request_ready = (\\requested count resetActive -> if resetActive || count >= {limit} then low else requested) <$> {name}_request_ready_request <*> {name}_outstanding <*> reset_active",
            f"{name}_request_transfer = (\\valid ready -> valid .&. ready) <$> {name}_request_valid <*> {name}_request_ready",
            f"{name}_response_valid = (\\requested count requestTransfer resetActive -> if resetActive || (count == 0 && requestTransfer == low) then low else requested) <$> {name}_response_valid_request <*> {name}_outstanding <*> {name}_request_transfer <*> reset_active",
            f"{name}_response_transfer = (\\valid ready -> valid .&. ready) <$> {name}_response_valid <*> {name}_response_ready",
        ))
    common.append(
        f"{name}_within_limit = (\\transfer count -> transfer == low || count < {limit}) <$> {name}_request_transfer <*> {name}_outstanding"
    )
    if interface.ordering is RequestResponseOrdering.OUT_OF_ORDER:
        common.append(
            f"{name}_has_request = (\\transfer count -> transfer == low || count > 0) <$> {name}_response_transfer <*> {name}_outstanding"
        )
    else:
        common.append(
            f"{name}_has_request = (\\transfer count requestTransfer -> transfer == low || count > 0 || requestTransfer == high) <$> {name}_response_transfer <*> {name}_outstanding <*> {name}_request_transfer"
        )
    common.append(
        f"{name}_outstanding_next = (\\count requestTransfer responseTransfer -> case (requestTransfer == high, responseTransfer == high) of {{ (True, False) -> if count < {limit} then count + 1 else count; (False, True) -> if count > 0 then count - 1 else count; _ -> count }}) <$> {name}_outstanding <*> {name}_request_transfer <*> {name}_response_transfer"
    )
    if interface.ordering is RequestResponseOrdering.OUT_OF_ORDER:
        common.extend(
            (
                f"({name}_ids_valid_next, {name}_ids_next) = unbundle (zlangUpdateIds <$> {name}_request_transfer <*> {name}_request_id <*> {name}_response_transfer <*> {name}_response_id <*> {name}_ids_valid <*> {name}_ids)",
                f"{name}_id_ok = (\\duplicate missing -> not duplicate && not missing) <$> {name}_duplicate <*> {name}_missing",
                f"{name}_request_valid_checked = Verification.checkI \"{name}_ids_valid\" Verification.AutoRenderAs (Verification.assert {name}_id_ok) . Verification.checkI \"{name}_within_limit\" Verification.AutoRenderAs (Verification.assert {name}_within_limit) $ {name}_request_valid",
            )
        )
    elif requester:
        common.append(
            f"{name}_request_valid_checked = Verification.checkI \"{name}_within_limit\" Verification.AutoRenderAs (Verification.assert {name}_within_limit) $ {name}_request_valid"
        )
    if requester:
        common.append(
            f"{name}_response_ready_checked = Verification.checkI \"{name}_has_request\" Verification.AutoRenderAs (Verification.assert {name}_has_request) $ {name}_response_ready"
        )
    else:
        common.extend((
            f"{name}_request_ready_checked = Verification.checkI \"{name}_within_limit\" Verification.AutoRenderAs (Verification.assert {name}_within_limit) $ {name}_request_ready",
            f"{name}_response_valid_checked = Verification.checkI \"{name}_has_request\" Verification.AutoRenderAs (Verification.assert {name}_has_request) $ {name}_response_valid",
        ))
    return tuple(common)


def _request_response_id_helpers() -> str:
    return '''zlangContainsId :: (KnownNat n, Eq a) => a -> Vec n Bit -> Vec n a -> Bool
zlangContainsId identifier valids identifiers =
  or (zipWith (\\valid slot -> valid == high && slot == identifier) valids identifiers)

zlangInsertId :: KnownNat n => a -> Vec n Bit -> Vec n a -> (Vec n Bit, Vec n a)
zlangInsertId identifier valids identifiers =
  case findIndex (== low) valids of
    Just index -> (replace index high valids, replace index identifier identifiers)
    Nothing -> (valids, identifiers)

zlangRemoveId :: Eq a => a -> Vec n Bit -> Vec n a -> (Vec n Bit, Vec n a)
zlangRemoveId identifier valids identifiers =
  (zipWith (\\valid slot -> if valid == high && slot == identifier then low else valid) valids identifiers, identifiers)

zlangUpdateIds :: (KnownNat n, Eq a) => Bit -> a -> Bit -> a -> Vec n Bit -> Vec n a -> (Vec n Bit, Vec n a)
zlangUpdateIds requestTransfer requestId responseTransfer responseId valids identifiers =
  let (afterResponseValid, afterResponseIds) =
        if responseTransfer == high
          then zlangRemoveId responseId valids identifiers
          else (valids, identifiers)
  in if requestTransfer == high
       then zlangInsertId requestId afterResponseValid afterResponseIds
       else (afterResponseValid, afterResponseIds)

'''


def _emit_product(items: list[str]) -> str:
    if not items:
        raise ClashEmissionError("module has no externally driven outputs")
    if len(items) == 1:
        return items[0]
    return f"({', '.join(items)})"


def _emit_scalar_product(items: list[str]) -> str:
    """Render the ordered product used by an ordinary scalar selected top.

    Clash represents a top with no physical outputs as the unit product.  Keep
    the historical spelling for one output and use an ordinary tuple for two
    or more outputs so existing one-output generated source remains stable.
    """

    if not items:
        return "()"
    return _emit_product(items)


def _top_scalar_output_annotation(outputs: tuple[Port, ...]) -> str:
    annotations = [
        _top_value_port_annotation(output.name, output.type)
        for output in outputs
    ]
    if not annotations:
        return 'PortProduct "" []'
    if len(annotations) == 1:
        return annotations[0]
    return f'PortProduct "" [{", ".join(annotations)}]'


def _scalar_output_assignments(module: Module) -> dict[str, expr.Expression]:
    """Return complete ordinary-wire output drivers by semantic port name."""

    outputs = {port.name: port for port in module.outputs}
    result: dict[str, expr.Expression] = {}
    for assignment in module.assignments:
        target = assignment.target
        if (
            not isinstance(target, Port)
            or target.name not in outputs
            or assignment.signal is not None
            or assignment.channel is not None
        ):
            raise ClashEmissionError(
                "ordinary scalar top contains a non-wire output assignment"
            )
        if target.name in result:
            raise ClashEmissionError(
                f"ordinary scalar output '{target.name}' has multiple drivers"
            )
        result[target.name] = assignment.expression
    return result


def _emit_scalar_wire_module(module: Module) -> str:
    """Emit a combinational scalar top with an ordered zero-or-more result."""

    if module.rules or module.registers or module.next_assignments:
        raise ClashEmissionError(
            "combinational scalar top unexpectedly contains sequential state"
        )
    drivers = _scalar_output_assignments(module)
    missing = [port.name for port in module.outputs if port.name not in drivers]
    if missing:
        raise ClashEmissionError(
            "ordinary scalar outputs have no drivers: " + ", ".join(missing)
        )

    inputs = module.inputs
    outputs = module.outputs
    output_type = _emit_scalar_product(
        [_emit_type(port.type) for port in outputs]
    )
    output_value = _emit_scalar_product(
        [_emit_expression(drivers[port.name]) for port in outputs]
    )
    signature = " -> ".join(
        [*(_emit_type(port.type) for port in inputs), output_type]
    )
    arguments = " ".join(_clash_name(port.name) for port in inputs)
    input_ports = ", ".join(
        _top_value_port_annotation(port.name, port.type) for port in inputs
    )
    extensions, imports, declarations = _emit_prelude(module)
    architecture_notes = "".join(
        "-- ZLang implement architecture candidate: "
        f"output={exploration.output} selected={exploration.selected} "
        f"kind={exploration.selected_candidate.kind.value} "
        f"parallelism={exploration.selected_candidate.parallelism} "
        f"add_depth={exploration.selected_candidate.add_depth} "
        "equivalence=mathematical\n"
        for exploration in module.architecture_explorations
    )

    return f'''{extensions}
module {module.name} where

{imports}
{declarations}{architecture_notes}topEntity :: {signature}
topEntity {arguments} = {output_value}

{{-# ANN topEntity
  (Synthesize
    {{ t_name = "{module.name}"
    , t_inputs = [{input_ports}]
    , t_output = {_top_scalar_output_annotation(outputs)}
    }}) #-}}
'''


def _pipeline_catalog_is_emitted(module: Module, exploration: object) -> bool:
    """Return whether a retained pipeline candidate is the emitted value.

    ``PipelineExploration`` is also retained for target-planner/report
    consumers when the unified ``implement`` selector chose a different
    candidate.  Its selected entry must never be described as RTL latency or
    registers unless the exact expression is present on the output
    assignment.  Pipeline allocation IDs are intentionally ignored by the
    selection identity helper; they are physical bookkeeping only.
    """

    output = getattr(exploration, "output", None)
    selected = getattr(exploration, "selected_candidate", None)
    selected_expression = getattr(selected, "expression", None)
    if output is None or selected_expression is None:
        return False
    selected_identity = selection_expression_semantic_identity(
        selected_expression
    )
    return any(
        assignment.target.name == output
        and assignment.signal is None
        and assignment.channel is None
        and selection_expression_semantic_identity(assignment.expression)
        == selected_identity
        for assignment in module.assignments
    )


def _assignment_name(assignment: object) -> str:
    target = assignment.target
    signal = assignment.signal
    return target.name if signal is None else f"{target.name}_{signal.value}"


def _local_typed_functions(module: Module) -> tuple[Function, ...]:
    """Return every callable definition owned directly by ``module``.

    This local view is useful while discovering aggregate types.  Actual Clash
    declaration emission uses :func:`_typed_functions`, which selects the
    hierarchy-wide reachable transitive closure.
    """
    result: list[Function] = []
    by_name: dict[str, Function] = {}
    specializations = sorted(
        getattr(module, "callable_definitions", ()),
        key=lambda item: (getattr(item, "callee_identity", "") or item.name, item.name),
    )
    for function in (*module.functions, *specializations):
        previous = by_name.get(function.name)
        if previous is not None:
            if previous != function:
                raise ClashEmissionError(
                    f"typed function name '{function.name}' has conflicting definitions"
                )
            continue
        by_name[function.name] = function
        result.append(function)
    return tuple(result)


def _typed_functions(module: Module) -> tuple[Function, ...]:
    """Return the hierarchy-wide executable callable closure exactly once."""

    try:
        return tuple(
            reachable_module_callables(module, include_hierarchy=True)
        )
    except CallableReachabilityError as error:
        raise ClashEmissionError(str(error)) from error


def _requires_no_reset_memory(module: Module) -> bool:
    """Return whether this hierarchy emits preserved memory state."""

    if any(
        memory.contents_reset is MemoryResetPolicy.PRESERVE
        or memory.read_data_reset is MemoryResetPolicy.PRESERVE
        for memory in module.memories
    ):
        return True
    return any(_requires_no_reset_memory(child) for child in module.children)


def _emit_prelude(module: Module) -> tuple[str, str, str]:
    extensions = "{-# LANGUAGE DataKinds #-}\n"
    if module.roms:
        extensions += "{-# LANGUAGE TypeApplications #-}\n"
    imports = "import Clash.Prelude\n"
    if _requires_no_reset_memory(module):
        imports += "import Clash.Explicit.Reset (noReset)\n"
    declarations = ""
    structs = _all_structs(module)
    if structs:
        bitpack_structs = _required_bitpack_structs(module)
        extensions += "{-# LANGUAGE DeriveAnyClass #-}\n"
        extensions += "{-# LANGUAGE DeriveGeneric #-}\n"
        imports += "import GHC.Generics (Generic)\n"
        declarations += "\n".join(
            _emit_struct(
                type_,
                derive_bitpack=_clash_struct_name(type_.name) in bitpack_structs,
            )
            for type_ in structs
        )
    functions = _typed_functions(module)
    if functions:
        declarations += "\n".join(
            _emit_function(function) for function in functions
        )
    if declarations:
        declarations += "\n"
    extensions += "{-# LANGUAGE NoImplicitPrelude #-}\n"
    return extensions, imports, declarations


def _all_structs(module: Module) -> tuple[StructType, ...]:
    """Collect source aggregate types used by a hierarchical compilation unit."""
    result: list[StructType] = []
    seen: set[str] = set()

    def add_type(type_: object) -> None:
        if isinstance(type_, VecType):
            add_type(type_.element_type)
            return
        if isinstance(type_, TupleType):
            for element in type_.elements:
                add_type(element)
            return
        if not isinstance(type_, StructType):
            return
        key = _clash_struct_name(type_.name)
        if key in seen:
            return
        # Dependencies must precede aggregate declarations in Haskell.  This
        # also discovers contextual specializations such as Complex<fixed<...>>
        # which are intentionally not enumerated in Module.structs.
        seen.add(key)
        for field in type_.fields:
            add_type(field.type)
        result.append(type_)

    def visit_value(value: object) -> None:
        type_ = getattr(value, "type", None)
        if type_ is not None:
            add_type(type_)
        if isinstance(value, tuple):
            for item in value:
                visit_value(item)
        elif is_dataclass(value):
            for item in fields(value):
                if item.name not in {"type", "origin"}:
                    visit_value(getattr(value, item.name))

    def visit(current: Module) -> None:
        for type_ in current.structs:
            add_type(type_)
        for port in current.ports:
            add_type(port.type)
        for function in _local_typed_functions(current):
            for parameter in function.parameters:
                add_type(parameter.type)
            add_type(function.return_type)
            visit_value(function.body)
        for local in current.locals:
            add_type(local.type)
            visit_value(local.expression)
        for register in current.registers:
            add_type(register.type)
            visit_value(register.initial)
        for rom in current.roms:
            add_type(rom.element_type)
            visit_value(rom.read_address)
            visit_value(rom.contents)
        for assignment in current.assignments:
            visit_value(assignment.expression)
        for assignment in current.next_assignments:
            visit_value(assignment.expression)
        for rule in current.rules:
            visit_value(rule.guard)
            visit_value(rule.actions)
        if current.resolved_transition is not None:
            visit_value(current.resolved_transition)
        for child in current.children:
            visit(child)

    visit(module)
    return tuple(result)


def _required_bitpack_structs(module: Module) -> frozenset[str]:
    """Find only structs crossing an explicit typed packing boundary."""

    required: set[str] = set()

    def add_type(type_: HardwareType) -> None:
        if isinstance(type_, VecType):
            add_type(type_.element_type)
        elif isinstance(type_, TupleType):
            for element in type_.elements:
                add_type(element)
        elif isinstance(type_, StructType):
            required.add(_clash_struct_name(type_.name))
            for field in type_.fields:
                add_type(field.type)

    def visit_value(value: object) -> None:
        if isinstance(value, expr.Bitcast):
            add_type(value.expression.type)
            add_type(value.type)
        elif isinstance(value, expr.Pack):
            add_type(value.expression.type)
        elif isinstance(value, expr.Unpack):
            add_type(value.type)
        elif isinstance(value, expr.Concat):
            for operand in value.operands:
                add_type(operand.type)
        if isinstance(value, tuple):
            for item in value:
                visit_value(item)
        elif is_dataclass(value):
            for item in fields(value):
                if item.name != "origin":
                    visit_value(getattr(value, item.name))

    def visit(current: Module) -> None:
        for function in _local_typed_functions(current):
            visit_value(function.body)
        for local in current.locals:
            visit_value(local.expression)
        for register in current.registers:
            visit_value(register.initial)
        for memory in current.memories:
            add_type(memory.element_type)
        for rom in current.roms:
            add_type(rom.element_type)
            visit_value(rom.read_address)
        for assignment in current.assignments:
            visit_value(assignment.expression)
        for assignment in current.next_assignments:
            visit_value(assignment.expression)
        for binding in current.instance_bindings:
            visit_value(binding.expression)
        for rule in current.rules:
            visit_value(rule.guard)
            visit_value(rule.actions)
        for child in current.children:
            visit(child)

    visit(module)
    return frozenset(required)


def _child_function_name(
    module: Module,
    specialization_identity: str | None = None,
) -> str:
    """Return one reusable scalar-child helper name.

    A source module name is not a monomorphic Clash component identity.  Root
    helper callers may omit the identity; nested catalogs allocate compact
    suffixes across the complete hierarchy, independent of physical instances.
    """

    base = _clash_name(module.name[:1].lower() + module.name[1:])
    if specialization_identity is None:
        return base
    return f"{base}_s{specialization_identity[:8]}"


@dataclass(frozen=True)
class _ScalarComponentOwner:
    """Stable owner for one set of scalar-child application sites."""

    kind: str
    module_name: str
    specialization_identity: str | None = None


@dataclass(frozen=True)
class _ScalarApplicationKey:
    parent: _ScalarComponentOwner
    instance_identity: str


@dataclass(frozen=True)
class _ScalarChildComponent:
    module: Module
    specialization_identity: str
    component_name: str
    owner: _ScalarComponentOwner
    representative_path: tuple[str, ...]


@dataclass(frozen=True)
class _ScalarChildCatalog:
    """Whole-hierarchy scalar helper and semantic application registry."""

    components: tuple[_ScalarChildComponent, ...]
    applications: tuple[tuple[_ScalarApplicationKey, str], ...]
    root_owner: _ScalarComponentOwner

    def get(self, key: _ScalarApplicationKey) -> str | None:
        return next(
            (name for candidate, name in self.applications if candidate == key),
            None,
        )


def _scalar_specialization_owner(
    module: Module,
    specialization_identity: str,
) -> _ScalarComponentOwner:
    return _ScalarComponentOwner(
        "specialization", module.name, specialization_identity,
    )


def _scalar_component_base_name(module: Module) -> str:
    return (
        _protocol_child_name(module)
        if module.csr_blocks else _child_function_name(module)
    )


def _scalar_component_bound_names(module: Module) -> frozenset[str]:
    """Return lexical names that can shadow a helper at an application site.

    These are derived exclusively from typed module contents.  Physical
    instance names remain result-binding identities and never become reusable
    component names.
    """

    private_names = _clash_module_names(module)
    names = {
        "reset_active",
        "zlang_rom_reset_hold",
        *(_clash_name(port.name) for port in module.ports),
        *(port.name for port in module.ports),
        *(_clash_name(local.name) for local in module.locals),
        *(_clash_name(register.name) for register in module.registers),
        *(_clash_name(fifo.name) for fifo in module.fifos),
        *(_clash_name(memory.name) for memory in module.memories),
        *(_clash_name(rom.name) for rom in module.roms),
    }
    names.update(
        f"{_clash_name(register.name)}_next"
        for register in module.registers
    )
    for rule in module.rules:
        names.update((
            private_names.rule(rule.name, "guard"),
            private_names.rule(rule.name, "fire"),
        ))
    for instance, child in zip(
        module.instances, module.children, strict=False,
    ):
        names.update(
            private_names.child_signal(instance.name, port.name)
            for port in child.outputs
        )

    def add_binding_lines(lines: tuple[str, ...]) -> None:
        for line in lines:
            lhs, separator, _rhs = line.partition(" = ")
            if not separator:
                continue
            names.update(re.findall(r"[a-z][A-Za-z0-9_]*", lhs))

    # Pure hierarchical helpers use this exact typed plan.  Reserve its
    # generated temporaries before assigning any nested helper name; the
    # renderer also supplies the selected nested names to the planner below as
    # a final layout-scope guard.
    if (
        not module.is_sequential
        and len(module.outputs) == 1
        and len(module.assignments) == 1
    ):
        materialized = plan_materialization(
            (module.assignments[0].expression,),
            reserved_names=(
                module.outputs[0].name,
                *(_clash_name(port.name) for port in module.inputs),
            ),
            generated_prefix="zlang_child_expr_",
        )
        names.update(item.name for item in materialized)

    delay_nodes: dict[int, expr.Delay | expr.Pipeline] = {}
    for assignment in (*module.assignments, *module.next_assignments):
        _collect_delays(assignment.expression, delay_nodes)
    for binding in module.instance_bindings:
        _collect_delays(binding.expression, delay_nodes)
    for rule in module.rules:
        _collect_delays(rule.guard, delay_nodes)
        for action in rule.actions:
            _collect_delays(action.expression, delay_nodes)
            if action.activation is not None:
                _collect_delays(action.activation, delay_nodes)
    for value in delay_nodes.values():
        names.update(
            private_names.stage(_stage_prefix(value), value.instance, stage)
            for stage in range(1, expr.sequential_stage_count(value) + 1)
        )

    # Storage helpers already centralize the exact generated bindings for each
    # typed resource.  Reuse those plans as the lexical inventory instead of
    # maintaining a second, incomplete list of suffix conventions here.
    if module.fifos or module.memories or module.roms:
        materialized_bindings, render = _storage_materialization(module)
        add_binding_lines(materialized_bindings)
        if module.resolved_transition is not None and (
            any(fifo.scheduled for fifo in module.fifos)
            or any(memory.scheduled for memory in module.memories)
        ):
            add_binding_lines(_emit_unified_schedule_bindings(module, render))
            add_binding_lines(_emit_unified_register_bindings(module, render))
        for fifo in module.fifos:
            add_binding_lines(
                _emit_scheduled_fifo_bindings(module, fifo, render)
                if fifo.scheduled
                else _emit_declared_fifo_bindings(fifo, render)
            )
        for memory in module.memories:
            add_binding_lines(
                _emit_scheduled_memory_bindings(module, memory, render)
                if memory.scheduled
                else _emit_memory_bindings(memory, render)
            )
        for rom in module.roms:
            add_binding_lines(_emit_rom_bindings(rom, render))
    return frozenset(names)


def _scalar_child_catalog(module: Module) -> _ScalarChildCatalog:
    """Plan all transitive scalar helpers from exact typed hierarchy identity.

    One declaration is emitted for each ``(module, specialization)`` pair.
    Applications are keyed by the parent's semantic specialization and the
    child's semantic instance identity, so a reusable helper never depends on
    a physical source instance name.
    """

    try:
        hierarchy = build_hierarchy_index(module)
    except HierarchyError as error:
        raise ClashEmissionError(str(error)) from error

    root_owner = _ScalarComponentOwner("root", module.name)
    component_records: dict[
        tuple[str, str], tuple[Module, tuple[str, ...]]
    ] = {}
    parent_modules: dict[tuple[str, str], list[Module]] = {}
    application_components: dict[_ScalarApplicationKey, tuple[str, str]] = {}

    for entry in hierarchy.entries[1:]:
        specialization = entry.specialization_identity
        instance_identity = entry.instance_identity
        parent_path = entry.parent_path
        if (
            specialization is None
            or instance_identity is None
            or parent_path is None
        ):
            raise ClashEmissionError(
                "typed scalar hierarchy child lacks semantic identity: "
                + ".".join(entry.physical_path)
            )
        component_key = (entry.module.name, specialization)
        component_records.setdefault(
            component_key, (entry.module, entry.physical_path),
        )
        parent_entry = hierarchy.at(parent_path)
        if parent_path == hierarchy.root_path:
            parent_owner = root_owner
        else:
            parent_specialization = parent_entry.specialization_identity
            if parent_specialization is None:
                raise ClashEmissionError(
                    "typed scalar hierarchy parent lacks specialization identity: "
                    + ".".join(parent_path)
                )
            parent_owner = _scalar_specialization_owner(
                parent_entry.module, parent_specialization,
            )
        application_key = _ScalarApplicationKey(
            parent_owner, instance_identity,
        )
        previous = application_components.get(application_key)
        if previous is not None and previous != component_key:
            raise ClashEmissionError(
                f"semantic scalar instance identity '{instance_identity}' has "
                "conflicting typed specializations"
            )
        application_components[application_key] = component_key
        parent_modules.setdefault(component_key, []).append(parent_entry.module)

    # Match the established nested-before-parent declaration order while
    # deduplicating each specialization across the complete hierarchy.
    component_order: list[tuple[str, str]] = []
    ordered_seen: set[tuple[str, str]] = set()

    def visit(path: tuple[str, ...]) -> None:
        for child_entry in hierarchy.children_of(path):
            visit(child_entry.physical_path)
            specialization = child_entry.specialization_identity
            assert specialization is not None
            key = (child_entry.module.name, specialization)
            if key not in ordered_seen:
                ordered_seen.add(key)
                component_order.append(key)

    visit(hierarchy.root_path)

    naming = build_component_name_plan(
        hierarchy,
        identifier=lambda name: _clash_name(name[:1].lower() + name[1:]),
    )
    desired_bases = {
        key: _scalar_component_base_name(child)
        for key, (child, _path) in component_records.items()
    }

    globally_reserved = {
        *_CLASH_RESERVED,
        *_CLASH_HELPER_RUNTIME_RESERVED,
        "circuit",
        "topEntity",
        *(function.name for function in _typed_functions(module)),
    }
    allocated: dict[tuple[str, str], str] = {}
    used: set[str] = set()
    for key in sorted(component_records):
        child, _path = component_records[key]
        base = desired_bases[key]
        specialization = key[1]
        shadowed = set().union(*(
            _scalar_component_bound_names(parent)
            for parent in parent_modules.get(key, ())
        ))
        token = naming.suffix(key[0], specialization)
        candidates = [f"{base}_{token}", f"zlang_child_{base}_{token}"]
        digest = hashlib.sha256(
            f"{key[0]}\0{specialization}".encode()
        ).hexdigest()[:12]
        candidates.append(f"zlang_child_{token}_{digest}")
        component_name = next(
            (
                candidate for candidate in candidates
                if candidate not in globally_reserved
                and candidate not in shadowed
                and candidate not in used
            ),
            None,
        )
        suffix = 0
        while component_name is None:
            candidate = f"zlang_child_{token}_{digest}_{suffix}"
            suffix += 1
            if (
                candidate not in globally_reserved
                and candidate not in shadowed
                and candidate not in used
            ):
                component_name = candidate
        allocated[key] = component_name
        used.add(component_name)

    components = tuple(
        _ScalarChildComponent(
            component_records[key][0],
            key[1],
            allocated[key],
            _scalar_specialization_owner(component_records[key][0], key[1]),
            component_records[key][1],
        )
        for key in component_order
    )
    applications = tuple(
        (application_key, allocated[component_key])
        for application_key, component_key in application_components.items()
    )
    return _ScalarChildCatalog(components, applications, root_owner)


def _scalar_child_function_for_instance(
    module: Module,
    instance_name: str,
    catalog: _ScalarChildCatalog | None = None,
    component_owner: _ScalarComponentOwner | None = None,
) -> str:
    selected_catalog = catalog or _scalar_child_catalog(module)
    selected_owner = component_owner or selected_catalog.root_owner
    elaborated = next(
        (
            item for item in module.elaborated_instances
            if item.instance.name == instance_name
        ),
        None,
    )
    if elaborated is None or not elaborated.instance_identity:
        raise ClashEmissionError(
            f"missing elaborated scalar child instance '{instance_name}'"
        )
    function = selected_catalog.get(_ScalarApplicationKey(
        selected_owner, elaborated.instance_identity,
    ))
    if function is None:
        raise ClashEmissionError(
            f"missing scalar specialization for semantic instance identity "
            f"'{elaborated.instance_identity}'"
        )
    return function


def _emit_child_application(
    function: str,
    applications: list[str],
    *,
    sequential_child: bool,
) -> str:
    """Apply one child without losing complete Signal-expression boundaries."""

    if not applications:
        return function
    if sequential_child:
        return f"{function} " + " ".join(f"({argument})" for argument in applications)
    return f"{function} <$> " + " <*> ".join(
        f"({argument})" for argument in applications
    )


def _emit_combinational_hierarchy(module: Module) -> str:
    """Emit the bounded scalar hierarchy, including structurally unrolled arrays."""

    private_names = _clash_module_names(module)
    leaf_names = _clash_child_leaf_names(module, private_names)
    output_drivers = _scalar_output_assignments(module)
    missing_outputs = [
        port.name for port in module.outputs if port.name not in output_drivers
    ]
    if missing_outputs:
        raise ClashEmissionError(
            "combinational hierarchy outputs have no drivers: "
            + ", ".join(missing_outputs)
        )
    if len(module.instances) != len(module.children):
        raise ClashEmissionError("elaborated hierarchy instance/child cardinality differs")
    scalar_catalog = _scalar_child_catalog(module)
    extensions, imports, declarations = _emit_prelude(module)
    declarations += _emit_scalar_child_declarations(
        module, catalog=scalar_catalog,
    )
    if declarations and not declarations.endswith("\n"):
        declarations += "\n"
    bindings: list[str] = []
    for instance, child in zip(module.instances, module.children, strict=True):
        if child.is_sequential or any(
            port.protocol is not InterfaceProtocol.WIRE for port in child.ports
        ):
            raise ClashEmissionError(
                "combinational instance arrays currently require scalar wire children"
            )
        child_bindings = {
            item.port: item.expression
            for item in module.instance_bindings
            if item.instance == instance.name
        }
        missing = [
            port.name for port in child.inputs if port.name not in child_bindings
        ]
        if missing:
            raise ClashEmissionError(
                f"instance '{instance.name}' is missing bindings for "
                f"{', '.join(missing)}"
            )
        arguments = " ".join(
            f"({_emit_expression(child_bindings[port.name], leaf_names)})"
            for port in child.inputs
        )
        application = _scalar_child_function_for_instance(
            module,
            instance.name,
            scalar_catalog,
            scalar_catalog.root_owner,
        )
        if arguments:
            application += " " + arguments
        child_outputs = [
            private_names.child_signal(instance.name, port.name)
            for port in child.outputs
        ]
        bindings.append(f"{_emit_product(child_outputs)} = {application}")

    inputs = module.inputs
    outputs = module.outputs
    signature = " -> ".join(
        [
            *(_emit_type(port.type) for port in inputs),
            _emit_scalar_product([_emit_type(output.type) for output in outputs]),
        ]
    )
    arguments = " ".join(_clash_name(port.name) for port in inputs)
    body = _emit_scalar_product([
        _emit_expression(output_drivers[output.name], leaf_names) for output in outputs
    ])
    where = "\n".join(f"  {item}" for item in bindings)
    input_ports = ", ".join(
        _top_value_port_annotation(port.name, port.type) for port in inputs
    )
    return f'''{extensions}
module {module.name} where

{imports}
{declarations}topEntity :: {signature}
topEntity {arguments} = {body}
 where
{where}

{{-# ANN topEntity
  (Synthesize
    {{ t_name = "{module.name}"
    , t_inputs = [{input_ports}]
    , t_output = {_top_scalar_output_annotation(outputs)}
    }}) #-}}
'''


def _emit_child_function(
    module: Module,
    component_name: str | None = None,
    *,
    scalar_catalog: _ScalarChildCatalog | None = None,
    component_owner: _ScalarComponentOwner | None = None,
) -> str:
    """Emit a pure child core used by a parent hierarchy wrapper."""
    if module.clock_domains or module.registers or module.rules:
        raise ClashEmissionError(
            f"hierarchical child '{module.name}' must be combinational in the M40 subset"
        )
    staged: dict[int, expr.Delay | expr.Pipeline] = {}
    for assignment in module.assignments:
        _collect_delays(assignment.expression, staged)
    if staged:
        raise ClashEmissionError(
            f"hierarchical child '{module.name}' has staged logic but no clock/reset domain"
        )
    if len(module.outputs) != 1 or len(module.assignments) != 1:
        raise ClashEmissionError(
            f"hierarchical child '{module.name}' requires one wire output"
        )
    if len(module.instances) != len(module.children):
        raise ClashEmissionError(
            f"hierarchical child '{module.name}' has incomplete nested "
            "elaboration metadata"
        )
    private_names = _clash_module_names(module)
    leaf_names = _clash_child_leaf_names(module, private_names)
    inputs = module.inputs
    signature = " -> ".join(
        [*(_emit_type(port.type) for port in inputs), _emit_type(module.outputs[0].type)]
    )
    arguments = " ".join(_clash_name(port.name) for port in inputs)
    output_expression = module.assignments[0].expression
    function_name = component_name or _child_function_name(module)
    selected_catalog = scalar_catalog
    selected_owner = component_owner
    nested_declarations = ""
    nested_functions: dict[str, str] = {}
    if module.children:
        if selected_catalog is None:
            selected_catalog = _scalar_child_catalog(module)
            selected_owner = selected_catalog.root_owner
            nested_declarations = _emit_scalar_child_declarations(
                module, catalog=selected_catalog,
            ) + "\n"
        elif selected_owner is None:
            raise ClashEmissionError(
                f"scalar child '{module.name}' lacks a component owner"
            )
        nested_functions = {
            instance.name: _scalar_child_function_for_instance(
                module,
                instance.name,
                selected_catalog,
                selected_owner,
            )
            for instance in module.instances
        }
    materialized = plan_materialization(
        (output_expression,),
        reserved_names=(
            function_name,
            module.outputs[0].name,
            *(_clash_name(port.name) for port in inputs),
            *nested_functions.values(),
        ),
        generated_prefix="zlang_child_expr_",
    )
    aliases = {item.expression: item.name for item in materialized}
    body = _emit_expression(replace_materialized(output_expression, aliases), leaf_names)
    if not module.children:
        if not materialized:
            return (
                f"{function_name} :: {signature}\n"
                f"{function_name} {arguments} = {body}\n"
            )
        legacy_bindings: list[str] = []
        for item in reversed(materialized):
            rewritten = replace_materialized(
                item.expression,
                aliases,
                keep=item.expression,
            )
            legacy_bindings.extend(
                (
                    f"  {item.name} :: {_emit_type(item.expression.type)}",
                    f"  {item.name} = {_emit_expression(rewritten, leaf_names)}",
                )
            )
        return (
            f"{function_name} :: {signature}\n"
            f"{function_name} {arguments} = {body}\n"
            " where\n"
            + "\n".join(legacy_bindings)
            + "\n"
        )
    bindings: list[str] = []
    for instance, child in zip(
        module.instances, module.children, strict=True
    ):
        if child.is_sequential or any(
            port.protocol is not InterfaceProtocol.WIRE
            for port in child.ports
        ):
            raise ClashEmissionError(
                f"combinational child '{module.name}' has a non-combinational "
                f"nested instance '{instance.name}'"
            )
        child_bindings = {
            item.port: item.expression
            for item in module.instance_bindings
            if item.instance == instance.name
        }
        missing = [
            port.name
            for port in child.inputs
            if port.name not in child_bindings
        ]
        if missing:
            raise ClashEmissionError(
                f"instance '{instance.name}' is missing bindings for "
                + ", ".join(missing)
            )
        application = nested_functions[instance.name]
        arguments_ = " ".join(
            f"({_emit_expression(child_bindings[port.name], leaf_names)})"
            for port in child.inputs
        )
        if arguments_:
            application += " " + arguments_
        child_outputs = [
            private_names.child_signal(instance.name, port.name)
            for port in child.outputs
        ]
        bindings.append(
            f"{_emit_product(child_outputs)} = {application}"
        )

    # A pure hierarchical child is the same closed typed value graph as an
    # ordinary callable helper.  Retain shared/expensive subgraphs as exact
    # local values instead of serializing their expanded tree at every use.
    # The planner is backend-independent and deterministic; explicit local
    # signatures preserve width, signedness and aggregate shape.
    for item in reversed(materialized):
        rewritten = replace_materialized(
            item.expression,
            aliases,
            keep=item.expression,
        )
        bindings.extend(
            (
                f"{item.name} :: {_emit_type(item.expression.type)}",
                f"{item.name} = {_emit_expression(rewritten, leaf_names)}",
            )
        )
    definition = (
        f"{function_name} :: {signature}\n"
        f"{function_name} {arguments} = {body}\n"
    )
    if bindings:
        definition += " where\n" + "\n".join(
            f"  {binding}" for binding in bindings
        ) + "\n"
    return nested_declarations + definition


def _clash_stage_materialization(
    staged: tuple[expr.Delay | expr.Pipeline, ...],
    delay_names: dict[tuple[object, ...], str],
    *,
    reserved_names: tuple[str, ...],
):
    """Share exact combinational DAG fragments across staged Signal roots.

    Register placement is already fixed in backend-independent Pipeline nodes.
    This helper only preserves DAG sharing when several register inputs consume
    the same typed fragment; it neither creates nor moves a clock boundary.
    """

    stage_aliases = {
        value: delay_names[_leaf_key(value)] for value in staged
    }
    physical_roots = tuple(
        replace_materialized(value.expression, stage_aliases)
        for value in staged
    )
    materialized = plan_materialization(
        physical_roots,
        reserved_names=reserved_names,
        generated_prefix="zlang_stage_expr_",
        minimum_shared_size=2,
    )
    aliases = {item.expression: item.name for item in materialized}

    def render(value: expr.Expression) -> str:
        physical = replace_materialized(value, stage_aliases)
        return _emit_signal_expression(
            replace_materialized(physical, aliases), delay_names
        )

    bindings = tuple(
        f"{item.name} = "
        + _emit_signal_expression(
            replace_materialized(
                item.expression,
                aliases,
                keep=item.expression,
            ),
            delay_names,
        )
        for item in dependency_ordered_materialization(materialized)
    )
    return bindings, render


def _emit_child_sequential(
    module: Module,
    component_name: str | None = None,
    *,
    scalar_catalog: _ScalarChildCatalog | None = None,
    component_owner: _ScalarComponentOwner | None = None,
) -> str:
    """Emit a stateful child as a hidden-clock/reset signal function."""
    if not module.outputs:
        raise ClashEmissionError(
            f"sequential child '{module.name}' requires at least one wire output"
        )
    if any(port.protocol is not InterfaceProtocol.WIRE for port in module.outputs):
        raise ClashEmissionError(
            f"sequential scalar child '{module.name}' has a non-wire output"
        )
    private_names = _clash_module_names(module)
    domain = "ZLangSystem"
    inputs = module.inputs
    outputs = module.outputs
    output_drivers = _scalar_output_assignments(module)
    signature = " -> ".join(
        [
            *(f"Signal {domain} ({_emit_type(port.type)})" for port in inputs),
            _emit_scalar_product(
                [
                    f"Signal {domain} ({_emit_type(port.type)})"
                    for port in outputs
                ]
            ),
        ]
    )
    args = " ".join(_clash_name(port.name) for port in inputs)
    selected_catalog = scalar_catalog
    selected_owner = component_owner
    child_declarations = ""
    if module.children:
        if selected_catalog is None:
            selected_catalog = _scalar_child_catalog(module)
            selected_owner = selected_catalog.root_owner
            child_declarations = _emit_scalar_child_declarations(
                module, catalog=selected_catalog,
            ) + "\n"
        elif selected_owner is None:
            raise ClashEmissionError(
                f"scalar child '{module.name}' lacks a component owner"
            )
    bindings: list[str] = ["reset_active = unsafeToActiveHigh hasReset"]
    delay_nodes: dict[int, expr.Delay | expr.Pipeline] = {}
    for assignment in (*module.assignments, *module.next_assignments):
        _collect_delays(assignment.expression, delay_nodes)
    for binding in module.instance_bindings:
        _collect_delays(binding.expression, delay_nodes)
    for rule in module.rules:
        _collect_delays(rule.guard, delay_nodes)
        for action in rule.actions:
            _collect_delays(action.expression, delay_nodes)
            if action.activation is not None:
                _collect_delays(action.activation, delay_nodes)
    delay_names = {
        _leaf_key(value): (
            private_names.stage(_stage_prefix(value), value.instance, expr.sequential_stage_count(value))
        )
        for value in delay_nodes.values()
    }
    delay_names.update(_clash_child_leaf_names(module, private_names))
    stage_materialized_bindings, render_staged = _clash_stage_materialization(
        tuple(delay_nodes.values()),
        delay_names,
        reserved_names=(
            *(_clash_name(port.name) for port in module.ports),
            *delay_names.values(),
        ),
    )
    bindings.extend(stage_materialized_bindings)
    if module.roms:
        bindings.append("zlang_rom_reset_hold = register True (pure False)")
    for rom in module.roms:
        bindings.extend(_emit_rom_bindings(rom))
    for instance, child in zip(
        module.instances, module.children, strict=True,
    ):
        child_bindings = {
            item.port: item.expression
            for item in module.instance_bindings
            if item.instance == instance.name
        }
        scalar_inputs = [port for port in child.inputs if port.protocol is InterfaceProtocol.WIRE]
        missing = [port.name for port in scalar_inputs if port.name not in child_bindings]
        if missing:
            raise ClashEmissionError(
                f"instance '{instance.name}' is missing bindings for {', '.join(missing)}"
            )
        applications = [
            _emit_signal_expression(child_bindings[port.name], delay_names)
            for port in scalar_inputs
        ]
        function = _scalar_child_function_for_instance(
            module,
            instance.name,
            selected_catalog,
            selected_owner,
        )
        application = _emit_child_application(
            function, applications, sequential_child=child.is_sequential
        )
        child_outputs = [
            private_names.child_signal(instance.name, port.name)
            for port in child.outputs
        ]
        result_pattern = _emit_product(child_outputs)
        bindings.append(f"{result_pattern} = {application}")
    transition = module.resolved_transition
    if module.rules and transition is None:
        raise ClashEmissionError(
            f"sequential child '{module.name}' lacks resolved transition IR"
        )
    groups = ordered_state_groups(transition) if transition is not None else ()
    rules_by_name = {rule.name: rule for rule in module.rules}
    if set(rules_by_name) != {group.rule_name for group in groups}:
        raise ClashEmissionError(
            f"sequential child '{module.name}' rule schedule differs from "
            "resolved transition IR"
        )
    ordered_rules = [rules_by_name[group.rule_name] for group in groups]
    conditional_state = bool(
        transition is not None and conditional_actions(transition)
    )
    if groups:
        bindings.extend(_emit_unified_schedule_bindings(
            module,
            lambda value: _emit_signal_expression(value, delay_names),
        ))
    next_by_register = {item.target.name: item.expression for item in module.next_assignments}
    if conditional_state:
        bindings.extend(_emit_unified_register_bindings(
            module,
            lambda value: _emit_signal_expression(value, delay_names),
        ))
    else:
        for register in module.registers:
            bindings.append(
                f"{register.name} = register {_emit_expression(register.initial)} ({register.name}_next)"
            )
            scheduled: expr.Expression = next_by_register.get(
                register.name, expr.RegisterRef(register.name, register.type)
            )
            for rule in reversed(ordered_rules):
                for action in rule.actions:
                    if action.target.name == register.name:
                        scheduled = expr.Mux(
                            expr.InputRef(private_names.rule(rule.name), BitType()),
                            action.expression, scheduled, register.type
                        )
            bindings.append(
                f"{register.name}_next = "
                f"{_emit_signal_expression(scheduled, delay_names)}"
            )
    for value in delay_nodes.values():
        previous = render_staged(value.expression)
        for stage in range(1, expr.sequential_stage_count(value) + 1):
            stage_name = private_names.stage(_stage_prefix(value), value.instance, stage)
            bindings.append(
                f"{stage_name} = register {_zero_value(value.type)} ({previous})"
            )
            previous = stage_name
    output_values: list[str] = []
    for output in outputs:
        scheduled_output = output_drivers.get(output.name)
        if conditional_state:
            assert transition is not None
            resource = next(
                (
                    item for item in transition.resources
                    if item.kind is StateResourceKind.OUTPUT
                    and item.name == output.name
                ),
                None,
            )
            output_writers = (
                [
                    (group, action)
                    for group in groups
                    for action in group.actions
                    if action.resource_id == resource.semantic_id
                    and action.kind is StateActionKind.OUTPUT_WRITE
                ]
                if resource is not None else []
            )
        else:
            output_writers = [
                (rule, action)
                for rule in ordered_rules
                for action in rule.actions
                if action.target.name == output.name
            ]
        if scheduled_output is None:
            if not output_writers:
                raise ClashEmissionError(
                    f"sequential child output '{output.name}' has no driver"
                )
            scheduled_output = expr.Constant(0, output.type)
        for rule, action in reversed(output_writers):
            scheduled_output = expr.Mux(
                expr.InputRef(
                    (
                        _unified_action_enable(transition, rule, action, private_names)
                        if conditional_state and transition is not None
                        else private_names.rule(rule.name)
                    ),
                    BitType(),
                ),
                (
                    action.operands[0]
                    if conditional_state else action.expression
                ),
                scheduled_output,
                output.type,
            )
        output_name = _clash_name(output.name)
        bindings.append(
            f"{output_name} = "
            f"{_emit_signal_expression(scheduled_output, delay_names)}"
        )
        output_values.append(output_name)
    result = _emit_scalar_product(output_values)
    body = "\n".join(f"  {item}" for item in bindings)
    return (
        f"{component_name or _child_function_name(module)} :: "
        f"HiddenClockResetEnable {domain} => {signature}\n"
        f"{component_name or _child_function_name(module)} {args} = {result}\n"
        f" where\n{body}\n{child_declarations}"
    )


def _emit_scalar_child_declarations(
    module: Module,
    *,
    include_csr: bool = True,
    catalog: _ScalarChildCatalog | None = None,
) -> str:
    selected_catalog = catalog or _scalar_child_catalog(module)
    declarations: list[str] = []
    for component in selected_catalog.components:
        child = component.module
        function = component.component_name
        if child.csr_blocks and not include_csr:
            continue
        if child.csr_blocks:
            declarations.append(
                _emit_csr_child_function(child, function)
            )
        elif child.fifos or child.memories or child.roms:
            declarations.append(
                _emit_storage_circuit(
                    child,
                    function,
                    scalar_catalog=selected_catalog,
                    component_owner=component.owner,
                    include_child_declarations=False,
                ).circuit
            )
        elif child.is_sequential:
            declarations.append(_emit_child_sequential(
                child,
                function,
                scalar_catalog=selected_catalog,
                component_owner=component.owner,
            ))
        else:
            declarations.append(_emit_child_function(
                child,
                function,
                scalar_catalog=selected_catalog,
                component_owner=component.owner,
            ))
    return "\n".join(declarations)


def _emit_sequential_module(module: Module) -> str:
    private_names = _clash_module_names(module)
    if module.clock is None or module.reset is None:
        raise ClashEmissionError("sequential module requires clock and reset")
    if module.hierarchical_connections:
        return _emit_hierarchical_protocol_module(module)
    if any(
        port.protocol is not InterfaceProtocol.WIRE
        for child in module.children
        for port in child.ports
    ):
        raise ClashEmissionError(
            "hierarchical protocol child emission is not yet implemented; "
            "protocol endpoints are retained in semantic IR"
        )
    inputs = module.inputs
    outputs = module.outputs
    direct_output_drivers = _scalar_output_assignments(module)
    scalar_catalog = _scalar_child_catalog(module) if module.children else None
    extensions, imports, declarations = _emit_prelude(module)
    declarations += _domain_declaration(module, "ZLangSystem")
    if module.children:
        assert scalar_catalog is not None
        declarations += "\n"
        declarations += _emit_scalar_child_declarations(
            module, catalog=scalar_catalog,
        )
        declarations += "\n"
    extensions = extensions.replace(
        "{-# LANGUAGE NoImplicitPrelude #-}\n",
        "{-# LANGUAGE TemplateHaskell #-}\n{-# LANGUAGE NoImplicitPrelude #-}\n",
    )
    domain = "ZLangSystem"
    pipeline_notes = "".join(
        "-- ZLang implement pipeline candidate: "
        f"output={exploration.output} selected={exploration.selected} "
        f"tree={exploration.selected_candidate.tree.value} "
        f"registers={exploration.selected_candidate.register_placement.value} "
        f"multipliers={exploration.selected_candidate.multiplier_mapping.value} "
        f"latency={exploration.selected_candidate.latency} "
        f"ii={exploration.selected_candidate.initiation_interval}\n"
        for exploration in module.pipeline_explorations
        if _pipeline_catalog_is_emitted(module, exploration)
    )

    signal_inputs = [
        f"Signal {domain} ({_emit_type(port.type)})" for port in inputs
    ]
    result_type = _emit_scalar_product([
        f"Signal {domain} ({_emit_type(output.type)})"
        for output in outputs
    ])
    top_signature = " -> ".join(
        [
            f"Clock {domain}",
            f"Reset {domain}",
            *signal_inputs,
            result_type,
        ]
    )
    circuit_signature = " -> ".join(
        [*signal_inputs, result_type]
    )
    input_arguments = " ".join(_clash_name(port.name) for port in inputs)
    circuit_lhs = f"circuit {input_arguments}" if input_arguments else "circuit"
    top_arguments = " ".join(
        name for name in (module.clock, module.reset, input_arguments) if name
    )
    circuit_application = (
        f" {input_arguments}" if input_arguments else ""
    )

    delay_nodes: dict[int, expr.Delay | expr.Pipeline] = {}
    for assignment in (*module.assignments, *module.next_assignments):
        _collect_delays(assignment.expression, delay_nodes)
    for rule in module.rules:
        _collect_delays(rule.guard, delay_nodes)
        for action in rule.actions:
            _collect_delays(action.expression, delay_nodes)
            if action.activation is not None:
                _collect_delays(action.activation, delay_nodes)
    delay_names = {
        _leaf_key(delay): (
            private_names.stage(_stage_prefix(delay), delay.instance, expr.sequential_stage_count(delay))
        )
        for delay in delay_nodes.values()
    }
    delay_names.update(_clash_child_leaf_names(module, private_names))

    ordered_rules = _rule_schedule(module)
    transition = module.resolved_transition
    conditional_state = bool(
        transition is not None and conditional_actions(transition)
    )
    bindings: list[str] = (
        ["reset_active = unsafeToActiveHigh hasReset"] if module.rules else []
    )
    stage_materialized_bindings, render_staged = _clash_stage_materialization(
        tuple(delay_nodes.values()),
        delay_names,
        reserved_names=(
            *(_clash_name(port.name) for port in module.ports),
            *delay_names.values(),
        ),
    )
    bindings.extend(stage_materialized_bindings)
    if module.roms:
        if not bindings:
            bindings.append("reset_active = unsafeToActiveHigh hasReset")
        bindings.append("zlang_rom_reset_hold = register True (pure False)")
        for rom in module.roms:
            bindings.extend(_emit_rom_bindings(rom))

    for instance, child in zip(
        module.instances, module.children, strict=True,
    ):
        child_bindings = {
            item.port: item.expression
            for item in module.instance_bindings
            if item.instance == instance.name
        }
        scalar_inputs = [port for port in child.inputs if port.protocol is InterfaceProtocol.WIRE]
        missing = [port.name for port in scalar_inputs if port.name not in child_bindings]
        if missing:
            raise ClashEmissionError(
                f"instance '{instance.name}' is missing bindings for {', '.join(missing)}"
            )
        applications = [
            _emit_signal_expression(child_bindings[port.name], delay_names)
            for port in scalar_inputs
        ]
        function = _scalar_child_function_for_instance(
            module,
            instance.name,
            scalar_catalog,
            scalar_catalog.root_owner if scalar_catalog is not None else None,
        )
        application = _emit_child_application(
            function, applications, sequential_child=child.is_sequential
        )
        child_outputs = [
            private_names.child_signal(instance.name, port.name)
            for port in child.outputs
        ]
        bindings.append(f"{_emit_product(child_outputs)} = {application}")
    if conditional_state:
        assert transition is not None
        render_state = lambda value: _emit_signal_expression(value, delay_names)
        bindings.extend(_emit_unified_schedule_bindings(module, render_state))
        bindings.extend(_emit_unified_register_bindings(module, render_state))
    else:
        next_by_register = {
            assignment.target.name: assignment.expression
            for assignment in module.next_assignments
        }
        earlier_rules: list[object] = []
        for rule in ordered_rules:
            fire_name = private_names.rule(rule.name)
            raw_guard = _emit_signal_expression(rule.guard, delay_names)
            targets = {action.target.name for action in rule.actions}
            blockers = [
                earlier
                for earlier in earlier_rules
                if targets & {action.target.name for action in earlier.actions}
            ]
            if blockers:
                parameters = " ".join(
                    ("guard", "resetActive", *(f"blocked{index}" for index in range(len(blockers))))
                )
                clear = " && ".join(
                    f"blocked{index} == low" for index in range(len(blockers))
                )
                applications = "".join(
                    f" <*> {private_names.rule(blocker.name)}" for blocker in blockers
                )
                bindings.append(
                    f"{fire_name} = (\\{parameters} -> if not resetActive && guard == high && {clear} "
                    f"then high else low) <$> {_clash_apply_arg(raw_guard)} "
                    f"<*> reset_active{applications}"
                )
            else:
                bindings.append(
                    f"{fire_name} = (\\guard resetActive -> if resetActive then low "
                    f"else guard) <$> {_clash_apply_arg(raw_guard)} <*> reset_active"
                )
            earlier_rules.append(rule)
        for register in module.registers:
            initial = _emit_expression(register.initial)
            bindings.append(
                f"{register.name} = register {initial} ({register.name}_next)"
            )
            next_expression = next_by_register.get(register.name)
            scheduled_expression: expr.Expression = (
                next_expression
                if next_expression is not None
                else expr.RegisterRef(register.name, register.type)
            )
            writers = [
                (rule, action)
                for rule in ordered_rules
                for action in rule.actions
                if action.target.name == register.name
            ]
            for rule, action in reversed(writers):
                scheduled_expression = expr.Mux(
                    expr.InputRef(private_names.rule(rule.name), BitType()),
                    action.expression,
                    scheduled_expression,
                    register.type,
                )
            bindings.append(
                f"{register.name}_next = "
                f"{_emit_signal_expression(scheduled_expression, delay_names)}"
            )

    for delay in delay_nodes.values():
        source = render_staged(delay.expression)
        previous = source
        for stage in range(1, expr.sequential_stage_count(delay) + 1):
            stage_name = private_names.stage(_stage_prefix(delay), delay.instance, stage)
            reset_value = _zero_value(delay.type)
            bindings.append(
                f"{stage_name} = register {reset_value} ({previous})"
            )
            previous = stage_name

    output_values: list[str] = []
    for output in outputs:
        scheduled_output = direct_output_drivers.get(output.name)
        if conditional_state:
            assert transition is not None
            resource = next(
                (
                    item for item in transition.resources
                    if item.kind is StateResourceKind.OUTPUT
                    and item.name == output.name
                ),
                None,
            )
            output_writers = (
                [
                    (group, action)
                    for group in ordered_state_groups(transition)
                    for action in group.actions
                    if action.resource_id == resource.semantic_id
                    and action.kind is StateActionKind.OUTPUT_WRITE
                ]
                if resource is not None else []
            )
        else:
            output_writers = [
                (rule, action)
                for rule in ordered_rules
                for action in rule.actions
                if action.target.name == output.name
            ]
        if scheduled_output is None:
            if not output_writers:
                raise ClashEmissionError(
                    f"ordinary scalar output '{output.name}' has no driver"
                )
            scheduled_output = expr.Constant(0, output.type)
        for rule, action in reversed(output_writers):
            scheduled_output = expr.Mux(
                expr.InputRef(
                    (
                        _unified_action_enable(transition, rule, action, private_names)
                        if conditional_state and transition is not None
                        else private_names.rule(rule.name)
                    ),
                    BitType(),
                ),
                (
                    action.operands[0]
                    if conditional_state else action.expression
                ),
                scheduled_output,
                output.type,
            )
        output_expression = _emit_signal_expression(
            scheduled_output, delay_names
        )
        output_name = _clash_name(output.name)
        bindings.append(f"{output_name} = {output_expression}")
        output_values.append(output_name)
    result_value = _emit_scalar_product(output_values)
    where_block = "\n".join(f"  {binding}" for binding in bindings)
    input_ports = ", ".join(
        (
            f'PortName "{module.clock}"',
            f'PortName "{module.reset}"',
            *(
                _top_value_port_annotation(port.name, port.type)
                for port in inputs
            ),
        )
    )

    return f'''{extensions}
module {module.name} where

{imports}
{pipeline_notes}{declarations}

circuit :: HiddenClockResetEnable {domain} => {circuit_signature}
{circuit_lhs} = {result_value}
 where
{where_block}

topEntity :: {top_signature}
topEntity {top_arguments} = exposeClockResetEnable circuit {module.clock} {_top_reset_expression(module)} enableGen{circuit_application}

{{-# ANN topEntity
  (Synthesize
    {{ t_name = "{module.name}"
    , t_inputs = [{input_ports}]
    , t_output = {_top_scalar_output_annotation(outputs)}
    }}) #-}}
'''


def _rule_schedule(module: Module) -> list[object]:
    """Return rules from highest to lowest explicit priority."""

    remaining = {rule.name: rule for rule in module.rules}
    edges = {(item.higher, item.lower) for item in module.rule_priorities}
    ordered: list[object] = []
    while remaining:
        ready = sorted(
            name
            for name in remaining
            if not any(lower == name and higher in remaining for higher, lower in edges)
        )
        if not ready:
            raise ClashEmissionError("rule priority graph contains a cycle")
        for name in ready:
            ordered.append(remaining.pop(name))
    return ordered


def _emit_signal_expression(
    expression: expr.Expression,
    delay_names: dict[tuple[object, ...], str],
) -> str:
    if isinstance(expression, expr.InstanceOutputRef):
        return delay_names.get(
            _leaf_key(expression),
            f"{_clash_instance_name(expression.instance)}_{_clash_name(expression.port)}",
        )
    if isinstance(expression, (expr.InputRef, expr.RegisterRef)):
        return delay_names.get(_leaf_key(expression), _clash_name(expression.name))
    if isinstance(expression, expr.FifoRef):
        return f"{expression.fifo}_{expression.signal.value}"
    if isinstance(expression, expr.MemoryRef):
        return f"{expression.memory}_{expression.signal.value}"
    if isinstance(expression, expr.RomRef):
        if expression.signal is not RomSignal.READ_DATA:
            raise ClashEmissionError(
                "ROM read_address is a driven storage input, not a readable value"
            )
        return f"{expression.rom}_read_data"
    if isinstance(expression, (expr.Delay, expr.Pipeline)):
        return delay_names[_leaf_key(expression)]
    leaves = _signal_leaves(expression, delay_names)
    if not leaves:
        return f"pure ({_emit_expression(expression)})"
    substitutions = {
        key: f"value_{index}" for index, (key, _) in enumerate(leaves)
    }
    parameters = " ".join(substitutions[key] for key, _ in leaves)
    rendered = _emit_expression(expression, substitutions)
    applications = "".join(
        (" <$> " if index == 0 else " <*> ") + signal
        for index, (_, signal) in enumerate(leaves)
    )
    return f"(\\{parameters} -> {rendered}){applications}"


def _signal_leaves(
    expression: expr.Expression,
    delay_names: dict[tuple[object, ...], str],
) -> list[tuple[tuple[object, ...], str]]:
    found: list[tuple[tuple[object, ...], str]] = []

    def visit(node: expr.Expression) -> None:
        if isinstance(node, (expr.InputRef, expr.RegisterRef)):
            # Keep leaf spelling consistent with the function/top argument.
            # Legal ZLang names such as ``data`` are reserved Haskell words
            # and are declared as ``data_zlang`` by ``_clash_name``.
            key = _leaf_key(node)
            found.append((key, delay_names.get(key, _clash_name(node.name))))
        elif isinstance(node, expr.InstanceOutputRef):
            found.append(
                (_leaf_key(node), delay_names.get(
                    _leaf_key(node),
                    f"{_clash_instance_name(node.instance)}_{_clash_name(node.port)}",
                ))
            )
        elif isinstance(node, expr.ReadyValidRef):
            found.append((_leaf_key(node), _ready_valid_reference_name(node)))
        elif isinstance(node, expr.CreditRef):
            found.append((_leaf_key(node), _credit_reference_name(node)))
        elif isinstance(node, expr.PacketRef):
            found.append((_leaf_key(node), _packet_reference_name(node)))
        elif isinstance(node, expr.VirtualChannelCreditRef):
            found.append((_leaf_key(node), _vc_credit_reference_name(node)))
        elif isinstance(node, expr.RequestResponseRef):
            found.append((_leaf_key(node), _request_response_reference_name(node)))
        elif isinstance(node, expr.FifoRef):
            found.append(
                (_leaf_key(node), f"{node.fifo}_{node.signal.value}")
            )
        elif isinstance(node, expr.MemoryRef):
            found.append(
                (_leaf_key(node), f"{node.memory}_{node.signal.value}")
            )
        elif isinstance(node, expr.RomRef):
            if node.signal is not RomSignal.READ_DATA:
                raise ClashEmissionError(
                    "ROM read_address is a driven storage input, not a readable value"
                )
            found.append((_leaf_key(node), f"{node.rom}_read_data"))
        elif isinstance(node, expr.ParameterRef):
            raise ClashEmissionError("function parameter escaped into module expression")
        elif isinstance(node, (expr.Delay, expr.Pipeline)):
            key = _leaf_key(node)
            found.append((key, delay_names[key]))
        elif isinstance(node, expr.Constant):
            return
        elif isinstance(node, (expr.EnumEncode, expr.EnumValid)):
            visit(node.expression)
        elif isinstance(node, expr.EnumDecode):
            visit(node.expression)
            visit(node.fallback)
        elif isinstance(node, (expr.UnionTag, expr.UnionField)):
            visit(node.expression)
        elif isinstance(node, (expr.Add, expr.Binary)):
            visit(node.left)
            visit(node.right)
        elif isinstance(
            node,
            (
                expr.Extend,
                expr.Truncate,
                expr.FixedConvert,
                expr.FieldAccess,
                expr.TupleProject,
                expr.VectorIndex,
                expr.Slice,
                expr.Bitcast,
                expr.Reshape,
                expr.Pack,
                expr.Unpack,
            ),
        ):
            visit(node.expression)
        elif isinstance(node, (expr.Concat, expr.VectorConcat)):
            for operand in node.operands:
                visit(operand)
        elif isinstance(node, expr.RuntimeIndex):
            visit(node.expression)
            visit(node.index)
        elif isinstance(node, expr.VectorUpdate):
            visit(node.expression)
            visit(node.index)
            visit(node.value)
        elif isinstance(node, expr.Mux):
            visit(node.condition)
            visit(node.when_true)
            visit(node.when_false)
        elif isinstance(node, expr.StructConstruct):
            for _, value in node.fields:
                visit(value)
        elif isinstance(node, expr.TupleConstruct):
            for value in node.elements:
                visit(value)
        elif isinstance(node, expr.UnionConstruct):
            for _, value in node.fields:
                visit(value)
        elif isinstance(node, expr.Switch):
            visit(node.selector)
            for case in node.cases:
                visit(case.expression)
            visit(node.default)
        elif isinstance(node, expr.Call):
            for argument in node.arguments:
                visit(argument)
        elif isinstance(node, (expr.Generate, expr.Map)):
            for element in node.elements:
                visit(element)
        elif isinstance(node, expr.FunctionalRegion):
            for element in materialize_functional_region(node):
                visit(element)
        elif isinstance(node, expr.Dot):
            visit(node.left)
            visit(node.right)
        elif isinstance(node, expr.Reduce):
            visit(
                materialize_exact_reduction(node)
                if node.plan is not None
                else node.collection
            )
        elif isinstance(node, expr.ImplementationChoice):
            visit(node.selected_alternative.expression)
        else:
            raise ClashEmissionError(f"cannot collect signals from {node!r}")

    visit(expression)
    unique: dict[tuple[object, ...], str] = {}
    for key, signal in found:
        unique.setdefault(key, signal)
    return list(unique.items())


def _collect_delays(
    expression: expr.Expression,
    found: dict[int, expr.Delay | expr.Pipeline],
) -> None:
    if isinstance(expression, (expr.Delay, expr.Pipeline)):
        _collect_delays(expression.expression, found)
        found.setdefault(expression.instance, expression)
    elif isinstance(expression, (expr.EnumEncode, expr.EnumValid)):
        _collect_delays(expression.expression, found)
    elif isinstance(expression, expr.EnumDecode):
        _collect_delays(expression.expression, found)
        _collect_delays(expression.fallback, found)
    elif isinstance(expression, (expr.UnionTag, expr.UnionField)):
        _collect_delays(expression.expression, found)
    elif isinstance(expression, (expr.Add, expr.Binary)):
        _collect_delays(expression.left, found)
        _collect_delays(expression.right, found)
    elif isinstance(
        expression,
        (
            expr.Extend,
            expr.Truncate,
            expr.FixedConvert,
            expr.FieldAccess,
            expr.TupleProject,
            expr.VectorIndex,
            expr.Slice,
            expr.Bitcast,
            expr.Reshape,
            expr.Pack,
            expr.Unpack,
        ),
    ):
        _collect_delays(expression.expression, found)
    elif isinstance(expression, (expr.Concat, expr.VectorConcat)):
        for operand in expression.operands:
            _collect_delays(operand, found)
    elif isinstance(expression, expr.RuntimeIndex):
        _collect_delays(expression.expression, found)
        _collect_delays(expression.index, found)
    elif isinstance(expression, expr.VectorUpdate):
        _collect_delays(expression.expression, found)
        _collect_delays(expression.index, found)
        _collect_delays(expression.value, found)
    elif isinstance(expression, expr.Mux):
        _collect_delays(expression.condition, found)
        _collect_delays(expression.when_true, found)
        _collect_delays(expression.when_false, found)
    elif isinstance(expression, expr.Switch):
        _collect_delays(expression.selector, found)
        for case in expression.cases:
            _collect_delays(case.expression, found)
        _collect_delays(expression.default, found)
    elif isinstance(expression, (expr.StructConstruct, expr.UnionConstruct)):
        for _, value in expression.fields:
            _collect_delays(value, found)
    elif isinstance(expression, expr.TupleConstruct):
        for value in expression.elements:
            _collect_delays(value, found)
    elif isinstance(expression, expr.Call):
        for argument in expression.arguments:
            _collect_delays(argument, found)
    elif isinstance(expression, (expr.Generate, expr.Map)):
        for element in expression.elements:
            _collect_delays(element, found)
    elif isinstance(expression, expr.FunctionalRegion):
        for element in materialize_functional_region(expression):
            _collect_delays(element, found)
    elif isinstance(expression, expr.Dot):
        _collect_delays(expression.left, found)
        _collect_delays(expression.right, found)
    elif isinstance(expression, expr.Reduce):
        _collect_delays(
            materialize_exact_reduction(expression)
            if expression.plan is not None
            else expression.collection,
            found,
        )
    elif isinstance(expression, expr.ImplementationChoice):
        _collect_delays(expression.selected_alternative.expression, found)


def _leaf_key(expression: expr.Expression) -> tuple[object, ...]:
    if isinstance(expression, (expr.Delay, expr.Pipeline)):
        return (_stage_prefix(expression), expression.instance)
    if isinstance(expression, (expr.InputRef, expr.ParameterRef, expr.RegisterRef)):
        return (type(expression).__name__, expression.name)
    if isinstance(expression, expr.InstanceOutputRef):
        return (type(expression).__name__, expression.instance, expression.port)
    if isinstance(expression, expr.ReadyValidRef):
        return (
            type(expression).__name__,
            expression.interface,
            expression.signal.value,
        )
    if isinstance(expression, expr.CreditRef):
        return (
            type(expression).__name__,
            expression.interface,
            expression.signal.value,
        )
    if isinstance(expression, (expr.PacketRef, expr.VirtualChannelCreditRef)):
        return (
            type(expression).__name__,
            expression.interface,
            expression.signal.value,
        )
    if isinstance(expression, expr.RequestResponseRef):
        return (
            type(expression).__name__,
            expression.interface,
            expression.channel.value,
            expression.signal.value,
        )
    if isinstance(expression, expr.FifoRef):
        return (type(expression).__name__, expression.fifo, expression.signal.value)
    if isinstance(expression, expr.MemoryRef):
        return (
            type(expression).__name__,
            expression.memory,
            expression.signal.value,
        )
    if isinstance(expression, expr.RomRef):
        return (
            type(expression).__name__,
            expression.rom,
            expression.signal.value,
        )
    raise ClashEmissionError(f"expression is not a signal leaf: {expression!r}")


def _credit_reference_name(expression: expr.CreditRef) -> str:
    return f"{expression.interface}_{expression.signal.value}"


def _packet_reference_name(expression: expr.PacketRef) -> str:
    return f"{expression.interface}_{expression.signal.value}"


def _vc_credit_reference_name(expression: expr.VirtualChannelCreditRef) -> str:
    return f"{expression.interface}_{expression.signal.value}"


def _ready_valid_reference_name(expression: expr.ReadyValidRef) -> str:
    if expression.signal is ReadyValidSignal.TRANSFER:
        return f"{expression.interface}_transfer"
    return f"{expression.interface}_{expression.signal.value}"


def _request_response_reference_name(
    expression: expr.RequestResponseRef,
) -> str:
    return (
        f"{expression.interface}_{expression.channel.value}_"
        f"{expression.signal.value}"
    )


def _zero_value(type_: HardwareType) -> str:
    if isinstance(type_, BitType):
        return "low"
    if isinstance(type_, EnumType):
        return f"({type_.codes[0]} :: {_emit_type(type_)})"
    if isinstance(type_, (UIntType, SIntType, BitsType, FixedType, UFixedType)):
        return f"(0 :: {_emit_type(type_)})"
    if isinstance(type_, (StructType, TaggedUnionType, VecType)):
        return (
            f"(unpack (0 :: BitVector {type_.width}) :: {_emit_type(type_)})"
        )
    if isinstance(type_, TupleType):
        return "(" + ", ".join(_zero_value(item) for item in type_.elements) + ")"
    raise ClashEmissionError(f"no reset value for delayed {type_}")


def _stage_prefix(expression: expr.Delay | expr.Pipeline) -> str:
    return "delay" if isinstance(expression, expr.Delay) else "pipeline"


def _emit_expression(
    expression: expr.Expression,
    names: dict[tuple[object, ...], str] | None = None,
) -> str:
    if isinstance(expression, (expr.InputRef, expr.ParameterRef, expr.RegisterRef)):
        if names is not None and (key := _leaf_key(expression)) in names:
            return names[key]
        return _clash_name(expression.name)
    if isinstance(expression, expr.InstanceOutputRef):
        if names is not None and (key := _leaf_key(expression)) in names:
            return names[key]
        return (
            f"{_clash_instance_name(expression.instance)}_"
            f"{_clash_name(expression.port)}"
        )
    if isinstance(expression, expr.ReadyValidRef):
        if names is not None and (key := _leaf_key(expression)) in names:
            return names[key]
        if expression.signal is ReadyValidSignal.TRANSFER:
            return (
                f"(({expression.interface}_valid) .&. "
                f"({expression.interface}_ready))"
            )
        return f"{expression.interface}_{expression.signal.value}"
    if isinstance(expression, expr.CreditRef):
        if names is not None and (key := _leaf_key(expression)) in names:
            return names[key]
        return _credit_reference_name(expression)
    if isinstance(expression, expr.PacketRef):
        if names is not None and (key := _leaf_key(expression)) in names:
            return names[key]
        return _packet_reference_name(expression)
    if isinstance(expression, expr.VirtualChannelCreditRef):
        if names is not None and (key := _leaf_key(expression)) in names:
            return names[key]
        return _vc_credit_reference_name(expression)
    if isinstance(expression, expr.RequestResponseRef):
        if names is not None and (key := _leaf_key(expression)) in names:
            return names[key]
        return _request_response_reference_name(expression)
    if isinstance(expression, expr.FifoRef):
        if names is not None and (key := _leaf_key(expression)) in names:
            return names[key]
        return f"{expression.fifo}_{expression.signal.value}"
    if isinstance(expression, expr.MemoryRef):
        if names is not None and (key := _leaf_key(expression)) in names:
            return names[key]
        return f"{expression.memory}_{expression.signal.value}"
    if isinstance(expression, expr.RomRef):
        if expression.signal is not RomSignal.READ_DATA:
            raise ClashEmissionError(
                "ROM read_address is a driven storage input, not a readable value"
            )
        if names is not None and (key := _leaf_key(expression)) in names:
            return names[key]
        return f"{expression.rom}_read_data"
    if isinstance(expression, (expr.Delay, expr.Pipeline)):
        if names is not None and (key := _leaf_key(expression)) in names:
            return names[key]
        raise ClashEmissionError("staged expression used outside a sequential circuit")
    if isinstance(expression, expr.Constant):
        if isinstance(expression.type, BitType):
            return "high" if expression.value else "low"
        return f"({expression.value} :: {_emit_type(expression.type)})"
    if isinstance(expression, expr.EnumEncode):
        value = _emit_expression(expression.expression, names)
        return f"(pack ({value}) :: {_emit_type(expression.type)})"
    if isinstance(expression, expr.EnumValid):
        value = _emit_expression(expression.expression, names)
        comparisons = " || ".join(
            f"(({value}) == ({code} :: {_emit_type(expression.expression.type)}))"
            for code in expression.enum_type.codes
        )
        return f"boolToBit ({comparisons})"
    if isinstance(expression, expr.EnumDecode):
        value = _emit_expression(expression.expression, names)
        comparisons = " || ".join(
            f"(({value}) == ({code} :: {_emit_type(expression.expression.type)}))"
            for code in expression.type.codes
        )
        fallback = _emit_expression(expression.fallback, names)
        decoded = f"(unpack ({value}) :: {_emit_type(expression.type)})"
        return f"(if {comparisons} then {decoded} else ({fallback}))"
    if isinstance(expression, expr.UnionConstruct):
        union_type = expression.type
        terms = [
            f"(({union_type.tag(expression.variant)} :: BitVector {union_type.width}) `shiftL` {union_type.payload_width})"
        ]
        variant = union_type.variant(expression.variant)
        assert variant is not None
        offset = union_type.payload_width
        for field, (_, value) in zip(variant.fields, expression.fields, strict=True):
            offset -= field.type.width
            rendered = _emit_expression(value, names)
            terms.append(
                f"((resize (pack ({rendered})) :: BitVector {union_type.width}) `shiftL` {offset})"
            )
        return "(" + " .|. ".join(terms) + ")"
    if isinstance(expression, expr.UnionTag):
        union_type = expression.expression.type
        assert isinstance(union_type, TaggedUnionType)
        rendered = _emit_expression(expression.expression, names)
        return (
            f"(resize (shiftR ({rendered}) {union_type.payload_width}) "
            f":: BitVector {union_type.tag_width})"
        )
    if isinstance(expression, expr.UnionField):
        union_type = expression.expression.type
        assert isinstance(union_type, TaggedUnionType)
        variant = union_type.variant(expression.variant)
        assert variant is not None
        offset = union_type.payload_width
        for field in variant.fields:
            offset -= field.type.width
            if field.name == expression.field:
                rendered = _emit_expression(expression.expression, names)
                raw = (
                    f"(resize (shiftR ({rendered}) {offset}) "
                    f":: BitVector {field.type.width})"
                )
                return f"(unpack {raw} :: {_emit_type(field.type)})"
        raise ClashEmissionError("invalid tagged-union field projection")
    if isinstance(expression, expr.Add):
        left = _emit_resized(expression.left, expression.type, names)
        right = _emit_resized(expression.right, expression.type, names)
        return f"{left} + {right}"
    if isinstance(expression, expr.Binary):
        left = _emit_at_type(expression.left, expression.operand_type, names)
        if expression.operator in {
            expr.BinaryOperator.SHIFT_LEFT,
            expr.BinaryOperator.SHIFT_RIGHT,
        }:
            function = (
                "shiftL"
                if expression.operator is expr.BinaryOperator.SHIFT_LEFT
                else "shiftR"
            )
            right = _emit_expression(expression.right, names)
            return f"{function} {left} (fromIntegral ({right}))"
        right = _emit_at_type(expression.right, expression.operand_type, names)
        if expression.operator is expr.BinaryOperator.BIT_XOR:
            return f"xor {left} {right}"
        operator = {
            expr.BinaryOperator.SUBTRACT: "-",
            expr.BinaryOperator.MULTIPLY: "*",
            expr.BinaryOperator.BIT_AND: ".&.",
            expr.BinaryOperator.BIT_OR: ".|.",
            expr.BinaryOperator.EQUAL: "==",
            expr.BinaryOperator.NOT_EQUAL: "/=",
            expr.BinaryOperator.LESS: "<",
            expr.BinaryOperator.LESS_EQUAL: "<=",
            expr.BinaryOperator.GREATER: ">",
            expr.BinaryOperator.GREATER_EQUAL: ">=",
        }[expression.operator]
        rendered = f"{left} {operator} {right}"
        if isinstance(expression.type, BitType) and expression.operator in {
            expr.BinaryOperator.EQUAL,
            expr.BinaryOperator.NOT_EQUAL,
            expr.BinaryOperator.LESS,
            expr.BinaryOperator.LESS_EQUAL,
            expr.BinaryOperator.GREATER,
            expr.BinaryOperator.GREATER_EQUAL,
        }:
            return f"boolToBit ({rendered})"
        return rendered
    if isinstance(expression, (expr.Extend, expr.Truncate)):
        return _emit_resized(expression.expression, expression.type, names)
    if isinstance(expression, expr.FixedConvert):
        operand = _emit_expression(expression.expression, names)
        target = _emit_type(expression.type)
        if expression.kind in {expr.FixedConversionKind.FROM_RAW, expr.FixedConversionKind.TO_RAW}:
            return f"(resize ({operand}) :: {target})"
        target_signed = isinstance(expression.type, FixedType)
        if expression.rational_denominator is not None:
            assert isinstance(expression.expression, expr.Constant)
            constant = quantize_rational(
                expression.expression.value,
                expression.rational_denominator,
                fraction=expression.type.fraction,
                width=expression.type.width,
                signed=target_signed,
                rounding=expression.rounding,
                overflow=expression.overflow,
            )
            return f"({constant} :: {target})"
        source_fraction = getattr(expression.expression.type, "fraction", 0)
        delta = expression.type.fraction - source_fraction
        source_signed = isinstance(expression.expression.type, (SIntType, FixedType))
        arithmetic_signed = source_signed or target_signed
        intermediate_width = max(
            expression.expression.type.width + max(delta, 0) + 2,
            expression.type.width + 2,
        )
        intermediate_type = f"{'Signed' if arithmetic_signed else 'Unsigned'} {intermediate_width}"
        value = f"(resize ({operand}) :: {intermediate_type})"
        if delta > 0:
            converted = f"shiftL {value} {delta}"
        elif delta == 0:
            converted = value
        else:
            shift = -delta
            magnitude = f"(if {value} < 0 then negate {value} else {value})" if source_signed else value
            quotient = f"shiftR {magnitude} {shift}"
            discarded = f"(({magnitude} .&. {(1 << shift) - 1}) /= 0)"
            if expression.rounding is expr.FixedRounding.NEAREST_EVEN:
                rounded = (
                    f"shiftR ({magnitude} + {(1 << (shift - 1)) - 1} + "
                    f"(shiftR {magnitude} {shift} .&. 1)) {shift}"
                )
                converted = f"(if {value} < 0 then negate ({rounded}) else ({rounded}))" if source_signed else rounded
            elif expression.rounding is expr.FixedRounding.AWAY_ZERO:
                rounded = f"({quotient} + (if {discarded} then 1 else 0))"
                converted = f"(if {value} < 0 then negate ({rounded}) else ({rounded}))" if source_signed else rounded
            elif expression.rounding is expr.FixedRounding.FLOOR and source_signed:
                converted = f"(if {value} < 0 then negate ({quotient} + (if {discarded} then 1 else 0)) else ({quotient}))"
            else:
                converted = f"(if {value} < 0 then negate ({quotient}) else ({quotient}))" if source_signed else quotient
        if expression.overflow is expr.FixedOverflow.SATURATE:
            minimum = -(1 << (expression.type.width - 1)) if target_signed else 0
            maximum = ((1 << (expression.type.width - 1)) - 1 if target_signed
                       else (1 << expression.type.width) - 1)
            converted = f"max ({minimum}) (min ({maximum}) ({converted}))"
        return f"(resize ({converted}) :: {target})"
    if isinstance(expression, expr.Mux):
        condition = _emit_expression(expression.condition, names)
        when_true = _emit_expression(expression.when_true, names)
        when_false = _emit_expression(expression.when_false, names)
        return (
            f"if ({condition}) == high then ({when_true}) "
            f"else ({when_false})"
        )
    if isinstance(expression, expr.Switch):
        selector = _emit_expression(expression.selector, names)
        alternatives = "; ".join(
            f"{case.key} -> {_emit_expression(case.expression, names)}"
            for case in expression.cases
        )
        default = _emit_expression(expression.default, names)
        return f"case {selector} of {{ {alternatives}; _ -> {default} }}"
    if isinstance(expression, expr.Call):
        arguments = " ".join(
            f"({_emit_expression(argument, names)})" for argument in expression.arguments
        )
        return f"{expression.function} {arguments}".rstrip()
    if isinstance(expression, (expr.Generate, expr.Map)):
        elements = " :> ".join(
            f"({_emit_expression(element, names)})"
            for element in expression.elements
        )
        return f"({elements} :> Nil)"
    if isinstance(expression, expr.FunctionalRegion):
        elements = " :> ".join(
            f"({_emit_expression(element, names)})"
            for element in materialize_functional_region(expression)
        )
        return f"({elements} :> Nil)"
    if isinstance(expression, expr.Dot):
        products = " :> ".join(
            f"({_emit_expression(product, names)})"
            for product in expression.products
        )
        return f"({products} :> Nil)"
    if isinstance(expression, expr.Reduce):
        return _emit_expression(lower_reduction(expression), names)
    if isinstance(expression, expr.ImplementationChoice):
        selected = expression.selected_alternative
        rendered = _emit_expression(selected.expression, names)
        if selected.kind is expr.ImplementationKind.DSP_MAC:
            return f"(let zlangDspMac = ({rendered}) in zlangDspMac)"
        return rendered
    if isinstance(expression, expr.FieldAccess):
        aggregate = _emit_expression(expression.expression, names)
        if not isinstance(expression.expression.type, StructType):
            raise ClashEmissionError("field access base is not a struct")
        accessor = _field_accessor(expression.expression.type.name, expression.field)
        return f"{accessor} ({aggregate})"
    if isinstance(expression, expr.StructConstruct):
        values = " ".join(
            f"({_emit_expression(value, names)})" for _, value in expression.fields
        )
        return f"({_clash_struct_name(expression.struct_name)} {values})"
    if isinstance(expression, expr.TupleConstruct):
        return "(" + ", ".join(
            _emit_expression(value, names) for value in expression.elements
        ) + ")"
    if isinstance(expression, expr.TupleProject):
        tuple_type = expression.expression.type
        if not isinstance(tuple_type, TupleType):
            raise ClashEmissionError("tuple projection base is not a tuple")
        binders = ", ".join(
            f"zlangTuple{item}" for item in range(len(tuple_type.elements))
        )
        aggregate = _emit_expression(expression.expression, names)
        return f"((\\({binders}) -> zlangTuple{expression.index}) ({aggregate}))"
    if isinstance(expression, expr.VectorIndex):
        vector = _emit_expression(expression.expression, names)
        if not isinstance(expression.expression.type, VecType):
            raise ClashEmissionError("vector index base is not a vector")
        length = expression.expression.type.length
        return f"({vector}) !! ({expression.index} :: Index {length})"
    if isinstance(expression, expr.RuntimeIndex):
        vector = _emit_expression(expression.expression, names)
        if not isinstance(expression.expression.type, VecType):
            raise ClashEmissionError("runtime vector index base is not a vector")
        length = expression.expression.type.length
        index = _emit_expression(expression.index, names)
        return _emit_runtime_vector_selector(vector, index, length)
    if isinstance(expression, expr.VectorUpdate):
        vector = _emit_expression(expression.expression, names)
        if not isinstance(expression.expression.type, VecType):
            raise ClashEmissionError("vector update base is not a vector")
        length = expression.expression.type.length
        index = _emit_expression(expression.index, names)
        value = _emit_expression(expression.value, names)
        return _emit_runtime_vector_update(vector, index, value, length)
    if isinstance(expression, expr.Slice):
        value = _emit_expression(expression.expression, names)
        return f"(slice d{expression.msb} d{expression.lsb} (pack ({value})))"
    if isinstance(expression, expr.Concat):
        packed = [
            f"(pack ({_emit_expression(operand, names)}))"
            for operand in expression.operands
        ]
        if len(packed) < 2:
            raise ClashEmissionError("typed concat requires at least two operands")
        rendered = packed[0]
        for operand in packed[1:]:
            rendered = f"({rendered} ++# {operand})"
        return rendered
    if isinstance(expression, expr.VectorConcat):
        rendered = [
            f"({_emit_expression(operand, names)})"
            for operand in expression.operands
        ]
        if len(rendered) < 2:
            raise ClashEmissionError(
                "typed vector concat requires at least two operands"
            )
        result = rendered[0]
        for operand in rendered[1:]:
            result = f"({result} ++ {operand})"
        return result
    if isinstance(expression, expr.Reshape):
        source_type = expression.expression.type
        target_type = expression.type
        if not isinstance(source_type, VecType) or not isinstance(target_type, VecType):
            raise ClashEmissionError("reshape requires vector source and target types")
        source = _emit_expression(expression.expression, names)
        source_name = "zlangReshapeSource"
        leaves = _flatten_clash_vector(source_name, source_type)
        rebuilt, consumed = _rebuild_clash_vector(target_type, leaves, 0)
        if consumed != len(leaves):
            raise ClashEmissionError("reshape did not consume every source vector leaf")
        # A lambda keeps the source expression outside the temporary binder's
        # scope.  This remains correct even when a legal ZLang input happens
        # to be named ``zlangReshapeSource``.
        return f"((\\{source_name} -> {rebuilt}) ({source}))"
    if isinstance(expression, expr.Bitcast):
        value = _emit_expression(expression.expression, names)
        return f"(unpack (pack ({value})) :: {_emit_type(expression.type)})"
    if isinstance(expression, expr.Pack):
        value = _emit_expression(expression.expression, names)
        return f"(pack ({value}))"
    if isinstance(expression, expr.Unpack):
        value = _emit_expression(expression.expression, names)
        return f"(unpack ({value}) :: {_emit_type(expression.type)})"
    raise ClashEmissionError(f"unsupported IR expression {expression!r}")


def _flatten_clash_vector(value: str, type_: VecType) -> list[str]:
    """Flatten nested Clash Vec values in ZLang outer-to-inner order."""

    leaves: list[str] = []
    for index in range(type_.length):
        element = f"(({value}) !! ({index} :: Index {type_.length}))"
        if isinstance(type_.element_type, VecType):
            leaves.extend(_flatten_clash_vector(element, type_.element_type))
        else:
            leaves.append(element)
    return leaves


def _emit_runtime_vector_selector(vector: str, index: str, length: int) -> str:
    """Select a runtime Vec element without leaking a machine-width index.

    Clash's dynamic ``Vec`` indexing primitive currently widens even an exact
    ``Index n`` to a machine-sized RTL array selector.  Verilator correctly
    reports that generated selector as a width truncation.  A typed mux over
    literal ``Index n`` leaves preserves the already range-proven ZLang index,
    evaluates the vector expression once, and keeps every physical selector
    compile-time exact.
    """

    if length <= 0:
        raise ClashEmissionError("runtime vector index requires a non-empty vector")
    vector_name = "zlangRuntimeVector"
    index_name = "zlangRuntimeIndex"
    selected = f"{vector_name} !! ({length - 1} :: Index {length})"
    for item in reversed(range(length - 1)):
        selected = (
            f"if {index_name} == {item} then "
            f"{vector_name} !! ({item} :: Index {length}) else ({selected})"
        )
    return (
        f"((\\{vector_name} {index_name} -> {selected}) "
        f"({vector}) ({index}))"
    )


def _emit_runtime_vector_update(
    vector: str,
    index: str,
    value: str,
    length: int,
) -> str:
    """Replace one range-proven Vec element without a widened RTL selector."""

    if length <= 0:
        raise ClashEmissionError("vector update requires a non-empty vector")
    vector_name = "zlangUpdateVector"
    index_name = "zlangUpdateIndex"
    value_name = "zlangUpdateValue"
    updated = (
        f"replace ({length - 1} :: Index {length}) "
        f"{value_name} {vector_name}"
    )
    for item in reversed(range(length - 1)):
        updated = (
            f"if {index_name} == {item} then "
            f"replace ({item} :: Index {length}) {value_name} {vector_name} "
            f"else ({updated})"
        )
    return (
        f"((\\{vector_name} {index_name} {value_name} -> {updated}) "
        f"({vector}) ({index}) ({value}))"
    )


def _rebuild_clash_vector(
    type_: VecType,
    leaves: list[str],
    offset: int,
) -> tuple[str, int]:
    """Build a nested Clash Vec from a logical leaf sequence."""

    elements: list[str] = []
    if isinstance(type_.element_type, VecType):
        for _ in range(type_.length):
            element, offset = _rebuild_clash_vector(
                type_.element_type, leaves, offset
            )
            elements.append(element)
    else:
        end = offset + type_.length
        if end > len(leaves):
            raise ClashEmissionError(
                "reshape target requires more leaves than its source"
            )
        elements.extend(leaves[offset:end])
        offset = end
    return "(" + " :> ".join(f"({item})" for item in elements) + " :> Nil)", offset


def _emit_resized(
    expression: expr.Expression,
    target: HardwareType,
    names: dict[tuple[object, ...], str] | None = None,
) -> str:
    rendered = _emit_expression(expression, names)
    return f"(resize ({rendered}) :: {_emit_type(target)})"


def _emit_at_type(
    expression: expr.Expression,
    target: HardwareType,
    names: dict[tuple[object, ...], str] | None = None,
) -> str:
    rendered = _emit_expression(expression, names)
    if expression.type == target:
        return f"({rendered})"
    return f"(resize ({rendered}) :: {_emit_type(target)})"


def _emit_type(type_: HardwareType) -> str:
    if isinstance(type_, BitType):
        return "Bit"
    if isinstance(type_, UIntType):
        return f"Unsigned {type_.width}"
    if isinstance(type_, SIntType):
        return f"Signed {type_.width}"
    if isinstance(type_, FixedType):
        return f"Signed {type_.width}"
    if isinstance(type_, UFixedType):
        return f"Unsigned {type_.width}"
    if isinstance(type_, BitsType):
        return f"BitVector {type_.width}"
    if isinstance(type_, EnumType):
        return f"Unsigned {type_.width}"
    if isinstance(type_, TaggedUnionType):
        return f"BitVector {type_.width}"
    if isinstance(type_, StructType):
        return _clash_struct_name(type_.name)
    if isinstance(type_, TupleType):
        return "(" + ", ".join(_emit_type(item) for item in type_.elements) + ")"
    if isinstance(type_, VecType):
        return f"Vec {type_.length} ({_emit_type(type_.element_type)})"
    raise ClashEmissionError(f"unsupported hardware type {type_!r}")


def _emit_struct(type_: StructType, *, derive_bitpack: bool = False) -> str:
    name = _clash_struct_name(type_.name)
    fields = "\n  , ".join(
        f"{_field_accessor(name, field.name)} :: {_emit_type(field.type)}"
        for field in type_.fields
    )
    deriving = "Generic, NFDataX, Show, Eq"
    if derive_bitpack:
        deriving += ", BitPack"
    return (
        f"data {name} = {name}\n"
        f"  {{ {fields}\n"
        f"  }} deriving ({deriving})\n"
    )


def _emit_function(function: Function) -> str:
    signature_parts = [_emit_type(parameter.type) for parameter in function.parameters]
    signature_parts.append(_emit_type(function.return_type))
    signature = " -> ".join(signature_parts)
    # Parameter references are rendered through ``_clash_name`` below. Apply
    # the identical mapping at the helper ABI boundary so legal ZLang names
    # such as ``low``, ``high`` and ``data`` cannot become free variables in a
    # generated monomorphic helper.
    parameter_names = tuple(
        _clash_name(parameter.name) for parameter in function.parameters
    )
    parameters = " ".join(parameter_names)
    materialized = plan_materialization(
        (function.body,),
        reserved_names=(
            function.name,
            *parameter_names,
        ),
        generated_prefix="zlang_fn_expr_",
    )
    aliases = {item.expression: item.name for item in materialized}
    body = _emit_expression(replace_materialized(function.body, aliases))
    if not materialized:
        return f"{function.name} :: {signature}\n{function.name} {parameters} = {body}\n"

    # Explicit local signatures retain exact widths/signedness and prevent
    # aggregate constants or packed values from becoming ambiguously
    # polymorphic.  Haskell ``where`` bindings are mutually visible; reverse
    # the planner's parent-first DFS order anyway so the generated source reads
    # in dependency-first order, matching the SystemVerilog helper ABI.
    bindings: list[str] = []
    for item in reversed(materialized):
        rewritten = replace_materialized(
            item.expression,
            aliases,
            keep=item.expression,
        )
        bindings.extend(
            (
                f"  {item.name} :: {_emit_type(item.expression.type)}",
                f"  {item.name} = {_emit_expression(rewritten)}",
            )
        )
    return (
        f"{function.name} :: {signature}\n"
        f"{function.name} {parameters} = {body}\n"
        " where\n"
        + "\n".join(bindings)
        + "\n"
    )


def _field_accessor(struct_name: str, field_name: str) -> str:
    return f"{_clash_struct_name(struct_name).lower()}_{field_name}"


def _clash_struct_name(name: str) -> str:
    """Render a source struct identity as a legal, deterministic Haskell name.

    Parameterized source structs carry their specialization in names such as
    ``RegRequest<AW,DW>``.  Angle brackets are meaningful to ZLang but are not
    legal in Clash identifiers, so retain the specialization identity while
    encoding it as an identifier rather than leaking source syntax into HDL.
    """
    rendered = re.sub(r"[^A-Za-z0-9_]", "_", name)
    rendered = re.sub(r"_+", "_", rendered).strip("_")
    if not rendered:
        raise ClashEmissionError("empty struct name after Clash identifier encoding")
    if rendered[0].isdigit():
        rendered = "_" + rendered
    return rendered
