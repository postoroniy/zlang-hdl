"""Formal observation binding and backend-artifact connection."""

from __future__ import annotations

from dataclasses import replace

from zlang.ir.csr import csr_internal_port_names, derived_state_bindings
from zlang.ir.formal_domains import (
    POWER_UP_FORMAL_DOMAIN_REASON,
    _formal_item_domain_from_design,
    _physical_domain_for_formal_item,
    _public_domain_bindings,
)
from zlang.ir.formal_models import (
    CoverProperty,
    FormalDesign,
    FormalError,
    FormalProperty,
    Ownership,
    SignalBinding,
)
from zlang.ir.formal_observations import (
    fifo_observation_id,
    port_observation_id,
    register_observation_id,
    request_response_observation_id,
    rule_fire_observation_id,
)
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import Module, PortDirection


def top_aggregate_ownership(module: Module) -> dict[str, Ownership]:
    """Return safety verification ownership for each projected top aggregate leaf."""
    from zlang.ir.top_abi import build_top_physical_abi
    return {
        leaf.leaf_semantic_id: (
            Ownership.ENVIRONMENT
            if leaf.direction is PortDirection.INPUT
            else Ownership.IMPLEMENTATION
        )
        for leaf in build_top_physical_abi(module).aggregate_leaves
    }


def signal_bindings(module: Module, *, rtl_module: str | None = None,
                    overrides: dict[str, str] | None = None,
                    include_rule_fire: bool = False) -> tuple[SignalBinding, ...]:
    """Publish explicit bindings for public ports and supported semantic state."""
    rtl_module = rtl_module or module.name
    overrides = overrides or {}
    result: list[SignalBinding] = []
    csr_internal = csr_internal_port_names(module.csr_access, module.csr_blocks)
    if module.clock is not None:
        result.append(SignalBinding(
            "clock", rtl_module, overrides.get("clock", module.clock), 1,
            "input", module.clock,
        ))
    if module.reset is not None:
        result.append(SignalBinding(
            "reset", rtl_module, overrides.get("reset", module.reset), 1,
            "input", module.clock,
        ))
    for port in module.ports:
        if port.name in csr_internal:
            continue
        port_id = port_observation_id(port.name)
        result.append(SignalBinding(port_id, rtl_module, overrides.get(port_id, port.name),
                                    port.type.width, port.direction.value, port.domain or module.clock))
    for register in module.registers:
        register_id = register_observation_id(register.name)
        result.append(SignalBinding(register_id, rtl_module,
                                    overrides.get(register_id, register.name), register.type.width,
                                    "internal", register.domain or module.clock,
                                    (
                                        register.initial.origin
                                        if register.initial is not None
                                        else None
                                    )))
    if include_rule_fire and module.resolved_transition is not None:
        for group in module.resolved_transition.action_groups:
            key = rule_fire_observation_id(group.rule_name)
            result.append(SignalBinding(
                key, rtl_module,
                overrides.get(key, f"rule_{group.rule_name}_fire"),
                1, "internal", module.clock, group.source_origin,
            ))
    for fifo in module.fifos:
        for signal, width in (("count", fifo.count_width), ("push", 1), ("pop", 1), ("empty", 1), ("full", 1), ("front", fifo.element_type.width)):
            key = fifo_observation_id(fifo.name, signal)
            result.append(SignalBinding(
                key, rtl_module, overrides.get(key, f"{fifo.name}_{signal}"),
                width, "internal", module.clock,
                fifo.data.origin if fifo.data is not None else fifo.source_origin,
            ))
    for connection in module.request_response_connections:
        tracker = (f"rr_{connection.request.source.owner}_{connection.request.source.name}_"
                   f"{connection.response.destination.owner}_outstanding")
        tracker_width = max(1, connection.max_outstanding.bit_length())
        request_width = (
            max(1, connection.request.request_buffer_depth.bit_length())
            if connection.request.request_buffer_depth else tracker_width
        )
        response_width = (
            max(1, connection.response.response_buffer_depth.bit_length())
            if connection.response.response_buffer_depth else tracker_width
        )
        for signal, width, rtl_name in (
            ("outstanding", tracker_width, tracker),
            ("request_accept", 1, tracker + "_request_accept"),
            ("response_consume", 1, tracker + "_response_consume"),
            ("request_occupancy", request_width, tracker + "_request_occupancy"),
            ("response_occupancy", response_width, tracker + "_response_occupancy"),
        ):
            key = request_response_observation_id(connection.semantic_id, signal)
            result.append(SignalBinding(key, rtl_module, overrides.get(key, rtl_name), width,
                                        "internal", module.clock, connection.source_origin))
    for block_ordinal, block in enumerate(module.csr_blocks):
        fields = {}
        block_id = block.identity
        for register_ordinal, register in enumerate(block.registers):
            register_id = register.identity
            for field_ordinal, field in enumerate(register.fields):
                field_id = field.identity
                if field_id is None:
                    from zlang.ir.csr import (
                        CsrBlockIdentity, CsrFieldIdentity, CsrRegisterIdentity,
                    )
                    effective_block = block_id or CsrBlockIdentity(
                        module.source_hash or module.name, block_ordinal
                    )
                    effective_register = register_id or CsrRegisterIdentity(
                        effective_block, register_ordinal
                    )
                    field_id = CsrFieldIdentity(effective_register, field_ordinal)
                fields[field_id] = (register, field)
        for state in derived_state_bindings(
            block, module_identity=module.source_hash or module.name,
            block_ordinal=block_ordinal, clock_domain=module.clock,
            reset_domain=module.reset,
        ):
            register, field = fields[state.csr_field_id]
            physical = f"csr_{block.name}_{register.name.lower()}_{field.name}"
            hit = f"csr_{block.name}_{register.name.lower()}_write_hit"
            value = physical + "_write_value"
            for key, width, rtl_name in (
                (state.semantic_state_id, state.field_width, physical),
                (state.write_hit_id, 1, hit),
                (state.write_value_id, state.field_width, value),
            ):
                result.append(SignalBinding(
                    key, rtl_module, overrides.get(key, rtl_name), width,
                    "internal", state.clock_domain, state.source_origin,
                ))
    for port in module.ports:
        if port.name in csr_internal:
            continue
        if port.protocol is InterfaceProtocol.READY_VALID:
            forward = port.direction.value
            reverse = (
                PortDirection.OUTPUT.value
                if port.direction is PortDirection.INPUT
                else PortDirection.INPUT.value
            )
            for signal, width, direction in (
                ("valid", 1, forward),
                ("ready", 1, reverse),
                ("payload", port.type.width, forward),
            ):
                key = port_observation_id(port.name, signal)
                result.append(SignalBinding(
                    key, rtl_module,
                    overrides.get(key, f"{port.name}_{signal}"),
                    width, direction, port.domain or module.clock,
                ))
        elif port.protocol is InterfaceProtocol.CREDIT:
            sender = port.direction is PortDirection.OUTPUT
            credit_fields = [
                ("payload", port.type.width, port.direction.value),
                ("send", 1, port.direction.value),
                (
                    "return", 1,
                    PortDirection.INPUT.value if sender
                    else PortDirection.OUTPUT.value,
                ),
            ]
            if sender:
                credit_fields.append((
                    "credits", max(1, (port.capacity or 1).bit_length()),
                    "internal",
                ))
            else:
                credit_fields.append((
                    "occupancy", max(1, (port.capacity or 1).bit_length()),
                    "internal",
                ))
            for signal, width, direction in credit_fields:
                key = port_observation_id(port.name, signal)
                result.append(SignalBinding(
                    key, rtl_module,
                    overrides.get(key, f"{port.name}_{signal}"),
                    width, direction, port.domain or module.clock,
                ))
        elif port.protocol is InterfaceProtocol.PACKET:
            forward = port.direction.value
            reverse = (
                PortDirection.OUTPUT.value
                if port.direction is PortDirection.INPUT
                else PortDirection.INPUT.value
            )
            for signal, width, direction in (
                ("payload", port.type.width, forward),
                ("valid", 1, forward),
                ("last", 1, forward),
                ("ready", 1, reverse),
            ):
                key = port_observation_id(port.name, signal)
                result.append(SignalBinding(
                    key, rtl_module,
                    overrides.get(key, f"{port.name}_{signal}"),
                    width, direction, port.domain or module.clock,
                ))
        elif port.protocol is InterfaceProtocol.VC_CREDIT:
            forward = port.direction.value
            reverse = (
                PortDirection.OUTPUT.value
                if port.direction is PortDirection.INPUT
                else PortDirection.INPUT.value
            )
            vc_width = max(1, ((port.virtual_channels or 1) - 1).bit_length())
            for signal, width, direction in (
                ("payload", port.type.width, forward),
                ("vc", vc_width, forward),
                ("send", 1, forward),
                ("return", 1, reverse),
                ("return_vc", vc_width, reverse),
            ):
                key = port_observation_id(port.name, signal)
                result.append(SignalBinding(
                    key, rtl_module,
                    overrides.get(key, f"{port.name}_{signal}"),
                    width, direction, port.domain or module.clock,
                ))
    for interface in module.request_responses:
        requester = interface.role.value == "requester"
        for channel, signal, width, implementation_owned in (
            ("request", "payload", interface.request_type.width, requester),
            ("request", "valid", 1, requester),
            ("request", "ready", 1, not requester),
            ("response", "payload", interface.response_type.width, not requester),
            ("response", "valid", 1, not requester),
            ("response", "ready", 1, requester),
        ):
            key = port_observation_id(interface.name, f"{channel}.{signal}")
            direction = (
                PortDirection.OUTPUT.value
                if implementation_owned else PortDirection.INPUT.value
            )
            result.append(SignalBinding(
                key, rtl_module,
                overrides.get(key, f"{interface.name}_{channel}_{signal}"),
                width, direction, module.clock,
            ))
    return tuple(result)


def connect_formal_design(design: FormalDesign, artifact: object) -> FormalDesign:
    """Bind one semantic design to backend-published formal observation ports.

    The adapter consumes the exact tokens present in ``BackendArtifact`` v4;
    it never derives a token from an RTL or source name.  The returned design
    is the only form accepted by executable harness/SBY emission.
    """

    # This low-level connector is exported as part of ``zlang.ir`` as well as
    # through the compiler-owned wrapper.  Keep the physical-reset gate here so
    # callers cannot bypass the frozen safety verification clock/reset model by constructing
    # properties directly and then attaching a current BackendArtifact.  The
    # gate is evaluated for each exact goal domain below; unrelated domains do
    # not poison otherwise executable goals.
    if design.non_executable_reason is not None:
        return design
    physical_domains = tuple(getattr(artifact, "physical_domains", ()))

    artifact_hash = getattr(artifact, "artifact_hash", None)
    formal_artifact_hash = getattr(artifact, "formal_artifact_hash", None)
    backend = getattr(artifact, "backend", None)
    implementation_text = getattr(artifact, "text", None)
    if not all(isinstance(item, str) and item for item in (
        artifact_hash, backend, implementation_text,
    )):
        raise FormalError("connected formal execution requires a complete BackendArtifact")
    try:
        getattr(artifact, "binding_map")().validate()
    except (AttributeError, ValueError) as error:
        raise FormalError(f"invalid backend binding map: {error}") from error

    recursive = tuple(getattr(artifact, "recursive_bindings", ()))
    observations = {
        item.semantic_binding_id: item
        for item in getattr(artifact, "formal_observations", ())
    }
    if len(observations) != len(tuple(getattr(artifact, "formal_observations", ()))):
        raise FormalError("backend artifact contains duplicate formal observation IDs")
    root_depth = min(
        (len(item.physical_instance_path) for item in recursive), default=None
    )
    root_recursive: dict[str, object] = {}
    if root_depth is not None:
        for item in recursive:
            if len(item.physical_instance_path) != root_depth:
                continue
            if item.local_semantic_id in root_recursive:
                raise FormalError(
                    f"backend artifact has duplicate root observation "
                    f"'{item.local_semantic_id}'"
                )
            root_recursive[item.local_semantic_id] = item

    public = tuple(getattr(artifact, "bindings", ()))
    public_by_id = {item.semantic_signal_id: item for item in public}
    if len(public_by_id) != len(public):
        raise FormalError("backend artifact contains duplicate public signal bindings")

    accepted_hashes = {artifact_hash}
    if formal_artifact_hash:
        accepted_hashes.add(formal_artifact_hash)

    def resolve_observation(
        semantic_id: str,
        expected_width: int,
        *,
        goal_clock: object,
        goal_reset: object | None,
    ) -> SignalBinding:
        if semantic_id in {"clock", "reset"}:
            item = goal_clock if semantic_id == "clock" else goal_reset
            if item is None or not item.physical_available or not item.rtl_path:
                raise FormalError(
                    f"backend artifact has no physical goal-domain binding "
                    f"for '{semantic_id}'"
                )
            if item.width != expected_width:
                raise FormalError(
                    f"backend binding width mismatch for '{semantic_id}': "
                    f"expected {expected_width}, got {item.width}"
                )
            return SignalBinding(
                semantic_id, item.rtl_module, item.rtl_path, item.width,
                "input", item.clock_domain, item.source_origin,
            )
        recursive_item = root_recursive.get(semantic_id)
        if recursive_item is None:
            raise FormalError(
                f"backend artifact has no root formal binding for '{semantic_id}'"
            )
        observation = observations.get(recursive_item.semantic_binding_id)
        token = getattr(observation, "observation_token", None)
        module_name = getattr(recursive_item, "rtl_module", None)
        if (
            observation is None
            or not observation.physical_available
            or not isinstance(token, str)
            or not token
            or not recursive_item.physical_available
            or not isinstance(module_name, str)
            or not module_name
        ):
            raise FormalError(
                f"formal observation unavailable for '{semantic_id}'"
            )
        if observation.width != expected_width or recursive_item.width != expected_width:
            raise FormalError(
                f"formal observation width mismatch for '{semantic_id}': "
                f"expected {expected_width}"
            )
        if observation.artifact_hash not in accepted_hashes:
            raise FormalError(
                f"formal observation artifact hash mismatch for '{semantic_id}'"
            )
        return SignalBinding(
            semantic_id, module_name, token, expected_width, "output",
            observation.clock_domain, recursive_item.source_origin,
        )

    # Bind each property independently.  A partially observable backend may
    # execute the exact subset it publishes, but every omitted property keeps
    # an explicit reason and is never weakened or wired to a guessed name.
    connected_by_id: dict[str, SignalBinding] = {}
    connected_modules: set[str] = set()
    connected_properties: list[FormalProperty] = []
    domain_blocked: set[str] = set()

    def goal_domain_bindings(
        item: FormalProperty | CoverProperty,
    ) -> tuple[object | None, object | None, str | None]:
        source_domain, reason = _formal_item_domain_from_design(item, design)
        if reason is not None or source_domain is None:
            return None, None, reason
        physical, reason = _physical_domain_for_formal_item(
            item, physical_domains, source_domain
        )
        if reason is not None:
            return None, None, reason
        clock_binding, reset_binding, reason = _public_domain_bindings(
            item, public
        )
        if reason is not None:
            return None, None, reason
        if physical is not None:
            if (
                getattr(clock_binding, "rtl_module", None)
                != getattr(physical, "rtl_module", None)
                or getattr(clock_binding, "rtl_path", None)
                != getattr(physical, "rtl_clock_path", None)
                or (
                    reset_binding is not None
                    and (
                        getattr(reset_binding, "rtl_module", None)
                        != getattr(physical, "rtl_module", None)
                        or getattr(reset_binding, "rtl_path", None)
                        != getattr(physical, "rtl_reset_path", None)
                    )
                )
            ):
                return None, None, (
                    f"formal goal '{item.id}' public clock/reset bindings "
                    "disagree with its physical-domain manifest"
                )
        return clock_binding, reset_binding, None

    for item in design.properties:
        if item.non_executable_reason is not None:
            connected_properties.append(item)
            continue
        if item.predicate is None:
            raise FormalError(
                f"property '{item.id}' has only a legacy string predicate and "
                "cannot be executed"
            )
        goal_clock, goal_reset, domain_reason = goal_domain_bindings(item)
        if domain_reason is not None:
            domain_blocked.add(item.id)
            connected_properties.append(replace(
                item, non_executable_reason=domain_reason,
            ))
            continue

        widths: dict[str, int] = {"clock": 1}
        if goal_reset is not None:
            widths["reset"] = 1
        for observation in item.predicate.observations():
            previous = widths.setdefault(
                observation.semantic_signal_id, observation.width
            )
            if previous != observation.width:
                raise FormalError(
                    f"observation '{observation.semantic_signal_id}' has "
                    "inconsistent predicate widths"
                )
        try:
            resolved = tuple(
                resolve_observation(
                    semantic_id,
                    width,
                    goal_clock=goal_clock,
                    goal_reset=goal_reset,
                )
                for semantic_id, width in widths.items()
            )
        except FormalError as error:
            connected_properties.append(replace(
                item, non_executable_reason=str(error)
            ))
            continue
        for binding in resolved:
            previous = connected_by_id.get(binding.semantic_signal_id)
            if previous is not None and previous != binding:
                raise FormalError(
                    f"inconsistent connected binding for "
                    f"'{binding.semantic_signal_id}'"
                )
            connected_by_id[binding.semantic_signal_id] = binding
            if binding.semantic_signal_id not in {"clock", "reset"}:
                connected_modules.add(binding.rtl_module)
        connected_properties.append(item)
    connected_covers: list[CoverProperty] = []
    for item in design.covers:
        if item.non_executable_reason is not None:
            connected_covers.append(item)
            continue
        if item.predicate is None:
            raise FormalError(
                f"cover property '{item.id}' has no structured executable predicate"
            )
        goal_clock, goal_reset, domain_reason = goal_domain_bindings(item)
        if domain_reason is not None:
            domain_blocked.add(item.id)
            connected_covers.append(replace(
                item, non_executable_reason=domain_reason,
            ))
            continue

        widths: dict[str, int] = {"clock": 1}
        if goal_reset is not None:
            widths["reset"] = 1
        for observation in item.predicate.observations():
            previous = widths.setdefault(
                observation.semantic_signal_id, observation.width
            )
            if previous != observation.width:
                raise FormalError(
                    f"observation '{observation.semantic_signal_id}' has "
                    "inconsistent predicate widths"
                )
        try:
            resolved = tuple(
                resolve_observation(
                    semantic_id,
                    width,
                    goal_clock=goal_clock,
                    goal_reset=goal_reset,
                )
                for semantic_id, width in widths.items()
            )
        except FormalError as error:
            connected_covers.append(replace(
                item, non_executable_reason=str(error)
            ))
            continue
        for binding in resolved:
            previous = connected_by_id.get(binding.semantic_signal_id)
            if previous is not None and previous != binding:
                raise FormalError(
                    f"inconsistent connected binding for "
                    f"'{binding.semantic_signal_id}'"
                )
            connected_by_id[binding.semantic_signal_id] = binding
            if binding.semantic_signal_id not in {"clock", "reset"}:
                connected_modules.add(binding.rtl_module)
        connected_covers.append(item)

    original_executable = tuple(
        item for item in (*design.properties, *design.covers)
        if item.non_executable_reason is None
    )
    if original_executable and all(
        item.id in domain_blocked for item in original_executable
    ):
        blocked = {
            item.non_executable_reason
            for item in (*connected_properties, *connected_covers)
            if item.id in domain_blocked
        }
        reason = (
            POWER_UP_FORMAL_DOMAIN_REASON
            if blocked == {POWER_UP_FORMAL_DOMAIN_REASON}
            else "all formal goals have unsupported or unresolved physical domains"
        )
        return replace(
            design,
            properties=tuple(connected_properties),
            covers=tuple(connected_covers),
            bindings=(),
            connected_backend=None,
            connected_artifact_hash=None,
            connected_module=None,
            implementation_text=None,
            dut_ports=(),
            non_executable_reason=reason,
        )
    if len(connected_modules) > 1:
        raise FormalError("one safety verification harness cannot bind multiple root RTL modules")

    dut_ports: list[SignalBinding] = []
    for item in public:
        role = getattr(item.role, "value", str(item.role))
        if role not in {"input", "output", "clock", "reset"}:
            continue
        if not item.physical_available or not item.rtl_path:
            continue
        direction = "input" if role in {"input", "clock", "reset"} else "output"
        dut_ports.append(SignalBinding(
            item.semantic_signal_id, item.rtl_module, item.rtl_path, item.width,
            direction, item.clock_domain, item.source_origin,
        ))
    # A valid artifact can deliberately publish no usable formal observation
    # (for example, after one required state projection is removed in a
    # fail-closed test).  It is still a connected implementation artifact; all
    # affected properties above carry explicit non-executable reasons.  Keep
    # the declared artifact module so report/SBY generation can distinguish
    # this case from an entirely unconnected semantic design.
    module_name = next(iter(connected_modules), None)
    if module_name is None:
        candidate_module = getattr(artifact, "module", None)
        if not isinstance(candidate_module, str) or not candidate_module:
            raise FormalError(
                "connected formal artifact publishes no implementation module"
            )
        module_name = candidate_module
    return replace(
        design,
        properties=tuple(connected_properties),
        covers=tuple(connected_covers),
        bindings=tuple(connected_by_id.values()),
        connected_backend=backend,
        connected_artifact_hash=formal_artifact_hash or artifact_hash,
        connected_module=module_name,
        implementation_text=implementation_text,
        dut_ports=tuple(dut_ports),
        non_executable_reason=None,
    )
