"""Formal property-family generation from typed module IR."""

from __future__ import annotations

from dataclasses import replace

from zlang.ir.csr import derived_state_bindings
from zlang.ir.formal_binding import signal_bindings
from zlang.ir.formal_models import (
    CoverProperty,
    FormalDesign,
    FormalError,
    FormalProperty,
    FormalPropertyClassification,
    Ownership,
    PropertyKind,
    TemporalForm,
)
from zlang.ir.formal_observations import (
    RequestResponseObservationSignal,
    fifo_observation_id,
    port_observation_id,
    register_observation_id,
    request_response_observation_id,
    rule_fire_observation_id,
)
from zlang.ir.formal_predicate_generation import (
    _and,
    _binary,
    _bit,
    _bit_observation,
    _constant,
    _equal,
    _formal_signedness,
    _implies,
    _not,
    _observation,
    _or,
    _ordered,
    _previous,
    _property,
    _resize,
    _semantic_predicate,
    _stable_id,
    _type_range,
    _without_previous_reset,
)
from zlang.ir.formal_predicates import (
    FormalBinaryOperator,
    FormalPredicate,
    FormalSignedness,
    FormalUnaryOperator,
    Mux as PredicateMux,
    ObservationCycle,
    Unary as PredicateUnary,
)
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import Module, PortDirection
from zlang.ir.types import EnumType, UIntType
from zlang.ir.verification import VerificationGoalKind


def _append_register_properties(
    module: Module,
    properties: list[FormalProperty],
) -> None:
    for register in module.registers:
        state = _observation(register_observation_id(register.name), register.type)
        signedness = _formal_signedness(register.type)
        if signedness is FormalSignedness.BIT:
            minimum = 0
            maximum = 1
            bounds = _or(
                _equal(state, _constant(0, 1, FormalSignedness.BIT)),
                _equal(state, _constant(1, 1, FormalSignedness.BIT)),
            )
        elif signedness is FormalSignedness.BITS:
            minimum = 0
            maximum = (1 << register.type.width) - 1
            bounds = _equal(state, state)
        elif signedness is FormalSignedness.SIGNED:
            minimum = -(1 << (register.type.width - 1))
            maximum = (1 << (register.type.width - 1)) - 1
        elif isinstance(register.type, EnumType):
            if register.type.explicit_codes is not None:
                minimum = min(register.type.codes)
                maximum = max(register.type.codes)
                comparisons = tuple(
                    _equal(
                        state,
                        _constant(code, register.type.width, signedness),
                    )
                    for code in register.type.codes
                )
                bounds = comparisons[0]
                for comparison in comparisons[1:]:
                    bounds = _or(bounds, comparison)
            else:
                minimum = 0
                maximum = len(register.type.members) - 1
        else:
            minimum = 0
            maximum = (1 << register.type.width) - 1
        if (
            signedness not in {FormalSignedness.BIT, FormalSignedness.BITS}
            and not (
                isinstance(register.type, EnumType)
                and register.type.explicit_codes is not None
            )
        ):
            bounds = _and(
                _ordered(
                    FormalBinaryOperator.GREATER_EQUAL,
                    state,
                    _constant(minimum, register.type.width, signedness),
                ),
                _ordered(
                    FormalBinaryOperator.LESS_EQUAL,
                    state,
                    _constant(maximum, register.type.width, signedness),
                ),
            )
        properties.append(_property(
            "register", register.name + ".range",
            _type_range(register.type).replace("state", register.name), module,
            ownership=Ownership.IMPLEMENTATION,
            generated_from=f"register:{register.name}", predicate=bounds,
            origin=(
                register.initial.origin
                if register.initial is not None else None
            ),
            classification=(
                FormalPropertyClassification.BEHAVIORAL
                if isinstance(register.type, EnumType)
                else FormalPropertyClassification.REPRESENTATION_INVARIANT
            ),
        ))
        if module.reset is not None and register.initial is not None:
            initial_value = getattr(register.initial, "value", 0)
            reset_predicate = _implies(
                _bit_observation("reset", ObservationCycle.PREVIOUS),
                _equal(
                    state,
                    _constant(initial_value, register.type.width, signedness),
                ),
            )
            properties.append(_property(
                "register", register.name + ".reset",
                f"previous({module.reset}) -> {register.name} == {initial_value}",
                module, ownership=Ownership.IMPLEMENTATION,
                generated_from=f"register:{register.name}:reset",
                predicate=reset_predicate, temporal=TemporalForm.NEXT_CYCLE,
                antecedent=module.reset,
                consequent=f"{register.name} == {initial_value}",
                origin=register.initial.origin, reset_condition=None,
            ))


def _append_fifo_properties(
    module: Module,
    properties: list[FormalProperty],
) -> None:
    for fifo in module.fifos:
        n, d = fifo.name, fifo.depth
        origin = fifo.data.origin if fifo.data is not None else fifo.source_origin
        count_type = UIntType(fifo.count_width)
        count = _observation(fifo_observation_id(n, "count"), count_type)
        push = _bit_observation(fifo_observation_id(n, "push"))
        pop = _bit_observation(fifo_observation_id(n, "pop"))
        empty = _bit_observation(fifo_observation_id(n, "empty"))
        full = _bit_observation(fifo_observation_id(n, "full"))
        front = _observation(
            fifo_observation_id(n, "front"), fifo.element_type
        )
        bounds = _ordered(
            FormalBinaryOperator.LESS_EQUAL,
            count,
            _constant(d, fifo.count_width, FormalSignedness.UNSIGNED),
        )
        no_pop = _or(_not(empty), _not(pop))
        no_push = _or(_or(_not(full), _not(push)), pop)
        work_width = fifo.count_width + 2
        previous_count = _resize(
            _previous(count), work_width, FormalSignedness.UNSIGNED
        )
        previous_push = _resize(
            _previous(push), work_width, FormalSignedness.UNSIGNED
        )
        previous_pop = _resize(
            _previous(pop), work_width, FormalSignedness.UNSIGNED
        )
        conservation_value = _binary(
            FormalBinaryOperator.SUBTRACT,
            _binary(
                FormalBinaryOperator.ADD,
                previous_count,
                previous_push,
                work_width,
                FormalSignedness.UNSIGNED,
            ),
            previous_pop,
            work_width,
            FormalSignedness.UNSIGNED,
        )
        conservation = _without_previous_reset(
            module,
            _equal(
                _resize(count, work_width, FormalSignedness.UNSIGNED),
                conservation_value,
            ),
        )
        front_antecedent = _and(
            _ordered(
                FormalBinaryOperator.GREATER,
                _previous(count),
                _constant(0, fifo.count_width, FormalSignedness.UNSIGNED),
            ),
            _not(_previous(pop)),
        )
        if module.reset is not None:
            front_antecedent = _and(
                front_antecedent,
                _not(_bit_observation("reset", ObservationCycle.PREVIOUS)),
            )
        front_stable = _implies(
            front_antecedent,
            _equal(front, _previous(front)),
        )
        for suffix, display, predicate, temporal in (
            ("bounds", f"{n}.count >= 0 && {n}.count <= {d}", bounds, TemporalForm.SAME_CYCLE),
            ("no_pop_empty", f"{n}.empty == 0 || {n}.pop == 0", no_pop, TemporalForm.SAME_CYCLE),
            ("no_push_full", f"{n}.full == 0 || {n}.push == 0 || {n}.pop == 1", no_push, TemporalForm.SAME_CYCLE),
            ("conservation", f"{n}.count == previous({n}.count + {n}.push - {n}.pop)", conservation, TemporalForm.NEXT_CYCLE),
            (
                "front_stable",
                f"previous({n}.count > 0 && !{n}.pop) -> "
                f"{n}.front == previous({n}.front)",
                front_stable,
                TemporalForm.NEXT_CYCLE,
            ),
        ):
            properties.append(_property(
                "fifo", f"{n}.{suffix}", display, module,
                ownership=Ownership.IMPLEMENTATION, generated_from=f"fifo:{n}",
                predicate=predicate, temporal=temporal, origin=origin,
            ))


def _append_protocol_port_properties(
    module: Module,
    properties: list[FormalProperty],
) -> None:
    for port in module.ports:
        if port.protocol is InterfaceProtocol.READY_VALID:
            prefix = port.name
            ownership = (Ownership.ENVIRONMENT if port.direction is PortDirection.INPUT
                         else Ownership.SOURCE_ENDPOINT)
            kind = PropertyKind.ASSUMPTION if ownership is Ownership.ENVIRONMENT else PropertyKind.ASSERTION
            valid = _bit_observation(port_observation_id(prefix, "valid"))
            ready = _bit_observation(port_observation_id(prefix, "ready"))
            payload = _observation(
                port_observation_id(prefix, "payload"), port.type
            )
            antecedent = _and(_previous(valid), _not(_previous(ready)))
            if module.reset is not None:
                antecedent = _and(
                    antecedent,
                    _not(_bit_observation("reset", ObservationCycle.PREVIOUS)),
                )
            stable = _and(valid, _equal(payload, _previous(payload)))
            properties.append(_property(
                "ready_valid", f"{prefix}.stall",
                f"previous({prefix}.valid && !{prefix}.ready) -> "
                f"{prefix}.valid && stable({prefix}.payload)",
                module, ownership=ownership,
                generated_from=f"ready_valid:{prefix}",
                predicate=_implies(antecedent, stable), kind=kind,
                temporal=TemporalForm.NEXT_CYCLE,
                antecedent=f"{prefix}.valid && !{prefix}.ready",
                consequent=f"{prefix}.valid && stable({prefix}.payload)",
            ))
        if port.protocol is InterfaceProtocol.CREDIT:
            prefix = port.name
            capacity = port.capacity or 0
            credit_width = max(1, capacity.bit_length())
            send = _bit_observation(port_observation_id(prefix, "send"))
            returned = _bit_observation(port_observation_id(prefix, "return"))
            sender = port.direction is PortDirection.OUTPUT
            state_name = "credits" if sender else "occupancy"
            state = _observation(
                port_observation_id(prefix, state_name), UIntType(credit_width)
            )
            bounds = _ordered(
                FormalBinaryOperator.LESS_EQUAL, state,
                _constant(capacity, credit_width, FormalSignedness.UNSIGNED),
            )
            work_width = credit_width + 2
            previous_state = _resize(
                _previous(state), work_width, FormalSignedness.UNSIGNED
            )
            previous_send = _resize(
                _previous(send), work_width, FormalSignedness.UNSIGNED
            )
            previous_return = _resize(
                _previous(returned), work_width, FormalSignedness.UNSIGNED
            )
            if sender:
                # A sender consumes one available credit when it sends and
                # replenishes one when the environment returns a credit.
                previous_value = _binary(
                    FormalBinaryOperator.ADD,
                    _binary(
                        FormalBinaryOperator.SUBTRACT,
                        previous_state,
                        previous_send,
                        work_width,
                        FormalSignedness.UNSIGNED,
                    ),
                    previous_return,
                    work_width, FormalSignedness.UNSIGNED,
                )
            else:
                # A receiver records accepted incoming sends until its local
                # implementation returns the corresponding credits.
                previous_value = _binary(
                    FormalBinaryOperator.SUBTRACT,
                    _binary(
                        FormalBinaryOperator.ADD,
                        previous_state,
                        previous_send,
                        work_width,
                        FormalSignedness.UNSIGNED,
                    ),
                    previous_return,
                    work_width,
                    FormalSignedness.UNSIGNED,
                )
            conservation = _without_previous_reset(
                module,
                _equal(
                    _resize(state, work_width, FormalSignedness.UNSIGNED),
                    previous_value,
                ),
            )

            zero = _constant(0, credit_width, FormalSignedness.UNSIGNED)
            below_capacity = _ordered(
                FormalBinaryOperator.LESS, state,
                _constant(capacity, credit_width, FormalSignedness.UNSIGNED),
            )
            above_zero = _ordered(FormalBinaryOperator.GREATER, state, zero)
            if sender:
                implementation_legality = _or(_not(send), above_zero)
                environment_legality = _or(
                    _or(_not(returned), send), below_capacity
                )
                implementation_suffix = "transfer"
                implementation_display = (
                    f"{prefix}.send == 0 || {prefix}.credits > 0"
                )
                environment_suffix = "return_capacity"
                environment_display = (
                    f"{prefix}.return == 0 || {prefix}.send == 1 || "
                    f"{prefix}.credits < {capacity}"
                )
                implementation_ownership = Ownership.SOURCE_ENDPOINT
                conservation_display = (
                    f"{prefix}.credits == previous({prefix}.credits - "
                    f"{prefix}.send + {prefix}.return)"
                )
                reset_value = capacity
                unavailable_reason = None
            else:
                implementation_legality = _or(
                    _or(_not(returned), send), above_zero
                )
                environment_legality = _or(
                    _or(_not(send), returned), below_capacity
                )
                implementation_suffix = "return"
                implementation_display = (
                    f"{prefix}.return == 0 || {prefix}.send == 1 || "
                    f"{prefix}.occupancy > 0"
                )
                environment_suffix = "send_capacity"
                environment_display = (
                    f"{prefix}.send == 0 || {prefix}.return == 1 || "
                    f"{prefix}.occupancy < {capacity}"
                )
                implementation_ownership = Ownership.SINK_ENDPOINT
                conservation_display = (
                    f"{prefix}.occupancy == previous({prefix}.occupancy + "
                    f"{prefix}.send - {prefix}.return)"
                )
                reset_value = 0
                # Applicability is a property of one concrete backend route,
                # not of the backend-independent safety verification property.  Receiver
                # occupancy is real implementation state and is already part
                # of the frozen credit predicate.  Publish its semantic
                # binding below and let each backend either connect it or
                # report the exact missing observation.
                unavailable_reason = None

            for suffix, display, predicate, temporal, ownership, kind in (
                (
                    "bounds",
                    f"{prefix}.{state_name} >= 0 && "
                    f"{prefix}.{state_name} <= {capacity}",
                    bounds,
                    TemporalForm.SAME_CYCLE,
                    Ownership.IMPLEMENTATION,
                    PropertyKind.ASSERTION,
                ),
                (
                    implementation_suffix,
                    implementation_display,
                    implementation_legality,
                    TemporalForm.SAME_CYCLE,
                    implementation_ownership,
                    PropertyKind.ASSERTION,
                ),
                (
                    environment_suffix,
                    environment_display,
                    environment_legality,
                    TemporalForm.SAME_CYCLE,
                    Ownership.ENVIRONMENT,
                    PropertyKind.ASSUMPTION,
                ),
                (
                    "conservation",
                    conservation_display,
                    conservation,
                    TemporalForm.NEXT_CYCLE,
                    Ownership.IMPLEMENTATION,
                    PropertyKind.ASSERTION,
                ),
            ):
                properties.append(_property(
                    "credit", f"{prefix}.{suffix}", display, module,
                    ownership=ownership, kind=kind,
                    generated_from=f"credit:{prefix}", predicate=predicate,
                    temporal=temporal,
                    non_executable_reason=unavailable_reason,
                ))
            if module.reset is not None:
                properties.append(_property(
                    "credit", f"{prefix}.reset",
                    f"previous({module.reset}) -> {prefix}.{state_name} == "
                    f"{reset_value}",
                    module, ownership=Ownership.IMPLEMENTATION,
                    generated_from=f"credit:{prefix}:reset",
                    predicate=_implies(
                        _bit_observation("reset", ObservationCycle.PREVIOUS),
                        _equal(
                            state,
                            _constant(
                                reset_value,
                                credit_width,
                                FormalSignedness.UNSIGNED,
                            ),
                        ),
                    ),
                    temporal=TemporalForm.NEXT_CYCLE,
                    antecedent=module.reset,
                    consequent=f"{prefix}.{state_name} == {reset_value}",
                    reset_condition=None,
                    non_executable_reason=unavailable_reason,
                ))


def _append_request_response_connection_properties(
    module: Module,
    properties: list[FormalProperty],
) -> None:
    # The parent connection owns one accepted-request ledger for both physical
    # ready/valid channels.  Keep these accounting properties separate from
    # the endpoint properties above so buffered and accepted work cannot be
    # conflated by a backend.
    for connection in module.request_response_connections:
        ids = {
            signal.value: request_response_observation_id(
                connection.semantic_id, signal
            )
            for signal in RequestResponseObservationSignal
        }
        tracker_width = max(1, connection.max_outstanding.bit_length())
        request_width = max(
            1,
            connection.request.request_buffer_depth.bit_length()
            if connection.request.request_buffer_depth
            else tracker_width,
        )
        response_width = max(
            1,
            connection.response.response_buffer_depth.bit_length()
            if connection.response.response_buffer_depth
            else tracker_width,
        )
        outstanding = _observation(ids["outstanding"], UIntType(tracker_width))
        request_accept = _bit_observation(ids["request_accept"])
        response_consume = _bit_observation(ids["response_consume"])
        request_occupancy = _observation(
            ids["request_occupancy"], UIntType(request_width)
        )
        response_occupancy = _observation(
            ids["response_occupancy"], UIntType(response_width)
        )
        generated = f"request_response:{connection.semantic_id}"
        bounds = _ordered(
            FormalBinaryOperator.LESS_EQUAL, outstanding,
            _constant(connection.max_outstanding, tracker_width, FormalSignedness.UNSIGNED),
        )
        no_response = _or(
            _not(response_consume),
            _or(
                request_accept,
                _ordered(
                    FormalBinaryOperator.GREATER, outstanding,
                    _constant(0, tracker_width, FormalSignedness.UNSIGNED),
                ),
            ),
        )
        work_width = tracker_width + 2
        next_outstanding = _binary(
            FormalBinaryOperator.SUBTRACT,
            _binary(
                FormalBinaryOperator.ADD,
                _resize(_previous(outstanding), work_width, FormalSignedness.UNSIGNED),
                _resize(_previous(request_accept), work_width, FormalSignedness.UNSIGNED),
                work_width, FormalSignedness.UNSIGNED,
            ),
            _resize(_previous(response_consume), work_width, FormalSignedness.UNSIGNED),
            work_width, FormalSignedness.UNSIGNED,
        )
        conservation = _without_previous_reset(
            module,
            _equal(
                _resize(outstanding, work_width, FormalSignedness.UNSIGNED),
                next_outstanding,
            ),
        )
        response_count = _ordered(
            FormalBinaryOperator.LESS_EQUAL,
            _resize(response_occupancy, work_width, FormalSignedness.UNSIGNED),
            _resize(outstanding, work_width, FormalSignedness.UNSIGNED),
        )
        properties.extend((
            _property(
                "request_response", f"{connection.semantic_id}.bounds",
                f"{ids['outstanding']} >= 0 && {ids['outstanding']} <= "
                f"{connection.max_outstanding}", module,
                ownership=Ownership.IMPLEMENTATION, generated_from=generated,
                predicate=bounds, origin=connection.source_origin,
            ),
            _property(
                "request_response", f"{connection.semantic_id}.no_response_without_request",
                f"{ids['response_consume']} == 0 || {ids['request_accept']} == 1 || "
                f"{ids['outstanding']} > 0", module,
                ownership=Ownership.IMPLEMENTATION, generated_from=generated,
                predicate=no_response, origin=connection.source_origin,
            ),
            _property(
                "request_response", f"{connection.semantic_id}.response_count",
                f"{ids['response_occupancy']} <= {ids['outstanding']}", module,
                ownership=Ownership.IMPLEMENTATION, generated_from=generated,
                predicate=response_count, origin=connection.source_origin,
            ),
            _property(
                "request_response", f"{connection.semantic_id}.conservation",
                f"{ids['outstanding']} == previous({ids['outstanding']} + "
                f"{ids['request_accept']} - {ids['response_consume']})", module,
                ownership=Ownership.IMPLEMENTATION, generated_from=generated,
                predicate=conservation, temporal=TemporalForm.NEXT_CYCLE,
                origin=connection.source_origin,
            ),
        ))
        if module.reset is not None:
            reset_clear = _and(
                _equal(outstanding, _constant(0, tracker_width, FormalSignedness.UNSIGNED)),
                _and(
                    _equal(request_occupancy, _constant(0, request_width, FormalSignedness.UNSIGNED)),
                    _equal(response_occupancy, _constant(0, response_width, FormalSignedness.UNSIGNED)),
                ),
            )
            properties.append(_property(
                "request_response", f"{connection.semantic_id}.reset_epoch",
                f"previous({module.reset}) -> {ids['outstanding']} == 0 && "
                f"{ids['request_occupancy']} == 0 && {ids['response_occupancy']} == 0",
                module, ownership=Ownership.IMPLEMENTATION,
                generated_from=generated, predicate=_implies(
                    _bit_observation("reset", ObservationCycle.PREVIOUS), reset_clear
                ),
                temporal=TemporalForm.NEXT_CYCLE, reset_condition=None,
                origin=connection.source_origin,
            ))


def _append_csr_properties(
    module: Module,
    properties: list[FormalProperty],
) -> None:
    for block_ordinal, block in enumerate(module.csr_blocks):
        effective_bindings = derived_state_bindings(
            block, module_identity=module.source_hash or module.name,
            block_ordinal=block_ordinal, clock_domain=module.clock,
            reset_domain=module.reset,
        )
        state_by_field = {
            item.csr_field_id: item for item in effective_bindings
        }
        for register_ordinal, register in enumerate(block.registers):
            for field_ordinal, field in enumerate(register.fields):
                prefix = f"{block.name}.{register.name}.{field.name}"
                state_binding = state_by_field.get(field.identity)
                if state_binding is None and field.identity is None:
                    state_binding = next(
                        (item for item in effective_bindings
                         if item.csr_field_id.register.declaration_ordinal == register_ordinal
                         and item.csr_field_id.declaration_ordinal == field_ordinal),
                        None,
                    )
                if state_binding is None:
                    continue
                state = state_binding.semantic_state_id
                write_hit = state_binding.write_hit_id
                write_value = state_binding.write_value_id
                state_ref = _observation(state, state_binding.canonical_type)
                hit_ref = _bit_observation(write_hit)
                value_ref = _observation(write_value, state_binding.canonical_type)
                width = state_binding.field_width
                signedness = _formal_signedness(state_binding.canonical_type)
                if module.reset is not None:
                    properties.append(_property(
                        "csr", prefix + ".reset",
                        f"previous({module.reset}) -> {prefix} == {field.reset}",
                        module, ownership=Ownership.IMPLEMENTATION,
                        generated_from=f"csr-field:{state_binding.csr_field_id.render()}:reset",
                        predicate=_implies(
                            _bit_observation("reset", ObservationCycle.PREVIOUS),
                            _equal(state_ref, _constant(field.reset, width, signedness)),
                        ),
                        temporal=TemporalForm.NEXT_CYCLE, reset_condition=None,
                        origin=field.source_origin,
                    ))
                previous_state = _previous(state_ref)
                previous_hit = _previous(hit_ref)
                previous_value = _previous(value_ref)
                if field.access.value == "rw":
                    update = PredicateMux(
                        previous_hit, previous_value, previous_state,
                        width, signedness,
                    )
                    suffix = "rw"
                if field.access.value == "w1c":
                    cleared = _binary(
                        FormalBinaryOperator.BIT_AND,
                        previous_state,
                        PredicateUnary(
                            FormalUnaryOperator.BITWISE_NOT,
                            previous_value,
                            width,
                            signedness,
                        ),
                        width,
                        signedness,
                    )
                    update = PredicateMux(
                        previous_hit, cleared, previous_state,
                        width, signedness,
                    )
                    suffix = "w1c"
                if field.access.value == "pulse":
                    update = PredicateMux(
                        previous_hit,
                        previous_value,
                        _constant(0, width, signedness),
                        width,
                        signedness,
                    )
                    suffix = "pulse"
                if field.access.value in {"rw", "w1c", "pulse"}:
                    predicate = _without_previous_reset(
                        module, _equal(state_ref, update)
                    )
                    properties.append(_property(
                        "csr", prefix + f".{suffix}",
                        f"{prefix} == previous({suffix} update)", module,
                        ownership=Ownership.IMPLEMENTATION,
                        generated_from=f"csr-field:{state_binding.csr_field_id.render()}:{suffix}",
                        predicate=predicate, temporal=TemporalForm.NEXT_CYCLE,
                        origin=field.source_origin,
                        non_executable_reason=(
                            "hardware-connected CSR priority requires its existing "
                            "implementation observation"
                            if field.binding is not None else None
                        ),
                    ))


def _append_rule_properties(
    module: Module,
    properties: list[FormalProperty],
) -> None:
    if module.resolved_transition is not None:
        from zlang.ir.state import actions_conflict, groups_may_conflict
        groups = module.resolved_transition.action_groups
        by_name = {group.rule_name: group for group in groups}

        def active_conflict_predicate(left, right):
            """Return the exact same-cycle conflict selected by activations.

            ``groups_may_conflict`` is the
            conservative semantic legality relation.  It is insufficient as
            an executable property once nested ``when`` makes an individual
            effect conditional: two outer groups may legally co-fire when the
            potentially conflicting effects are inactive.  Build the claim
            from the already-typed per-effect activation predicates instead;
            no new observation family is required.
            """

            pairs = tuple(
                (left_action, right_action)
                for left_action in left.actions
                for right_action in right.actions
                if actions_conflict(left_action, right_action)
            )
            if not pairs:
                return None, False, None
            # One unconditional conflicting pair makes the group conflict
            # unconditional and preserves the historical property spelling.
            if any(
                left_action.activation is None
                and right_action.activation is None
                for left_action, right_action in pairs
            ):
                return _bit(True), False, None
            terms: list[FormalPredicate] = []
            try:
                for left_action, right_action in pairs:
                    predicates = tuple(
                        _semantic_predicate(action.activation)
                        for action in (left_action, right_action)
                        if action.activation is not None
                    )
                    term = predicates[0]
                    for predicate in predicates[1:]:
                        term = _and(term, predicate)
                    terms.append(term)
            except FormalError as error:
                return None, True, str(error)
            result = terms[0]
            for term in terms[1:]:
                result = _or(result, term)
            return result, True, None

        conflicts = {
            tuple(sorted((left.rule_name, right.rule_name))):
                active_conflict_predicate(left, right)
            for index, left in enumerate(groups)
            for right in groups[index + 1:]
            if groups_may_conflict(left, right)
        }
        for (higher, lower), (
            active_conflict, conditional, lowering_error,
        ) in sorted(conflicts.items()):
            left = _bit_observation(rule_fire_observation_id(higher))
            right = _bit_observation(rule_fire_observation_id(lower))
            simultaneous = _and(left, right)
            predicate = (
                _not(_and(simultaneous, active_conflict))
                if conditional and active_conflict is not None
                else _not(simultaneous)
            )
            properties.append(_property(
                "rules", f"{higher}.exclusive.{lower}",
                (
                    f"!({higher}_fire && {lower}_fire && active_conflict)"
                    if conditional else
                    f"!({higher}_fire && {lower}_fire)"
                ), module,
                ownership=Ownership.SCHEDULER,
                generated_from=f"rules:{higher},{lower}",
                predicate=predicate,
                origin=by_name[higher].source_origin,
                non_executable_reason=(
                    "conditional rule-conflict activation is outside the "
                    f"structured formal predicate subset: {lowering_error}"
                    if lowering_error is not None else None
                ),
            ))
        for priority in module.resolved_transition.priorities:
            if tuple(sorted(priority)) not in conflicts:
                continue
            higher, lower = priority
            active_conflict, conditional, lowering_error = conflicts[
                tuple(sorted(priority))
            ]
            higher_fire = _bit_observation(rule_fire_observation_id(higher))
            antecedent = (
                _and(higher_fire, active_conflict)
                if conditional and active_conflict is not None
                else higher_fire
            )
            properties.append(_property(
                "rules", f"{higher}.priority.{lower}",
                (
                    f"({higher}_fire && active_conflict) -> !{lower}_fire"
                    if conditional else
                    f"{higher}_fire -> !{lower}_fire"
                ), module,
                ownership=Ownership.SCHEDULER,
                generated_from=f"priority:{higher}>{lower}",
                predicate=_implies(
                    antecedent,
                    _not(_bit_observation(rule_fire_observation_id(lower))),
                ),
                origin=by_name[higher].source_origin,
                non_executable_reason=(
                    "conditional rule-conflict activation is outside the "
                    f"structured formal predicate subset: {lowering_error}"
                    if lowering_error is not None else None
                ),
            ))


def _append_source_verification_properties(
    module: Module,
    properties: list[FormalProperty],
    covers: list[CoverProperty],
) -> None:
    if module.verification_scopes:
        legacy_contract_kinds = {
            item.name: item.kind.value for item in module.contracts
        }
        for scope in module.verification_scopes:
            requirement_predicates = tuple(
                _semantic_predicate(item.expression)
                for item in scope.requirements
            )
            conjunction: FormalPredicate | None = None
            for requirement_predicate in requirement_predicates:
                conjunction = (
                    requirement_predicate
                    if conjunction is None
                    else _and(conjunction, requirement_predicate)
                )
            if conjunction is not None:
                covers.append(CoverProperty(
                    id=f"{scope.semantic_id}.requirements_feasible",
                    clock=scope.clock,
                    reset_condition=scope.reset,
                    expression=conjunction.render(),
                    predicate=conjunction,
                    source_origin=scope.source_origin,
                    generated_from=f"verification-feasibility:{scope.name}",
                ))
            if scope.name == "$module":
                for requirement, predicate in zip(
                    scope.requirements, requirement_predicates, strict=True
                ):
                    properties.append(FormalProperty(
                        id=requirement.semantic_id,
                        kind=PropertyKind.ASSUMPTION,
                        clock=scope.clock,
                        reset_condition=scope.reset,
                        expression=predicate.render(),
                        temporal_form=TemporalForm.SAME_CYCLE,
                        ownership=Ownership.ENVIRONMENT,
                        source_origin=requirement.source_origin,
                        generated_from=(
                            f"contract:{requirement.name}"
                            if legacy_contract_kinds.get(requirement.name) == "assume"
                            else f"verification-requirement:{scope.name}:{requirement.name}"
                        ),
                        relevant_signals=predicate.observation_ids(),
                        predicate=predicate,
                    ))
            for goal in scope.goals:
                predicate = _semantic_predicate(goal.expression)
                if scope.name != "$module" and conjunction is not None:
                    executable = (
                        _and(conjunction, predicate)
                        if goal.kind is VerificationGoalKind.COVER
                        else _implies(conjunction, predicate)
                    )
                else:
                    executable = predicate
                if goal.kind is VerificationGoalKind.COVER:
                    covers.append(CoverProperty(
                        id=goal.semantic_id,
                        clock=scope.clock,
                        reset_condition=scope.reset,
                        expression=executable.render(),
                        predicate=executable,
                        source_origin=goal.source_origin,
                        generated_from=f"verification-cover:{scope.name}:{goal.name}",
                    ))
                else:
                    properties.append(FormalProperty(
                        id=goal.semantic_id,
                        kind=PropertyKind.ASSERTION,
                        clock=scope.clock,
                        reset_condition=scope.reset,
                        expression=executable.render(),
                        temporal_form=TemporalForm.SAME_CYCLE,
                        ownership=Ownership.IMPLEMENTATION,
                        source_origin=goal.source_origin,
                        generated_from=(
                            f"contract:{goal.name}"
                            if legacy_contract_kinds.get(goal.name) == "guarantee"
                            else (
                                f"verification-{goal.kind.value}:"
                                f"{scope.name}:{goal.name}"
                            )
                        ),
                        relevant_signals=executable.observation_ids(),
                        predicate=executable,
                    ))
    else:
        # Compatibility for hand-constructed semantic modules predating the
        # overlay normalization performed by semantic analysis.
        for contract in module.contracts:
            predicate = _semantic_predicate(contract.expression)
            properties.append(FormalProperty(
                id=_stable_id("contract", module.name, contract.name),
                kind=(PropertyKind.ASSUMPTION if contract.kind.value == "assume" else PropertyKind.ASSERTION),
                clock=contract.clock, reset_condition=contract.reset,
                expression=predicate.render(),
                temporal_form=TemporalForm.SAME_CYCLE, ownership=(Ownership.ENVIRONMENT if contract.kind.value == "assume" else Ownership.IMPLEMENTATION),
                source_origin=contract.expression.origin,
                generated_from=f"contract:{contract.name}",
                relevant_signals=predicate.observation_ids(), predicate=predicate,
            ))


def generate_properties(module: Module) -> FormalDesign:
    """Generate the frozen safety verification safety families from typed semantic IR."""
    # Multi-domain automatic state-property families remain outside the
    # frozen safety verification subset, but source goals in one exact supported domain may
    # still observe that state.  Preserve the complete module for semantic
    # bindings while filtering only the automatic-property generation view.
    binding_module = module
    # Purely combinational modules have no sampling domain. They still expose
    # stable public bindings, but automatic sequential/protocol properties are
    # omitted until a clocked selected IR exists; source contracts retain their
    # own explicit clock and continue to lower normally.
    if module.clock is None and len(module.clock_domains) != 1:
        module = replace(
            module,
            registers=(), fifos=(), csr_blocks=(), rules=(), rule_priorities=(),
            resolved_transition=None,
            ports=tuple(
                port for port in module.ports
                if port.protocol is InterfaceProtocol.WIRE and not port.registered
            ),
        )
    properties: list[FormalProperty] = []
    covers: list[CoverProperty] = []
    _append_register_properties(module, properties)
    _append_fifo_properties(module, properties)
    _append_protocol_port_properties(module, properties)
    _append_request_response_connection_properties(module, properties)
    _append_csr_properties(module, properties)
    _append_rule_properties(module, properties)
    _append_source_verification_properties(module, properties, covers)
    return FormalDesign(
        module.name,
        tuple(properties),
        signal_bindings(binding_module, include_rule_fire=True),
        covers=tuple(covers),
        clock_domains=tuple(binding_module.clock_domains),
    )
