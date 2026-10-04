# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Request/response simulation lowering."""

from __future__ import annotations

from dataclasses import replace

from zlang.ir import expressions as expr
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.ir import verification as ir_verification
from zlang.simulation_primitives import bit_binary as _bit_binary
from zlang.simulation_primitives import bit_not as _bit_not


import zlang.simulation_protocol_shared as protocol_shared


class _RequestResponseExpressionLowerer(
    protocol_shared._MappedProtocolExpressionLowerer[
        tuple[str, ir_interfaces.RequestResponseChannel, ir_interfaces.ReadyValidSignal]
    ],
):

    def rewrite_special(
        self,
        value: expr.Expression,
    ) -> expr.Expression | None:
        if not isinstance(value, expr.RequestResponseRef):
            return None
        if value.signal is ir_interfaces.ReadyValidSignal.TRANSFER:
            try:
                valid = self._values[
                    (value.interface, value.channel, ir_interfaces.ReadyValidSignal.VALID)
                ]
                ready = self._values[
                    (value.interface, value.channel, ir_interfaces.ReadyValidSignal.READY)
                ]
            except KeyError as error:
                raise protocol_shared.ProtocolSimulationLoweringError(
                    f"request/response transfer names incomplete channel "
                    f"'{value.interface}.{value.channel.value}'"
                ) from error
            return _bit_binary(expr.BinaryOperator.BIT_AND, valid, ready)
        try:
            return self._values[(value.interface, value.channel, value.signal)]
        except KeyError as error:
            raise protocol_shared.ProtocolSimulationLoweringError(
                f"request/response reference names unknown field "
                f"'{value.interface}.{value.channel.value}.{value.signal.value}'"
            ) from error


class _OutstandingIdLedger:
    """Own exact out-of-order request IDs and duplicate/missing checks."""

    def __init__(
        self,
        interface: ir_module.RequestResponseInterface,
        domain: str | None,
    ) -> None:
        if interface.match_by is None or interface.id_type is None:
            raise protocol_shared.ProtocolSimulationLoweringError(
                "out-of-order request/response ledger requires an exact ID field"
            )
        self.interface = interface
        self.id_registers = tuple(
            ir_module.Register(
                protocol_shared.request_response_state_name(
                    interface.name,
                    f"id:{index}",
                ),
                interface.id_type,
                expr.Constant(0, interface.id_type),
                domain,
            )
            for index in range(interface.max_outstanding)
        )
        self.valid_registers = tuple(
            ir_module.Register(
                protocol_shared.request_response_state_name(
                    interface.name,
                    f"id_valid:{index}",
                ),
                ir_types.BitType(),
                expr.Constant(0, ir_types.BitType()),
                domain,
            )
            for index in range(interface.max_outstanding)
        )

    def lower(
        self,
        module: ir_module.Module,
        request_payload: expr.Expression,
        response_payload: expr.Expression,
        request_transfer: expr.Expression,
        response_transfer: expr.Expression,
    ) -> tuple[
        tuple[ir_module.NextAssignment, ...],
        ir_verification.VerificationScope,
    ]:
        interface = self.interface
        assert interface.match_by is not None
        assert interface.id_type is not None
        request_id = expr.FieldAccess(
            request_payload,
            interface.match_by,
            interface.id_type,
        )
        response_id = expr.FieldAccess(
            response_payload,
            interface.match_by,
            interface.id_type,
        )
        request_present: expr.Expression = expr.Constant(0, ir_types.BitType())
        response_present: expr.Expression = expr.Constant(0, ir_types.BitType())
        valid_after_remove: list[expr.Expression] = []
        for id_register, valid_register in zip(
            self.id_registers,
            self.valid_registers,
            strict=True,
        ):
            stored_id = expr.RegisterRef(id_register.name, id_register.type)
            stored_valid = expr.RegisterRef(valid_register.name, valid_register.type)
            request_match = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                stored_valid,
                expr.Binary(
                    expr.BinaryOperator.EQUAL,
                    stored_id,
                    request_id,
                    interface.id_type,
                    ir_types.BitType(),
                ),
            )
            response_match = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                stored_valid,
                expr.Binary(
                    expr.BinaryOperator.EQUAL,
                    stored_id,
                    response_id,
                    interface.id_type,
                    ir_types.BitType(),
                ),
            )
            request_present = _bit_binary(
                expr.BinaryOperator.BIT_OR,
                request_present,
                request_match,
            )
            response_present = _bit_binary(
                expr.BinaryOperator.BIT_OR,
                response_present,
                response_match,
            )
            remove_here = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                response_transfer,
                response_match,
            )
            valid_after_remove.append(
                _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    stored_valid,
                    _bit_not(remove_here),
                )
            )

        duplicate = _bit_binary(
            expr.BinaryOperator.BIT_AND,
            request_transfer,
            request_present,
        )
        missing = _bit_binary(
            expr.BinaryOperator.BIT_AND,
            response_transfer,
            _bit_not(response_present),
        )
        runtime_scope = protocol_shared._runtime_protocol_scope(
            module=module,
            port=ir_module.Port(
                ir_module.PortDirection.OUTPUT,
                interface.name,
                interface.request_type,
                domain=module.clock,
            ),
            conditions=(
                (
                    f"request/response interface '{interface.name}' "
                    "issued duplicate outstanding ID",
                    _bit_not(duplicate),
                ),
                (
                    f"request/response interface '{interface.name}' "
                    "received response for non-outstanding ID",
                    _bit_not(missing),
                ),
            ),
        )
        next_assignments: list[ir_module.NextAssignment] = []
        inserted: expr.Expression = expr.Constant(0, ir_types.BitType())
        for index, (id_register, valid_register) in enumerate(
            zip(self.id_registers, self.valid_registers, strict=True)
        ):
            insert_here = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                request_transfer,
                _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    _bit_not(inserted),
                    _bit_not(valid_after_remove[index]),
                ),
            )
            next_assignments.extend(
                (
                    ir_module.NextAssignment(
                        valid_register,
                        _bit_binary(
                            expr.BinaryOperator.BIT_OR,
                            valid_after_remove[index],
                            insert_here,
                        ),
                    ),
                    ir_module.NextAssignment(
                        id_register,
                        expr.Mux(
                            insert_here,
                            request_id,
                            expr.RegisterRef(id_register.name, id_register.type),
                            id_register.type,
                        ),
                    ),
                )
            )
            inserted = _bit_binary(
                expr.BinaryOperator.BIT_OR,
                inserted,
                insert_here,
            )
        return tuple(next_assignments), runtime_scope


def lower_request_response_module(module: ir_module.Module) -> ir_module.Module:
    """Erase standalone request/response ledgers into primitive state."""

    interfaces = module.request_responses
    if not interfaces:
        return module
    if any(
        interface.ordering is ir_interfaces.RequestResponseOrdering.OUT_OF_ORDER
        and (
            interface.role is not ir_interfaces.RequestResponseRole.REQUESTER
            or interface.match_by is None
            or interface.id_type is None
        )
        for interface in interfaces
    ):
        raise protocol_shared.ProtocolSimulationLoweringError(
            "out-of-order request/response lowering requires a requester with "
            "an exact match field"
        )
    if any(port.protocol is not ir_interfaces.InterfaceProtocol.WIRE for port in module.ports):
        raise protocol_shared.ProtocolSimulationLoweringError(
            "request/response lowering cannot mix unrelated protocol ports"
        )
    if (
        module.connections
        or module.hierarchical_connections
        or module.aggregate_protocol_connections
        or module.request_response_connections
    ):
        raise protocol_shared.ProtocolSimulationLoweringError(
            "standalone request/response lowering cannot consume hierarchy links"
        )

    producers: dict[
        tuple[str, ir_interfaces.RequestResponseChannel, ir_interfaces.ReadyValidSignal],
        expr.Expression,
    ] = {}
    retained_assignments: list[ir_module.Assignment] = []
    for assignment in module.assignments:
        if not isinstance(assignment.target, ir_module.RequestResponseInterface):
            retained_assignments.append(assignment)
            continue
        if assignment.channel is None or not isinstance(
            assignment.signal,
            ir_interfaces.ReadyValidSignal,
        ):
            raise protocol_shared.ProtocolSimulationLoweringError(
                f"request/response assignment '{assignment.target.name}' has "
                "no exact channel signal"
            )
        if assignment.signal is ir_interfaces.ReadyValidSignal.TRANSFER:
            raise protocol_shared.ProtocolSimulationLoweringError(
                "derived request/response transfer cannot be assigned"
            )
        key = (
            assignment.target.name,
            assignment.channel,
            assignment.signal,
        )
        if protocol_shared.request_response_scalar_field(
            assignment.target,
            assignment.channel,
            assignment.signal,
        ).external:
            raise protocol_shared.ProtocolSimulationLoweringError(
                f"request/response field '{assignment.target.name}."
                f"{assignment.channel.value}.{assignment.signal.value}' is "
                "environment-owned"
            )
        if key in producers:
            raise protocol_shared.ProtocolSimulationLoweringError(
                f"request/response field '{assignment.target.name}."
                f"{assignment.channel.value}.{assignment.signal.value}' has "
                "multiple drivers"
            )
        producers[key] = assignment.expression

    values: dict[
        tuple[str, ir_interfaces.RequestResponseChannel, ir_interfaces.ReadyValidSignal],
        expr.Expression,
    ] = {}
    scalar = protocol_shared.ScalarPortInventory(module.ports)
    count_registers: dict[str, ir_module.Register] = {}
    id_ledgers: dict[str, _OutstandingIdLedger] = {}
    for interface in interfaces:
        if interface.max_outstanding < 1:
            raise protocol_shared.ProtocolSimulationLoweringError(
                f"request/response interface '{interface.name}' has no capacity"
            )
        for field in protocol_shared.request_response_scalar_fields(interface):
            name = protocol_shared.request_response_field_name(
                interface.name, field.channel, field.signal
            )
            scalar.add_protocol_field(
                external=field.external,
                name=name,
                type_=field.type,
                domain=module.clock,
            )
            if field.external:
                values[(interface.name, field.channel, field.signal)] = expr.InputRef(
                    name,
                    field.type,
                )
        count_type = ir_types.UIntType(max(1, interface.max_outstanding.bit_length()))
        count_registers[interface.name] = ir_module.Register(
            protocol_shared.request_response_state_name(interface.name, "outstanding"),
            count_type,
            expr.Constant(0, count_type),
            module.clock,
        )
        if interface.ordering is ir_interfaces.RequestResponseOrdering.OUT_OF_ORDER:
            id_ledgers[interface.name] = _OutstandingIdLedger(
                interface,
                module.clock,
            )
        count_name = protocol_shared.request_response_field_name(
            interface.name,
            "ledger",
            "outstanding",
        )
        scalar.add(
            ir_module.PortDirection.OUTPUT,
            count_name, count_type, module.clock,
        )

    lowerer = _RequestResponseExpressionLowerer(values)
    next_assignments = list(lowerer.value(module.next_assignments))
    generated_assignments: list[ir_module.Assignment] = []
    runtime_scopes: list[ir_verification.VerificationScope] = []
    for interface in interfaces:
        count_register = count_registers[interface.name]
        count = expr.RegisterRef(count_register.name, count_register.type)
        has_capacity = expr.Binary(
            expr.BinaryOperator.LESS,
            count,
            expr.Constant(interface.max_outstanding, count_register.type),
            count_register.type,
            ir_types.BitType(),
        )
        has_outstanding = expr.Binary(
            expr.BinaryOperator.NOT_EQUAL,
            count,
            expr.Constant(0, count_register.type),
            count_register.type,
            ir_types.BitType(),
        )

        def raw(
            channel: ir_interfaces.RequestResponseChannel,
            signal: ir_interfaces.ReadyValidSignal,
        ) -> expr.Expression:
            key = (interface.name, channel, signal)
            try:
                source = producers[key]
            except KeyError as error:
                raise protocol_shared.ProtocolSimulationLoweringError(
                    f"request/response field '{interface.name}."
                    f"{channel.value}.{signal.value}' has no exact driver"
                ) from error
            return lowerer.expression(source)

        reset_deasserted = _bit_not(
            expr.InputRef(f"$reset:{module.reset}", ir_types.BitType())
        )
        if interface.role is ir_interfaces.RequestResponseRole.REQUESTER:
            request_payload = raw(
                ir_interfaces.RequestResponseChannel.REQUEST,
                ir_interfaces.ReadyValidSignal.PAYLOAD,
            )
            values[(interface.name, ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.ReadyValidSignal.PAYLOAD)] = request_payload
            request_valid = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                reset_deasserted,
                _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    has_capacity,
                    raw(ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.ReadyValidSignal.VALID),
                ),
            )
            values[(interface.name, ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.ReadyValidSignal.VALID)] = request_valid
            request_ready = values[(interface.name, ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.ReadyValidSignal.READY)]
            request_transfer = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                request_valid,
                request_ready,
            )
            response_available = (
                has_outstanding
                if interface.ordering is ir_interfaces.RequestResponseOrdering.OUT_OF_ORDER
                else _bit_binary(
                    expr.BinaryOperator.BIT_OR,
                    has_outstanding,
                    request_transfer,
                )
            )
            response_ready = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                reset_deasserted,
                _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    response_available,
                    raw(ir_interfaces.RequestResponseChannel.RESPONSE, ir_interfaces.ReadyValidSignal.READY),
                ),
            )
            values[(interface.name, ir_interfaces.RequestResponseChannel.RESPONSE, ir_interfaces.ReadyValidSignal.READY)] = response_ready
            response_valid = values[(interface.name, ir_interfaces.RequestResponseChannel.RESPONSE, ir_interfaces.ReadyValidSignal.VALID)]
            response_transfer = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                response_valid,
                response_ready,
            )
            if interface.ordering is ir_interfaces.RequestResponseOrdering.OUT_OF_ORDER:
                response_payload = values[
                    (
                        interface.name,
                        ir_interfaces.RequestResponseChannel.RESPONSE,
                        ir_interfaces.ReadyValidSignal.PAYLOAD,
                    )
                ]
                ledger_next, ledger_scope = id_ledgers[interface.name].lower(
                    module,
                    request_payload,
                    response_payload,
                    request_transfer,
                    response_transfer,
                )
                next_assignments.extend(ledger_next)
                runtime_scopes.append(ledger_scope)
        else:
            request_ready = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                reset_deasserted,
                _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    has_capacity,
                    raw(ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.ReadyValidSignal.READY),
                ),
            )
            values[(interface.name, ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.ReadyValidSignal.READY)] = request_ready
            request_valid = values[(interface.name, ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.ReadyValidSignal.VALID)]
            request_transfer = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                request_valid,
                request_ready,
            )
            values[(interface.name, ir_interfaces.RequestResponseChannel.RESPONSE, ir_interfaces.ReadyValidSignal.PAYLOAD)] = raw(
                ir_interfaces.RequestResponseChannel.RESPONSE,
                ir_interfaces.ReadyValidSignal.PAYLOAD,
            )
            response_valid = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                reset_deasserted,
                _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    _bit_binary(
                        expr.BinaryOperator.BIT_OR,
                        has_outstanding,
                        request_transfer,
                    ),
                    raw(ir_interfaces.RequestResponseChannel.RESPONSE, ir_interfaces.ReadyValidSignal.VALID),
                ),
            )
            values[(interface.name, ir_interfaces.RequestResponseChannel.RESPONSE, ir_interfaces.ReadyValidSignal.VALID)] = response_valid
            response_ready = values[(interface.name, ir_interfaces.RequestResponseChannel.RESPONSE, ir_interfaces.ReadyValidSignal.READY)]
            response_transfer = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                response_valid,
                response_ready,
            )

        widened_request = expr.Extend(request_transfer, count_register.type)
        widened_response = expr.Extend(response_transfer, count_register.type)
        next_count = expr.Binary(
            expr.BinaryOperator.SUBTRACT,
            expr.Add(count, widened_request, count_register.type),
            widened_response,
            count_register.type,
            count_register.type,
        )
        next_assignments.append(ir_module.NextAssignment(count_register, next_count))
        for field in protocol_shared.request_response_scalar_fields(interface):
            name = protocol_shared.request_response_field_name(
                interface.name, field.channel, field.signal
            )
            port = scalar.by_name[name]
            if port.direction is ir_module.PortDirection.OUTPUT:
                generated_assignments.append(
                    ir_module.Assignment(
                        port,
                        values[(interface.name, field.channel, field.signal)],
                    )
                )
        count_name = protocol_shared.request_response_field_name(
            interface.name,
            "ledger",
            "outstanding",
        )
        generated_assignments.append(
            scalar.assignment(count_name, count)
        )

    lowerer = _RequestResponseExpressionLowerer(values)
    assignments = [
        replace(assignment, expression=lowerer.expression(assignment.expression))
        for assignment in retained_assignments
    ]
    assignments.extend(generated_assignments)
    return protocol_shared._finalize_protocol_module(
        module,
        lowerer,
        ports=tuple(scalar.ports),
        assignments=tuple(assignments),
        rewritten={
            "registers": (
                *lowerer.value(module.registers),
                *count_registers.values(),
                *(
                    register
                    for ledger in id_ledgers.values()
                    for register in ledger.id_registers
                ),
                *(
                    register
                    for ledger in id_ledgers.values()
                    for register in ledger.valid_registers
                ),
            ),
            "next_assignments": tuple(next_assignments),
            "verification_scopes": (
                *lowerer.value(module.verification_scopes),
                *runtime_scopes,
            ),
        },
        removed={"request_responses": ()},
    )
