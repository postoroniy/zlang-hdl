"""Structured Clash formal emission for recursive state observations.

This module emits one closed formal component per physical scalar instance and
transports actual current register, FIFO, request/response, and scheduler-
accepted rule-fire Signals to a deterministic top observation product.
CSR-derived, memory, and instance-array observations remain outside this
emitter.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import re

from zlang.backend.clash.emitter import (
    ClashEmissionError,
    _clash_instance_name,
    _clash_recursive_token_overrides,
    _clash_name,
    _emit_child_application,
    _emit_connection_module,
    _emit_credit_module,
    _emit_declared_fifo_bindings,
    _emit_expression,
    _emit_prelude,
    _emit_hierarchical_protocol_module,
    _emit_request_response_child_function,
    _protocol_instance_child_name,
    _request_response_child_formal_accessor,
    _request_response_component_output_name,
    _emit_scheduled_fifo_bindings,
    _emit_signal_expression,
    _emit_type,
    _emit_unified_schedule_bindings,
    _rule_schedule,
    _ready_valid_declarations,
)
from zlang.backend.manifest import BackendArtifact, PhysicalDomainManifest
from zlang.backend.identifiers import allocate_private_rtl_identifier
from zlang.backend.clash.domain import (
    ClashDomainError,
    domain_declaration,
    module_domain,
    top_reset_expression,
)
from zlang.formal import run_verilog_formal
from zlang.formal_domain import FormalDomainRenderingError, render_formal_domain
from zlang.ir.cdc import PowerUpPolicy
from zlang.ir import expressions as expr
from zlang.ir.formal import (
    FormalResult,
    FormalError,
    FormalStatus,
    ProofMode,
    PropertyKind,
    SignalBinding,
    TemporalForm,
    render_bound_predicate,
)
from zlang.ir.formal_observations import (
    port_observation_id,
    recursive_observation_id,
    rule_fire_observation_id,
)
from zlang.ir.formal_predicates import ObservationCycle
from zlang.ir.recursive_formal import RecursiveFormalResult
from zlang.ir.interfaces import ConnectionAdapter, CreditSignal, InterfaceProtocol
from zlang.ir.module import Module, Port
from zlang.ir.types import BitType


@dataclass(frozen=True)
class RegisterFormalSource:
    text: str
    observation_tokens: tuple[tuple[str, str], ...]
    top_name: str


_UNSUPPORTED_RESET_FORMAL_REASON = (
    "recursive M35 Clash execution requires one exact physical domain with "
    "power_up unspecified"
)


def _recursive_physical_domain_artifact_reason(
    module: Module,
    artifact: BackendArtifact,
) -> str | None:
    """Validate the exact non-default domain used by the recursive checker.

    The runner renders checker timing from semantic IR.  It must therefore
    reject a stale or corrupted v10 artifact whose published physical contract
    or public clock/reset paths describe different hardware.
    """

    domain = module_domain(module)
    if domain.is_legacy_default:
        # Legacy artifacts intentionally predate the v10 physical-domain record.
        return None
    if artifact.manifest_version < 10 or len(artifact.physical_domains) != 1:
        return (
            "recursive M35 Clash execution requires exactly one v10 physical "
            "domain manifest for a non-default reset"
        )
    actual = artifact.physical_domains[0]
    try:
        actual.validate()
        expected = PhysicalDomainManifest.publish(
            domain,
            rtl_module=module.name,
            rtl_clock_path=module.clock,
            rtl_reset_path=module.reset,
        )
    except ValueError as error:
        return f"recursive M35 physical domain manifest is invalid: {error}"
    if actual.contract_data != expected.contract_data:
        return (
            "recursive M35 physical domain contract does not match the "
            "semantic module"
        )
    if actual.build_data != expected.build_data:
        return (
            "recursive M35 physical clock/reset paths do not match the "
            "semantic module ABI"
        )
    return None


def _recursive_domain_mismatch_reason(
    module: Module,
    recursive_design: object,
    concrete: object,
    recursive_bindings: dict[str, object],
    connected_instances: dict[str, object] | None = None,
) -> str | None:
    """Validate a recursive property against the one connected root domain.

    The structured Clash formal component currently has one physical clock and
    reset ABI.  A child identity may use that ABI only when its semantic node,
    property clock, and every observed binding explicitly name the same domain.
    """
    root_clock = module.clock
    root_reset = module.reset
    instances = {
        item.instance_identity: item for item in recursive_design.instances
    }
    node = instances.get(concrete.instance_identity)
    if node is None:
        return (
            "recursive formal instance metadata unavailable: "
            f"{concrete.instance_identity}"
        )
    if node.clock_domain != root_clock or node.reset_domain != root_reset:
        return (
            "recursive formal instance domain does not match connected root: "
            f"instance {concrete.instance_identity} has clock/reset "
            f"({node.clock_domain!r}, {node.reset_domain!r}), root has "
            f"({root_clock!r}, {root_reset!r})"
        )
    connected_node = (
        connected_instances.get(concrete.instance_identity)
        if connected_instances is not None else None
    )
    if connected_instances is not None and connected_node is None:
        return (
            "connected formal artifact instance metadata unavailable: "
            f"{concrete.instance_identity}"
        )
    if connected_node is not None and (
        connected_node.clock_domain != root_clock
        or connected_node.reset_domain != root_reset
    ):
        return (
            "connected formal artifact instance domain does not match root: "
            f"instance {concrete.instance_identity} has clock/reset "
            f"({connected_node.clock_domain!r}, {connected_node.reset_domain!r}), "
            f"root has ({root_clock!r}, {root_reset!r})"
        )
    if concrete.property.clock != root_clock:
        return (
            "recursive formal property clock does not match connected root: "
            f"property {concrete.concrete_property_id} uses "
            f"{concrete.property.clock!r}, root uses {root_clock!r}"
        )
    if (concrete.property.reset_condition is not None
            and concrete.property.reset_condition != root_reset):
        return (
            "recursive formal property reset does not match connected root: "
            f"property {concrete.concrete_property_id} uses "
            f"{concrete.property.reset_condition!r}, root uses {root_reset!r}"
        )
    for semantic_id in concrete.property.relevant_signals:
        binding = recursive_bindings.get(semantic_id)
        if binding is None:
            # The existing binding-resolution path reports the more useful
            # unavailable-observation diagnostic below.
            continue
        if (binding.clock_domain != root_clock
                or binding.reset_domain != root_reset):
            return (
                "recursive formal observation domain does not match connected "
                f"root: {semantic_id} has clock/reset "
                f"({binding.clock_domain!r}, {binding.reset_domain!r}), root "
                f"has ({root_clock!r}, {root_reset!r})"
            )
    return None


def supports_register_formal(module: Module, recursive_design: object) -> bool:
    """Return whether the scalar hierarchy is in the structured state slice."""
    if (
        len(module.clock_domains) != 1
        or module.clock_domains[0].power_up is not PowerUpPolicy.UNSPECIFIED
    ):
        return False
    state_bindings = [
        item for item in recursive_design.bindings
        if item.ref.local_semantic_id.startswith(
            ("register:", "fifo:", "rr:", "rule:")
        )
    ]
    if not state_bindings:
        return False

    def supported(current: Module) -> bool:
        if current.request_response_connections:
            return bool(
                len(current.outputs) == 1
                and all(port.protocol is InterfaceProtocol.WIRE
                        for port in current.ports)
                and not current.fifos
                and not current.memories
                and not current.csr_blocks
                and not current.aggregate_protocol_endpoints
                and bool(current.clock and current.reset)
                and all((item.array_length or 1) == 1 for item in current.instances)
                and all(
                    len(child.request_responses) == 1
                    and not child.request_response_connections
                    for child in current.children
                )
            )
        return bool(
            len(current.outputs) == 1
            and all(port.protocol is InterfaceProtocol.WIRE for port in current.ports)
            and not current.memories
            and not current.csr_blocks
            and not current.request_responses
            and not current.hierarchical_connections
            and not current.aggregate_protocol_endpoints
            and (not (current.registers or current.fifos)
                 or bool(current.clock and current.reset))
            and all((item.array_length or 1) == 1 for item in current.instances)
            and all(supported(child) for child in current.children)
        )

    return bool(module.clock and module.reset and supported(module))


def supports_receiver_credit_formal(
    module: Module, recursive_design: object,
) -> bool:
    """Return whether root receiver credit state has a closed formal ABI.

    This is deliberately a root-only projection, not a new recursive
    observation family. The endpoint counter and its send/return leaves already
    exist in the typed credit component and in the frozen M35 property model.
    """

    if (
        len(module.clock_domains) != 1
        or module.clock_domains[0].power_up is not PowerUpPolicy.UNSPECIFIED
        or not module.clock
        or not module.reset
        or module.children
        or module.elaborated_instances
    ):
        return False
    receivers = tuple(
        port for port in module.ports
        if port.protocol is InterfaceProtocol.CREDIT
        and port.direction.value == "input"
    )
    if not receivers:
        return False
    for port in receivers:
        relevant_connections = tuple(
            connection for connection in module.connections
            if connection.source.name == port.name
        )
        if relevant_connections and not (
            len(relevant_connections) == 1
            and relevant_connections[0].source.protocol
            is InterfaceProtocol.CREDIT
            and relevant_connections[0].adapter
            is ConnectionAdapter.CREDIT_TO_READY_VALID
            and relevant_connections[0].buffer_depth > 0
        ):
            return False
    root = next(
        (
            item for item in recursive_design.instances
            if item.instance_identity == recursive_design.root_instance_identity
        ),
        None,
    )
    if root is None:
        return False
    required = {
        port_observation_id(port.name, signal)
        for port in receivers
        for signal in (
            "occupancy", CreditSignal.SEND.value, CreditSignal.RETURN.value,
        )
    }
    available = {
        item.ref.local_semantic_id
        for item in recursive_design.bindings
        if item.ref.instance_identity == root.instance_identity
    }
    return required <= available


def emit_receiver_credit_formal_source(
    module: Module, recursive_design: object,
) -> RegisterFormalSource:
    """Emit a typed root receiver-credit formal observation product."""

    if not supports_receiver_credit_formal(module, recursive_design):
        raise ClashEmissionError(
            "structured receiver-credit formal emission requires one root, "
            "single-domain typed credit receiver"
        )
    root = next(
        item for item in recursive_design.instances
        if item.instance_identity == recursive_design.root_instance_identity
    )
    ordered = tuple(sorted(
        recursive_design.bindings, key=lambda item: item.semantic_binding_id
    ))
    global_index = {
        item.semantic_binding_id: index for index, item in enumerate(ordered)
    }
    by_local = {
        item.ref.local_semantic_id: item
        for item in ordered
        if item.ref.instance_identity == root.instance_identity
    }
    projections: list[tuple[Port, str, str]] = []
    tokens: list[tuple[str, str]] = []
    for port in module.ports:
        if (
            port.protocol is not InterfaceProtocol.CREDIT
            or port.direction.value != "input"
        ):
            continue
        for signal in (
            "occupancy", CreditSignal.SEND.value, CreditSignal.RETURN.value,
        ):
            binding = by_local[port_observation_id(port.name, signal)]
            token = (
                f"zlang_formal_obs_{global_index[binding.semantic_binding_id]}"
            )
            projections.append((port, signal, token))
            tokens.append((binding.semantic_binding_id, token))
    uses_connection_emitter = any(
        connection.adapter is not None
        or connection.buffer_depth
        or connection.crossing is not None
        for connection in module.connections
    )
    source = (
        _emit_connection_module(
            module, formal_observations=tuple(projections),
        )
        if uses_connection_emitter
        else _emit_credit_module(
            module, formal_observations=tuple(projections),
        )
    )
    return RegisterFormalSource(
        source,
        tuple(tokens),
        f"{module.name}_formal",
    )


def emit_register_formal_source(module: Module, recursive_design: object) -> RegisterFormalSource:
    """Emit the structured formal-only scalar hierarchy."""
    if not supports_register_formal(module, recursive_design):
        raise ClashEmissionError(
            "structured recursive state formal emission supports only scalar, "
            "single-output, non-array register/FIFO hierarchy"
        )

    nodes = {item.instance_identity: item for item in recursive_design.instances}
    root = nodes[recursive_design.root_instance_identity]
    has_request_response = any(
        item.ref.local_semantic_id.startswith("rr:")
        for item in recursive_design.bindings
    )
    # Request/response connection accounting remains owned by the parent.
    # Its existing closed child product additionally transports only the
    # already-defined register and accepted rule-fire observations.  FIFO and
    # other state families remain outside this mixed protocol slice.
    transported_prefixes = (
        ("rr:", "register:", "rule:") if has_request_response
        else ("register:", "fifo:", "rule:")
    )
    all_state = tuple(sorted(
        (item for item in recursive_design.bindings
         if item.ref.local_semantic_id.startswith(transported_prefixes)),
        key=lambda item: item.semantic_binding_id,
    ))
    global_index = {
        item.semantic_binding_id: index
        for index, item in enumerate(sorted(
            recursive_design.bindings, key=lambda value: value.semantic_binding_id
        ))
    }
    observation_tokens = tuple(
        (item.semantic_binding_id, f"zlang_formal_obs_{global_index[item.semantic_binding_id]}")
        for item in all_state
    )
    modules_by_path: dict[tuple[str, ...], Module] = {}

    def map_modules(current: Module, node: object) -> None:
        modules_by_path[node.physical_instance_path] = current
        child_nodes = {nodes[identity].source_instance_name: nodes[identity]
                       for identity in node.children}
        for child_index, elaborated in enumerate(current.elaborated_instances):
            child_node = child_nodes.get(elaborated.instance.name)
            child_module = (
                current.children[child_index]
                if child_index < len(current.children) else None
            )
            if child_module is not None and child_module.name != elaborated.child_module:
                child_module = None
            if child_node is not None and child_module is not None:
                map_modules(child_module, child_node)

    map_modules(module, root)

    def token(node: object) -> str:
        return node.instance_identity[:12]

    def type_name(node: object) -> str:
        return f"ZFormalOutput{token(node)}"

    def constructor(node: object) -> str:
        return type_name(node)

    def function(node: object) -> str:
        return f"zformalInstance{token(node)}"

    def functional_accessor(node: object) -> str:
        return f"zformalFunctional{token(node)}"

    def observation_accessor(node: object, binding: object) -> str:
        return f"zformalObservation{token(node)}_{global_index[binding.semantic_binding_id]}"

    def subtree_bindings(node: object) -> tuple[object, ...]:
        path = node.physical_instance_path
        return tuple(item for item in all_state
                     if item.physical_instance_path[:len(path)] == path)

    def binding_type(binding: object) -> str:
        owner = nodes[binding.ref.instance_identity]
        owner_module = modules_by_path[owner.physical_instance_path]
        local_id = binding.ref.local_semantic_id
        if local_id.startswith("register:"):
            register_name = local_id.split(":", 1)[1]
            register = next(item for item in owner_module.registers
                            if item.name == register_name)
            return _emit_type(register.type)
        if local_id.startswith("rule:"):
            return "Bit"
        if local_id.startswith("rr:"):
            return "Bit" if local_id.endswith((":request_accept", ":response_consume")) \
                else f"Unsigned {binding.width}"
        fifo_name, signal = local_id.split(":", 1)[1].split(".", 1)
        fifo = next(item for item in owner_module.fifos if item.name == fifo_name)
        if signal == "count":
            return f"Unsigned {fifo.count_width}"
        if signal == "front":
            return _emit_type(fifo.element_type)
        return "Bit"

    def local_observation_signal(binding: object) -> str:
        local_id = binding.ref.local_semantic_id
        if local_id.startswith("register:"):
            return local_id.split(":", 1)[1]
        if local_id.startswith("rule:"):
            owner = nodes[binding.ref.instance_identity]
            owner_module = modules_by_path[owner.physical_instance_path]
            group = next((
                item for item in owner_module.resolved_transition.action_groups
                if rule_fire_observation_id(item.rule_name) == local_id
            ), None) if owner_module.resolved_transition is not None else None
            if group is None:
                raise ClashEmissionError(
                    f"no typed Clash accepted rule-fire observation for '{local_id}'"
                )
            return f"rule_{group.rule_name}_fire"
        if local_id.startswith("rr:"):
            owner = nodes[binding.ref.instance_identity]
            token = _clash_recursive_token_overrides(
                modules_by_path[owner.physical_instance_path]
            ).get(local_id)
            if token is None:
                raise ClashEmissionError(
                    f"no typed Clash request/response observation for '{local_id}'"
                )
            return token
        fifo_name, signal = local_id.split(":", 1)[1].split(".", 1)
        # M35 push/pop denote accepted transfers, not unaccepted requests.
        physical = {"push": "enqueue", "pop": "dequeue"}.get(signal, signal)
        return f"{fifo_name}_{physical}"

    emitted: set[str] = set()
    emitted_protocol_children: set[str] = set()
    declarations: list[str] = []

    def emit_instance(current: Module, node: object) -> None:
        if node.instance_identity in emitted:
            return
        child_nodes_by_name = {
            nodes[identity].source_instance_name: nodes[identity]
            for identity in node.children
        }
        if not current.request_response_connections:
            for child_index, elaborated in enumerate(current.elaborated_instances):
                child_node = child_nodes_by_name.get(elaborated.instance.name)
                child_module = (
                    current.children[child_index]
                    if child_index < len(current.children) else None
                )
                if (
                    child_module is not None
                    and child_module.name != elaborated.child_module
                ):
                    child_module = None
                if child_node is not None and child_module is not None:
                    emit_instance(child_module, child_node)

        observations = subtree_bindings(node)
        output_type = _emit_type(current.outputs[0].type)
        fields = [(functional_accessor(node), output_type)] + [
            (observation_accessor(node, item), binding_type(item))
            for item in observations
        ]
        rendered_fields = "\n  , ".join(f"{name} :: {type_}" for name, type_ in fields)
        declarations.append(
            f"data {type_name(node)} = {constructor(node)}\n"
            f"  {{ {rendered_fields}\n"
            "  } deriving (Generic, NFDataX, Show, Eq)\n"
        )

        if current.request_response_connections:
            for child_index, elaborated in enumerate(current.elaborated_instances):
                child_node = child_nodes_by_name.get(elaborated.instance.name)
                child = (
                    current.children[child_index]
                    if child_index < len(current.children) else None
                )
                if child is not None and child.name != elaborated.child_module:
                    child = None
                if child_node is None or child is None:
                    raise ClashEmissionError(
                        "formal request/response hierarchy is missing typed "
                        f"child metadata for '{elaborated.instance.name}'"
                    )
                component_name = _protocol_instance_child_name(
                    current, elaborated.instance.name, child,
                )
                child_key = component_name
                if child_key not in emitted_protocol_children:
                    local_observations = tuple(
                        item.ref.local_semantic_id
                        for item in subtree_bindings(child_node)
                        if item.ref.instance_identity == child_node.instance_identity
                        and item.ref.local_semantic_id.startswith(
                            ("register:", "rule:")
                        )
                    )
                    declarations.append(_emit_request_response_child_function(
                        child,
                        component_name,
                        formal_observation_ids=local_observations,
                    ))
                    emitted_protocol_children.add(child_key)
            generated = _emit_hierarchical_protocol_module(current)
            component = re.search(
                r"^circuit (?!::)([^\n]+?) = (.+?)\n where\n(.*?)\n\ntopEntity",
                generated,
                re.MULTILINE | re.DOTALL,
            )
            if component is None:
                raise ClashEmissionError(
                    f"cannot recover typed request/response component plan for '{current.name}'"
                )
            arguments = component.group(1).strip()
            functional = component.group(2).strip()
            bindings = [line[2:] if line.startswith("  ") else line
                        for line in component.group(3).splitlines()]
            child_observation_signals: dict[str, str] = {}
            for child_index, elaborated in enumerate(current.elaborated_instances):
                child_node = child_nodes_by_name.get(elaborated.instance.name)
                child = (
                    current.children[child_index]
                    if child_index < len(current.children) else None
                )
                if child is not None and child.name != elaborated.child_module:
                    child = None
                if child_node is None or child is None:
                    raise ClashEmissionError(
                        "formal request/response hierarchy is missing typed "
                        f"child metadata for '{elaborated.instance.name}'"
                    )
                component_name = _protocol_instance_child_name(
                    current, elaborated.instance.name, child,
                )
                component_output = _request_response_component_output_name(
                    elaborated.instance.name
                )
                for item in subtree_bindings(child_node):
                    if (
                        item.ref.instance_identity != child_node.instance_identity
                        or not item.ref.local_semantic_id.startswith(
                            ("register:", "rule:")
                        )
                    ):
                        continue
                    projected = (
                        f"{_request_response_child_formal_accessor(child, item.ref.local_semantic_id, component_name)} "
                        f"<$> {component_output}"
                    )
                    if item.ref.local_semantic_id.startswith("rule:"):
                        projected = (
                            "(\\fire resetActive -> if resetActive then low else fire) "
                            f"<$> ({projected}) <*> reset_active"
                        )
                    child_observation_signals[item.semantic_binding_id] = projected
            values = [functional]
            for item in observations:
                if item.ref.instance_identity == node.instance_identity:
                    values.append(local_observation_signal(item))
                    continue
                projected = child_observation_signals.get(item.semantic_binding_id)
                if projected is None:
                    raise ClashEmissionError(
                        "nested request/response observation is outside the "
                        f"mixed child state/rule slice: '{item.ref.local_semantic_id}'"
                    )
                values.append(projected)
            application = f"{constructor(node)} <$> " + " <*> ".join(
                f"({value})" for value in values
            )
            signature = " -> ".join([
                *(f"Signal ZLangSystem ({_emit_type(port.type)})"
                  for port in current.inputs),
                f"Signal ZLangSystem {type_name(node)}",
            ])
            where = "\n".join(f"  {item}" for item in bindings)
            declarations.append(
                f"{function(node)} :: HiddenClockResetEnable ZLangSystem => {signature}\n"
                f"{function(node)} {arguments} = {application}\n"
                f" where\n{where}\n"
                f"{{-# NOINLINE {function(node)} #-}}\n"
            )
            emitted.add(node.instance_identity)
            return

        domain = "ZLangSystem"
        inputs = current.inputs
        signature = " -> ".join([
            *(f"Signal {domain} ({_emit_type(port.type)})" for port in inputs),
            f"Signal {domain} {type_name(node)}",
        ])
        arguments = " ".join(_clash_name(port.name) for port in inputs)
        bindings: list[str] = []
        for local in current.locals:
            bindings.append(f"{local.name} = {_emit_signal_expression(local.expression, {})}")

        child_observation_signals: dict[str, str] = {}
        for child_index, elaborated in enumerate(current.elaborated_instances):
            instance = elaborated.instance
            child_node = child_nodes_by_name.get(instance.name)
            child = (
                current.children[child_index]
                if child_index < len(current.children) else None
            )
            if child is not None and child.name != elaborated.child_module:
                child = None
            if child_node is None or child is None:
                raise ClashEmissionError(
                    f"formal hierarchy is missing instance metadata for '{instance.name}'"
                )
            port_bindings = {
                item.port: item.expression for item in current.instance_bindings
                if item.instance == instance.name
            }
            missing = [port.name for port in child.inputs if port.name not in port_bindings]
            if missing:
                raise ClashEmissionError(
                    f"instance '{instance.name}' is missing bindings for {', '.join(missing)}"
                )
            applications = [
                _emit_signal_expression(port_bindings[port.name], {})
                for port in child.inputs
            ]
            instance_name = _clash_instance_name(instance.name)
            result_name = f"zformalResult_{instance_name}"
            call = _emit_child_application(
                function(child_node), applications, sequential_child=True,
            )
            bindings.append(f"{result_name} = {call}")
            for output_port in child.outputs:
                bindings.append(
                    f"{instance_name}_{_clash_name(output_port.name)} = "
                    f"{functional_accessor(child_node)} <$> {result_name}"
                )
            for child_binding in subtree_bindings(child_node):
                child_observation_signals[child_binding.semantic_binding_id] = (
                    f"{observation_accessor(child_node, child_binding)} <$> {result_name}"
                )

        reset_needed = bool(current.rules or current.fifos)
        if reset_needed:
            bindings.append("reset_active = unsafeToActiveHigh hasReset")
        ordered_rules = _rule_schedule(current)
        if current.resolved_transition is not None and current.rules:
            # Observe the authoritative accepted action-group relation for
            # every rule family. This is the same backend-independent
            # ResolvedTransition consumed by production Clash, including
            # explicit priority and resource acceptance.
            bindings.extend(_emit_unified_schedule_bindings(current))
        else:
            earlier_rules: list[object] = []
            for rule in ordered_rules:
                fire_name = f"rule_{rule.name}_fire"
                raw_guard = _emit_signal_expression(rule.guard, {})
                targets = {action.target.name for action in rule.actions}
                blockers = [
                    earlier for earlier in earlier_rules
                    if targets & {action.target.name for action in earlier.actions}
                ]
                if blockers:
                    parameters = " ".join((
                        "guard", "resetActive",
                        *(f"blocked{index}" for index in range(len(blockers))),
                    ))
                    clear = " && ".join(
                        f"blocked{index} == low" for index in range(len(blockers))
                    )
                    applications = "".join(
                        f" <*> rule_{blocker.name}_fire" for blocker in blockers
                    )
                    bindings.append(
                        f"{fire_name} = (\\{parameters} -> if not resetActive && guard == high && {clear} "
                        f"then high else low) <$> {raw_guard} <*> reset_active{applications}"
                    )
                else:
                    bindings.append(
                        f"{fire_name} = (\\guard resetActive -> if resetActive then low else guard) "
                        f"<$> {raw_guard} <*> reset_active"
                    )
                earlier_rules.append(rule)

        next_by_register = {
            assignment.target.name: assignment.expression
            for assignment in current.next_assignments
        }
        for register in current.registers:
            bindings.append(
                f"{register.name} = register {_emit_expression(register.initial)} "
                f"({register.name}_next)"
            )
            scheduled = next_by_register.get(
                register.name, expr.RegisterRef(register.name, register.type)
            )
            writers = [
                (rule, action)
                for rule in ordered_rules
                for action in rule.actions
                if action.target.name == register.name
            ]
            for rule, action in reversed(writers):
                scheduled = expr.Mux(
                    expr.InputRef(f"rule_{rule.name}_fire", BitType()),
                    action.expression, scheduled, register.type,
                )
            bindings.append(
                f"{register.name}_next = {_emit_signal_expression(scheduled, {})}"
            )
        for fifo in current.fifos:
            if fifo.scheduled:
                bindings.extend(_emit_scheduled_fifo_bindings(current, fifo))
            else:
                bindings.extend(_emit_declared_fifo_bindings(fifo))

        output_expression = (
            current.assignments[0].expression
            if current.assignments else expr.Constant(0, current.outputs[0].type)
        )
        bindings.append(
            f"zformalFunctionalSignal = {_emit_signal_expression(output_expression, {})}"
        )
        values = ["zformalFunctionalSignal"]
        for item in observations:
            if item.ref.instance_identity == node.instance_identity:
                values.append(local_observation_signal(item))
            else:
                values.append(child_observation_signals[item.semantic_binding_id])
        application = f"{constructor(node)} <$> " + " <*> ".join(
            f"({value})" for value in values
        )
        lhs = f"{function(node)} {arguments}" if arguments else function(node)
        where = "\n".join(f"  {item}" for item in bindings)
        declarations.append(
            f"{function(node)} :: HiddenClockResetEnable {domain} => {signature}\n"
            f"{lhs} = {application}\n"
            f" where\n{where}\n"
            f"{{-# NOINLINE {function(node)} #-}}\n"
        )
        emitted.add(node.instance_identity)

    emit_instance(module, root)

    extensions, imports, prelude_declarations = _emit_prelude(module)
    if has_request_response:
        prelude_declarations = _ready_valid_declarations() + prelude_declarations
    if "{-# LANGUAGE DeriveAnyClass #-}" not in extensions:
        extensions = extensions.replace(
            "{-# LANGUAGE NoImplicitPrelude #-}",
            "{-# LANGUAGE DeriveAnyClass #-}\n"
            "{-# LANGUAGE DeriveGeneric #-}\n"
            "{-# LANGUAGE NoImplicitPrelude #-}",
        )
    if "{-# LANGUAGE TemplateHaskell #-}" not in extensions:
        extensions = extensions.replace(
            "{-# LANGUAGE NoImplicitPrelude #-}",
            "{-# LANGUAGE TemplateHaskell #-}\n{-# LANGUAGE NoImplicitPrelude #-}",
        )
    if "import GHC.Generics (Generic)" not in imports:
        imports += "import GHC.Generics (Generic)\n"
    domain = "ZLangSystem"
    signal_inputs = [
        f"Signal {domain} ({_emit_type(port.type)})" for port in module.inputs
    ]
    circuit_signature = " -> ".join((*signal_inputs, f"Signal {domain} {type_name(root)}"))
    top_signature = " -> ".join((
        f"Clock {domain}", f"Reset {domain}", *signal_inputs,
        f"Signal {domain} {type_name(root)}",
    ))
    arguments = " ".join(_clash_name(port.name) for port in module.inputs)
    circuit_lhs = f"circuit {arguments}" if arguments else "circuit"
    root_call = function(root) + (f" {arguments}" if arguments else "")
    top_arguments = " ".join((module.clock, module.reset, arguments)).strip()
    circuit_application = f" {arguments}" if arguments else ""
    input_ports = ", ".join(
        f'PortName "{name}"'
        for name in (module.clock, module.reset, *(port.name for port in module.inputs))
    )
    output_ports = [f'PortName "{module.outputs[0].name}"'] + [
        f'PortName "{token_}"' for _, token_ in observation_tokens
    ]
    instance_declarations = "\n".join(declarations)
    try:
        domain_declaration_text = domain_declaration(module, domain)
        reset_expression = top_reset_expression(module)
    except ClashDomainError as error:
        raise ClashEmissionError(str(error)) from error
    text = f'''{extensions}
module {module.name} where

{imports}
{prelude_declarations}{domain_declaration_text}

{instance_declarations}
circuit :: HiddenClockResetEnable {domain} => {circuit_signature}
{circuit_lhs} = {root_call}

topEntity :: {top_signature}
topEntity {top_arguments} = exposeClockResetEnable circuit {module.clock} {reset_expression} enableGen{circuit_application}

{{-# ANN topEntity
  (Synthesize
    {{ t_name = "{module.name}_formal"
    , t_inputs = [{input_ports}]
    , t_output = PortProduct "" [{", ".join(output_ports)}]
    }}) #-}}
'''
    return RegisterFormalSource(text, observation_tokens, f"{module.name}_formal")


def validate_register_formal_artifact(
    artifact: BackendArtifact,
    verilog_files: tuple[Path, ...],
) -> BackendArtifact:
    """Finalize structured state observation locators from generated Clash RTL.

    Tokens are allocated from semantic binding order before compilation.  RTL
    inspection only confirms that the already-known top port exists at the
    expected width; it never reconstructs semantic identity from an RTL name.
    """
    if artifact.backend != "clash" or artifact.manifest_version < 4:
        raise ClashEmissionError("structured state formal validation requires a Clash v4 artifact")
    texts = tuple((path.name, path.read_text()) for path in sorted(verilog_files))
    top_name = f"{artifact.module}_formal"
    top_text = next(
        (text for _, text in texts
         if re.search(rf"\bmodule\s+{re.escape(top_name)}\b", text)),
        None,
    )
    if top_text is None:
        raise ClashEmissionError(f"generated formal RTL is missing top module '{top_name}'")
    digest = hashlib.sha256("".join(
        f"{name}\0{text}\0" for name, text in texts
    ).encode()).hexdigest()
    ordered_ids = {
        item.semantic_binding_id: index
        for index, item in enumerate(sorted(
            artifact.recursive_bindings, key=lambda value: value.semantic_binding_id
        ))
    }

    def observed_width(token: str) -> int | None:
        match = re.search(
            rf"\boutput\s+wire(?:\s+\[(\d+)\s*:\s*(\d+)\])?\s+{re.escape(token)}\b",
            top_text,
        )
        if match is None:
            return None
        if match.group(1) is None:
            return 1
        return abs(int(match.group(1)) - int(match.group(2))) + 1

    recursive = []
    available: dict[str, str] = {}
    for item in artifact.recursive_bindings:
        token = None
        receiver_credit_observation = (
            item.local_semantic_id.startswith("port:")
            and item.local_semantic_id.rsplit(".", 1)[-1]
            in {"occupancy", CreditSignal.SEND.value, CreditSignal.RETURN.value}
        )
        if receiver_credit_observation or item.local_semantic_id.startswith(
            ("register:", "fifo:", "rr:", "rule:", "csr-field:")
        ):
            candidate = f"zlang_formal_obs_{ordered_ids[item.semantic_binding_id]}"
            if observed_width(candidate) == item.width:
                token = candidate
                available[item.semantic_binding_id] = candidate
        recursive.append(replace(
            item,
            artifact_hash=digest,
            rtl_module=top_name if token else None,
            rtl_path=(),
            signal_token=token,
            formal_observation_token=token,
            physical_available=token is not None,
        ))
    observations = tuple(replace(
        item,
        observation_token=available.get(item.semantic_binding_id),
        artifact_hash=digest,
        physical_available=item.semantic_binding_id in available,
    ) for item in artifact.formal_observations)
    return replace(
        artifact,
        recursive_bindings=tuple(recursive),
        formal_observations=observations,
        formal_artifact_hash=digest,
    )


def finalize_formal_verilog_artifact(
    artifact: BackendArtifact,
    verilog_files: tuple[Path, ...],
) -> BackendArtifact:
    """Publish one immutable artifact from validated Clash-generated Verilog.

    ``emit_formal_artifact`` deliberately publishes Haskell source first, and
    ``validate_register_formal_artifact`` proves the formal observation ports
    against the generated RTL.  Verification bundles need the final generated
    Verilog itself.  This adapter closes that existing two-phase ABI without
    reconstructing a semantic signal from an RTL name: public tokens come from
    the artifact binding map and observation tokens come from the structured
    formal ABI, then both are checked against the exact formal-top declaration.

    The returned text is a deterministic concatenation of every generated
    Verilog compilation unit.  Its SHA-256 is the sole artifact/formal hash
    attached to public bindings, recursive bindings, and observations.
    """

    if not verilog_files:
        raise ClashEmissionError("Clash formal finalization requires generated Verilog")
    hidden_observations = tuple(
        item for item in artifact.recursive_bindings
        if item.local_semantic_id not in {"clock", "reset"}
        and (
            not item.local_semantic_id.startswith("port:")
            or item.direction == "internal"
        )
    )
    validated = (
        validate_register_formal_artifact(artifact, verilog_files)
        if hidden_observations else artifact
    )
    named_texts = tuple(
        (path.name, path.read_text())
        for path in sorted(verilog_files, key=lambda item: item.name)
    )
    if len({name for name, _ in named_texts}) != len(named_texts):
        raise ClashEmissionError(
            "Clash formal finalization requires unique generated Verilog filenames"
        )

    available_modules = {
        item.rtl_module
        for item in validated.recursive_bindings
        if item.physical_available and item.rtl_module
    }
    if not available_modules:
        available_modules = {
            item.rtl_module
            for item in validated.bindings
            if item.physical_available and item.rtl_module
        }
    if len(available_modules) != 1:
        raise ClashEmissionError(
            "Clash formal finalization requires exactly one validated formal top"
        )
    top_name = next(iter(available_modules))
    top_text = next(
        (
            text for _, text in named_texts
            if re.search(rf"\bmodule\s+{re.escape(top_name)}\b", text)
        ),
        None,
    )
    if top_text is None:
        raise ClashEmissionError(
            f"generated formal RTL is missing validated top module '{top_name}'"
        )

    module_match = re.search(
        rf"\bmodule\s+{re.escape(top_name)}\s*\((.*?)\);",
        top_text,
        re.DOTALL,
    )
    if module_match is None:
        raise ClashEmissionError(
            f"generated formal RTL has no parseable ANSI port list for '{top_name}'"
        )
    header = re.sub(r"/\*.*?\*/", " ", module_match.group(1), flags=re.DOTALL)
    header = re.sub(r"//[^\n]*", " ", header)
    port_pattern = re.compile(
        r"(?:^|,)\s*(input|output)\s+"
        r"(?:(?:wire|reg|logic|signed|unsigned)\s+)*"
        r"(?:\[(\d+)\s*:\s*(\d+)\]\s+)?"
        r"([A-Za-z_][A-Za-z0-9_$]*)",
        re.MULTILINE,
    )
    ports: dict[str, tuple[str, int]] = {}
    for match in port_pattern.finditer(header):
        token = match.group(4)
        if token in ports:
            raise ClashEmissionError(
                f"generated formal RTL declares duplicate top port '{token}'"
            )
        width = (
            1 if match.group(2) is None
            else abs(int(match.group(2)) - int(match.group(3))) + 1
        )
        ports[token] = (match.group(1), width)

    combined = "\n".join(
        f"// ZLang Clash formal unit: {name}\n{text.rstrip()}\n"
        for name, text in named_texts
    )
    digest = hashlib.sha256(combined.encode()).hexdigest()

    def rebound_public(item: object) -> object:
        token = item.rtl_path
        expected_direction = (
            "input"
            if getattr(item.role, "value", str(item.role)) in {"input", "clock", "reset"}
            else "output"
        )
        declared = ports.get(token) if token else None
        available = declared == (expected_direction, item.width)
        if item.physical_available and not available:
            rendered = "missing" if declared is None else (
                f"{declared[0]} width {declared[1]}"
            )
            raise ClashEmissionError(
                "generated Clash formal public port does not match the "
                f"published binding '{item.semantic_signal_id}': expected "
                f"{expected_direction} '{token}' width {item.width}, got {rendered}"
            )
        return replace(
            item,
            rtl_module=top_name,
            rtl_path=token if available else "",
            artifact_hash=digest,
            physical_available=available,
        )

    public = tuple(rebound_public(item) for item in validated.bindings)
    public_by_semantic = {
        item.semantic_signal_id: item for item in public if item.physical_available
    }
    root_depth = min(
        (len(item.physical_instance_path) for item in validated.recursive_bindings),
        default=None,
    )
    recursive = []
    for item in validated.recursive_bindings:
        rebound = item
        if (
            root_depth is not None
            and len(item.physical_instance_path) == root_depth
            and item.local_semantic_id.startswith("port:")
            and not item.physical_available
        ):
            public_item = public_by_semantic.get(item.local_semantic_id)
            if public_item is not None:
                rebound = replace(
                    item,
                    rtl_module=top_name,
                    rtl_path=(),
                    signal_token=public_item.rtl_path,
                    formal_observation_token=public_item.rtl_path,
                    physical_available=True,
                )
        recursive.append(rebound)

    recursive_by_id = {item.semantic_binding_id: item for item in recursive}
    if set(recursive_by_id) != {
        item.semantic_binding_id for item in validated.formal_observations
    }:
        raise ClashEmissionError(
            "Clash formal finalization found inconsistent recursive observation links"
        )
    observations = tuple(
        replace(
            item,
            observation_token=(
                recursive_by_id[item.semantic_binding_id].formal_observation_token
            ),
            physical_available=recursive_by_id[item.semantic_binding_id].physical_available,
        )
        for item in validated.formal_observations
    )
    recursive = tuple(replace(item, artifact_hash=digest) for item in recursive)
    observations = tuple(replace(item, artifact_hash=digest) for item in observations)
    finalized = replace(
        validated,
        module=top_name,
        text=combined,
        artifact_hash=digest,
        bindings=public,
        recursive_bindings=recursive,
        formal_observations=observations,
        formal_artifact_hash=digest,
        physical_domains=tuple(
            replace(item, rtl_module=top_name)
            for item in validated.physical_domains
        ),
    )
    # Exercise the complete v4 manifest/link validator before this artifact can
    # escape into an immutable verification bundle.
    finalized.to_json()
    return finalized


def run_recursive_register_formal(
    module: Module,
    recursive_design: object,
    artifact: BackendArtifact,
    verilog_files: tuple[Path, ...],
    *,
    mode: ProofMode = ProofMode.BMC,
    depth: int = 8,
    solver: str = "z3",
    property_ids: frozenset[str] | None = None,
) -> tuple[RecursiveFormalResult, ...]:
    """Execute implementation-bound recursive M35 register/FIFO properties."""
    if (
        len(module.clock_domains) != 1
        or module.clock_domains[0].power_up is not PowerUpPolicy.UNSPECIFIED
    ):
        return tuple(
            RecursiveFormalResult.from_formal_result(
                concrete,
                FormalResult(
                    concrete.concrete_property_id,
                    FormalStatus.SKIPPED,
                    mode,
                    "sby",
                    solver,
                    depth,
                    source_origin=concrete.property.source_origin,
                    reason=_UNSUPPORTED_RESET_FORMAL_REASON,
                ),
                artifact_hash=getattr(artifact, "formal_artifact_hash", None)
                or artifact.artifact_hash,
            )
            for concrete in sorted(
                recursive_design.properties,
                key=lambda item: item.concrete_property_id,
            )
            if property_ids is None
            or concrete.concrete_property_id in property_ids
        )
    artifact_domain_reason = _recursive_physical_domain_artifact_reason(
        module, artifact
    )
    if artifact_domain_reason is not None:
        return tuple(
            RecursiveFormalResult.from_formal_result(
                concrete,
                FormalResult(
                    concrete.concrete_property_id,
                    FormalStatus.SKIPPED,
                    mode,
                    "sby",
                    solver,
                    depth,
                    source_origin=concrete.property.source_origin,
                    reason=artifact_domain_reason,
                ),
                artifact_hash=getattr(artifact, "formal_artifact_hash", None)
                or artifact.artifact_hash,
            )
            for concrete in sorted(
                recursive_design.properties,
                key=lambda item: item.concrete_property_id,
            )
            if property_ids is None
            or concrete.concrete_property_id in property_ids
        )
    validated = validate_register_formal_artifact(artifact, verilog_files)
    rtl = "\n".join(path.read_text() for path in sorted(verilog_files))
    observations = {
        item.semantic_binding_id: item
        for item in validated.formal_observations
        if item.physical_available and item.observation_token is not None
    }
    recursive_bindings = {
        item.semantic_binding_id: item for item in validated.recursive_bindings
    }
    connected_instances = {
        item.instance_identity: item for item in validated.instances
    }

    def declaration(width: int, name: str, kind: str = "wire") -> str:
        packed = "" if width == 1 else f" [{width - 1}:0]"
        return f"  {kind}{packed} {name};"

    results: list[RecursiveFormalResult] = []
    for concrete in sorted(
        recursive_design.properties, key=lambda item: item.concrete_property_id
    ):
        if property_ids is not None and concrete.concrete_property_id not in property_ids:
            continue
        predicate = concrete.property.predicate
        if predicate is None or concrete.property.non_executable_reason is not None:
            reason = (
                concrete.property.non_executable_reason
                or "recursive property has no structured executable predicate"
            )
            skipped = FormalResult(
                concrete.concrete_property_id, FormalStatus.SKIPPED, mode,
                "sby", solver, depth, source_origin=concrete.property.source_origin,
                reason=reason,
            )
            results.append(RecursiveFormalResult.from_formal_result(
                concrete, skipped, artifact_hash=validated.formal_artifact_hash
            ))
            continue
        if concrete.property.temporal_form not in {
            TemporalForm.SAME_CYCLE,
            TemporalForm.NEXT_CYCLE,
        }:
            skipped = FormalResult(
                concrete.concrete_property_id, FormalStatus.SKIPPED, mode,
                "sby", solver, depth, source_origin=concrete.property.source_origin,
                reason=(
                    "recursive Clash structured predicate runner does not support "
                    f"temporal form '{concrete.property.temporal_form.value}'"
                ),
            )
            results.append(RecursiveFormalResult.from_formal_result(
                concrete, skipped, artifact_hash=validated.formal_artifact_hash
            ))
            continue

        incompatible_domain = _recursive_domain_mismatch_reason(
            module, recursive_design, concrete, recursive_bindings,
            connected_instances,
        )
        if incompatible_domain is not None:
            skipped = FormalResult(
                concrete.concrete_property_id, FormalStatus.SKIPPED, mode,
                "sby", solver, depth, source_origin=concrete.property.source_origin,
                reason=incompatible_domain,
            )
            results.append(RecursiveFormalResult.from_formal_result(
                concrete, skipped, artifact_hash=validated.formal_artifact_hash
            ))
            continue

        # Predicate leaves already contain concrete recursive semantic IDs.
        # Resolve every one through the artifact's typed observation manifest;
        # never recover a family or signal from the compatibility display text.
        refs_by_id = {
            recursive_observation_id(ref.instance_identity, ref.local_semantic_id): ref
            for ref in concrete.object_refs
        }
        predicate_bindings: dict[str, SignalBinding] = {}
        resolved_observations: list[tuple[object, str, object, object]] = []
        missing_reason: str | None = None
        for semantic_id in predicate.observation_ids():
            ref = refs_by_id.get(semantic_id)
            binding = recursive_bindings.get(semantic_id)
            if ref is None or binding is None:
                missing_reason = f"formal predicate binding unavailable: {semantic_id}"
                break
            if ref.local_semantic_id in {"clock", "reset"}:
                # The structured Clash slice is single-domain.  Child clock and
                # reset identities are transported by the explicit root ABI,
                # not by invented observation ports.
                rtl_name = module.clock if ref.local_semantic_id == "clock" else module.reset
                predicate_bindings[semantic_id] = SignalBinding(
                    semantic_id,
                    f"{module.name}_formal",
                    rtl_name,
                    binding.width,
                    "input",
                    binding.clock_domain,
                    binding.source_origin,
                )
                continue
            observation = observations.get(semantic_id)
            if observation is None or observation.observation_token is None:
                missing_reason = f"formal state observation unavailable: {semantic_id}"
                break
            predicate_bindings[semantic_id] = SignalBinding(
                semantic_id,
                binding.rtl_module or f"{module.name}_formal",
                observation.observation_token,
                binding.width,
                "output",
                binding.clock_domain,
                binding.source_origin,
            )
            resolved_observations.append((ref, semantic_id, observation, binding))

        # Clock and reset conditions are executable tokens as well, even when
        # the predicate itself does not reference them.  Require their concrete
        # recursive identities before constructing the checker.
        clock_id = recursive_observation_id(concrete.instance_identity, "clock")
        clock_binding = recursive_bindings.get(clock_id)
        if missing_reason is None and clock_binding is None:
            missing_reason = f"formal clock binding unavailable: {clock_id}"
        elif missing_reason is None:
            predicate_bindings.setdefault(
                clock_id,
                SignalBinding(
                    clock_id,
                    f"{module.name}_formal",
                    module.clock,
                    clock_binding.width,
                    "input",
                    clock_binding.clock_domain,
                    clock_binding.source_origin,
                ),
            )
        reset_id = recursive_observation_id(concrete.instance_identity, "reset")
        reset_binding = recursive_bindings.get(reset_id)
        if missing_reason is None and (
            concrete.property.reset_condition is not None
            or any(item.semantic_signal_id == reset_id for item in predicate.observations())
        ):
            if reset_binding is None:
                missing_reason = f"formal reset binding unavailable: {reset_id}"
            else:
                predicate_bindings.setdefault(
                    reset_id,
                    SignalBinding(
                        reset_id,
                        f"{module.name}_formal",
                        module.reset,
                        reset_binding.width,
                        "input",
                        reset_binding.clock_domain,
                        reset_binding.source_origin,
                    ),
                )
        if missing_reason is not None:
            skipped = FormalResult(
                concrete.concrete_property_id, FormalStatus.SKIPPED, mode,
                "sby", solver, depth, source_origin=concrete.property.source_origin,
                reason=missing_reason,
            )
            results.append(RecursiveFormalResult.from_formal_result(
                concrete, skipped, artifact_hash=validated.formal_artifact_hash
            ))
            continue

        used_names = {
            module.clock,
            module.reset,
            "functional",
            *(port.name for port in module.ports),
            *(item.observation_token for item in validated.formal_observations
              if item.observation_token is not None),
        }
        history_valid = allocate_private_rtl_identifier(
            "zlang_past_valid",
            semantic_identity=(
                f"{concrete.concrete_property_id}|recursive-m35|history-valid"
            ),
            used=used_names,
        )
        predicate_previous = tuple(
            item for item in predicate.observations()
            if item.cycle is ObservationCycle.PREVIOUS
        )
        reset_epoch_predicate = bool(predicate_previous) and all(
            (
                refs_by_id.get(item.semantic_signal_id) is not None
                and refs_by_id[item.semantic_signal_id].local_semantic_id == "reset"
            )
            for item in predicate_previous
        )
        reset_history_valid = None
        if reset_epoch_predicate and not module_domain(module).is_legacy_default:
            reset_history_valid = allocate_private_rtl_identifier(
                "zlang_reset_past_valid",
                semantic_identity=(
                    f"{concrete.concrete_property_id}|recursive-m35|"
                    "reset-history-valid"
                ),
                used=used_names,
            )
        try:
            domain_rendering = render_formal_domain(
                module_domain(module),
                clock_name=module.clock,
                reset_name=module.reset,
                used_names=used_names,
            )
        except (ClashDomainError, FormalDomainRenderingError) as error:
            skipped = FormalResult(
                concrete.concrete_property_id, FormalStatus.SKIPPED, mode,
                "sby", solver, depth, source_origin=concrete.property.source_origin,
                reason=str(error),
            )
            results.append(RecursiveFormalResult.from_formal_result(
                concrete, skipped, artifact_hash=validated.formal_artifact_hash
            ))
            continue
        for semantic_id, item in tuple(predicate_bindings.items()):
            ref = refs_by_id.get(semantic_id)
            if ref is not None and ref.local_semantic_id == "reset":
                predicate_bindings[semantic_id] = replace(
                    item, rtl_name=domain_rendering.reset_active
                )
        try:
            rendered_predicate = render_bound_predicate(
                predicate, predicate_bindings
            )
        except FormalError as error:
            skipped = FormalResult(
                concrete.concrete_property_id, FormalStatus.SKIPPED, mode,
                "sby", solver, depth, source_origin=concrete.property.source_origin,
                reason=f"unsupported structured formal predicate: {error}",
            )
            results.append(RecursiveFormalResult.from_formal_result(
                concrete, skipped, artifact_hash=validated.formal_artifact_hash
            ))
            continue

        wrapper = f"ZlangRecursiveState_{concrete.concrete_property_id[:12]}"
        lines = [f"module {wrapper}("]
        # The formal component has the complete physical top ABI.  Aggregate
        # protocol inputs are not represented by ordinary ``module.inputs``;
        # connect their frozen external leaves explicitly instead of leaving
        # free Clash inputs in the generated component.
        aggregate_inputs = module.top_aggregate_abi.inputs
        scalar_inputs = tuple(
            port for port in module.inputs
            if port.protocol is InterfaceProtocol.WIRE
            and not (aggregate_inputs and "__" in port.name)
        )
        top_inputs = [
            module.clock,
            module.reset,
            *(port.name for port in scalar_inputs),
            *(leaf.external_name for leaf in aggregate_inputs),
        ]
        input_widths = [
            1,
            1,
            *(port.type.width for port in scalar_inputs),
            *(leaf.width for leaf in aggregate_inputs),
        ]
        lines.append(",\n".join(
            f"  input wire{'' if width == 1 else f' [{width - 1}:0]'} {name}"
            for name, width in zip(top_inputs, input_widths)
        ) + ");")
        functional_output = next(
            (port for port in module.outputs
             if port.protocol is InterfaceProtocol.WIRE),
            None,
        )
        if functional_output is not None:
            lines.append(declaration(functional_output.type.width, "functional"))
        for item in validated.formal_observations:
            if item.observation_token is not None:
                lines.append(declaration(item.width, item.observation_token))
        connections = [
            *(f".{name}({name})" for name in top_inputs),
            *((f".{functional_output.name}(functional)",)
              if functional_output is not None else ()),
            *(f".{item.observation_token}({item.observation_token})"
              for item in validated.formal_observations
              if item.observation_token is not None),
        ]
        lines.append(
            f"  {module.name}_formal dut (" + ", ".join(connections) + ");"
        )
        lines.extend(f"  {item}" for item in domain_rendering.support_lines)
        lines.append(f"  {domain_rendering.initial_assumption}")
        lines.append(f"  reg {history_valid} = 0;")
        legacy = domain_rendering.domain.is_legacy_default
        if reset_history_valid is not None:
            lines.append(f"  reg {reset_history_valid} = 0;")
            lines.append(f"  always @({domain_rendering.sample_event}) begin")
            lines.append(f"    {reset_history_valid} <= 1;")
            lines.append("  end")
        if legacy:
            lines.append(f"  always @({domain_rendering.sample_event}) begin")
            lines.append(f"    {history_valid} <= 1;")
        else:
            lines.append(f"  always @({domain_rendering.history_event}) begin")
            lines.append(
                f"    if ({domain_rendering.external_reset_asserted}) "
                f"{history_valid} <= 0;"
            )
            if domain_rendering.release_tracker_name is not None:
                lines.append(
                    f"    else if ({domain_rendering.reset_active}) "
                    f"{history_valid} <= 0;"
                )
            lines.append(f"    else {history_valid} <= 1;")
            lines.append("  end")
            lines.append(f"  always @({domain_rendering.sample_event}) begin")
        guards: list[str] = []
        if any(
            item.cycle is ObservationCycle.PREVIOUS
            for item in predicate.observations()
        ):
            guards.append(reset_history_valid or history_valid)
        if concrete.property.reset_condition is not None:
            guards.append(f"!{domain_rendering.reset_active}")
        statement = (
            "assume"
            if concrete.property.kind is PropertyKind.ASSUMPTION
            else "assert"
        )
        if guards:
            lines.append(
                f"    if ({' && '.join(guards)}) "
                f"{statement}({rendered_predicate});"
            )
        else:
            lines.append(f"    {statement}({rendered_predicate});")
        lines.extend(("  end", "endmodule", ""))
        result = run_verilog_formal(
            rtl + "\n" + "\n".join(lines), top=wrapper,
            property_id=concrete.concrete_property_id, mode=mode,
            depth=depth, solver=solver, source_origin=concrete.property.source_origin,
            systemverilog=True,
        )
        object_value_items: list[tuple[str, str]] = []
        for ref, semantic_id, observation, _ in resolved_observations:
            object_value_items.append((semantic_id, observation.observation_token))
            if ref.implementation_state_id is not None:
                object_value_items.append((
                    f"implementation:{ref.implementation_state_id}",
                    observation.observation_token,
                ))
        object_values = tuple(object_value_items)
        if result.counterexample is not None:
            cycle_match = re.search(
                r"(?:step|cycle)\s+(\d+)", result.counterexample.raw_trace or "",
                re.IGNORECASE,
            )
            counterexample = replace(
                result.counterexample,
                cycle=int(cycle_match.group(1)) if cycle_match else None,
                values=object_values,
            )
            result = replace(result, counterexample=counterexample)
        results.append(RecursiveFormalResult.from_formal_result(
            concrete, result, artifact_hash=validated.formal_artifact_hash,
            object_values=object_values,
        ))
    return tuple(results)


__all__ = [
    "RegisterFormalSource", "emit_register_formal_source",
    "emit_receiver_credit_formal_source", "supports_receiver_credit_formal",
    "supports_register_formal", "validate_register_formal_artifact",
    "finalize_formal_verilog_artifact",
    "run_recursive_register_formal",
]
