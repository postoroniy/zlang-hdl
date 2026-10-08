"""Compiler-owned lowering of typed CSR banks to primitive simulation state."""

from __future__ import annotations

from dataclasses import replace

from zlang.ir import csr as ir_csr
from zlang.ir import expressions as expr
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import (
    Assignment,
    Module,
    NextAssignment,
    Port,
    PortDirection,
    Register,
)
from zlang.ir.types import BitType, UIntType
from zlang.simulation_primitives import bit_binary as _bit_binary


class CsrSimulationLoweringError(ValueError):
    """A typed CSR bank cannot be represented by the primitive machine."""


def _binary(
    operator: expr.BinaryOperator,
    left: expr.Expression,
    right: expr.Expression,
    type_,
) -> expr.Binary:
    return expr.Binary(operator, left, right, type_, type_)


def _equal(
    left: expr.Expression,
    right: expr.Expression,
    operand_type,
) -> expr.Binary:
    return expr.Binary(
        expr.BinaryOperator.EQUAL,
        left,
        right,
        operand_type,
        BitType(),
    )


def _or_all(values: list[expr.Expression]) -> expr.Expression:
    result: expr.Expression = expr.Constant(0, BitType())
    for value in values:
        result = _bit_binary(expr.BinaryOperator.BIT_OR, result, value)
    return result


def _field_state_name(
    block_index: int,
    register_index: int,
    field_index: int,
    field: ir_csr.CsrField,
) -> str:
    if field.identity is not None:
        identity = field.identity.render()
    else:
        identity = f"{block_index}:{register_index}:{field_index}"
    return f"$zlang_csr_state:{identity}"


def csr_field_state_register_name(
    block_index: int,
    register_index: int,
    field_index: int,
    field: ir_csr.CsrField,
) -> str:
    """Return the compiler-owned primitive register for one stored CSR field."""

    return _field_state_name(block_index, register_index, field_index, field)


def _stored(access: ir_csr.CsrAccess) -> bool:
    return ir_csr.access_owns_state(access)


class _CsrOutputPublisher:
    """Own scalar CSR observation/event/readback publication."""

    def __init__(
        self,
        module: Module,
        access: ir_csr.CsrAccessInterface,
        ports: dict[str, Port],
        *,
        address: expr.Expression,
        write: expr.Expression,
        write_data: expr.Expression,
        read: expr.Expression,
        address_type: UIntType,
        data_type: UIntType,
        state_by_field: dict[ir_csr.CsrFieldIdentity, Register],
        fields_by_identity: dict[
            ir_csr.CsrFieldIdentity,
            tuple[ir_csr.CsrBlock, ir_csr.CsrRegister, ir_csr.CsrField],
        ],
        address_matches: list[expr.Expression],
        read_values: list[tuple[expr.Expression, expr.Expression]],
        assignments: list[Assignment],
    ) -> None:
        self._module = module
        self._access = access
        self._ports = ports
        self._address = address
        self._write = write
        self._write_data = write_data
        self._read = read
        self._address_type = address_type
        self._data_type = data_type
        self._state_by_field = state_by_field
        self._fields_by_identity = fields_by_identity
        self._address_matches = address_matches
        self._read_values = read_values
        self._assignments = assignments

    def _assign(self, name: str, value: expr.Expression, label: str) -> None:
        target = self._ports.get(name)
        if target is None or target.type != value.type:
            raise CsrSimulationLoweringError(
                f"CSR {label} port '{name}' is missing or mistyped"
            )
        self._assignments.append(Assignment(target, value))

    def publish_state_bindings(self) -> None:
        for binding in (
            binding
            for block in self._module.csr_blocks
            for binding in block.state_bindings
        ):
            try:
                self._fields_by_identity[binding.csr_field_id]
                state_register = self._state_by_field[binding.csr_field_id]
            except KeyError as error:
                raise CsrSimulationLoweringError(
                    "CSR state binding does not name one stored field"
                ) from error
            self._assign(
                ir_csr.csr_state_port_name(binding),
                expr.RegisterRef(state_register.name, state_register.type),
                "internal ABI",
            )

    def publish_blocks(self) -> None:
        for block in self._module.csr_blocks:
            fields = {
                field.identity: (register, field)
                for register in block.registers
                for field in register.fields
            }
            self._publish_observations(block, fields)
            self._publish_events(block)
            self._publish_split_views(block)

    def _publish_observations(
        self,
        block: ir_csr.CsrBlock,
        fields: dict[
            ir_csr.CsrFieldIdentity | None,
            tuple[ir_csr.CsrRegister, ir_csr.CsrField],
        ],
    ) -> None:
        for observation in block.access_observations:
            register, field = fields[observation.csr_field_id]
            address_hit = _equal(
                self._address,
                expr.Constant(
                    block.base_address + register.offset,
                    self._address_type,
                ),
                self._address_type,
            )
            incoming = expr.Slice(
                self._write_data,
                field.msb,
                field.lsb,
                field.type,
                origin=field.source_origin,
            )
            if (
                field.access is ir_csr.CsrAccess.READ_ONLY
                and field.binding is not None
                and field.binding.kind is ir_csr.CsrBindingKind.STATUS
            ):
                observed_value: expr.Expression = ir_csr.csr_hardware_source(
                    field.binding,
                    field.type,
                    origin=field.source_origin,
                )
            elif field.access is ir_csr.CsrAccess.READ_ONLY:
                observed_value = expr.Constant(
                    field.reset,
                    field.type,
                    origin=field.source_origin,
                )
            else:
                state_register = self._state_by_field.get(field.identity)
                if state_register is None:
                    raise CsrSimulationLoweringError(
                        f"CSR observed field '{field.name}' has no value owner"
                    )
                observed_value = expr.RegisterRef(
                    state_register.name,
                    state_register.type,
                    origin=field.source_origin,
                )
            for name, value in (
                (
                    ir_csr.csr_read_hit_port_name(observation),
                    _bit_binary(
                        expr.BinaryOperator.BIT_AND,
                        self._read,
                        address_hit,
                    ),
                ),
                (
                    ir_csr.csr_observation_write_hit_port_name(observation),
                    _bit_binary(
                        expr.BinaryOperator.BIT_AND,
                        self._write,
                        address_hit,
                    ),
                ),
                (
                    ir_csr.csr_observation_write_value_port_name(observation),
                    incoming,
                ),
                (
                    ir_csr.csr_observation_value_port_name(observation),
                    observed_value,
                ),
            ):
                self._assign(name, value, "observation")

    def _publish_events(self, block: ir_csr.CsrBlock) -> None:
        for register in block.registers:
            address_hit = _equal(
                self._address,
                expr.Constant(
                    block.base_address + register.offset,
                    self._address_type,
                ),
                self._address_type,
            )
            for event in register.events:
                qualifier = (
                    self._write
                    if event.kind is ir_csr.CsrEventKind.WRITE
                    else self._read
                )
                hit = _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    qualifier,
                    address_hit,
                )
                if event.kind is ir_csr.CsrEventKind.WRITE:
                    incoming = expr.Slice(
                        self._write_data,
                        event.msb,
                        event.lsb,
                        event.canonical_type,
                        origin=event.source_origin,
                    )
                    value: expr.Expression = expr.Mux(
                        hit,
                        incoming,
                        expr.Constant(0, event.canonical_type),
                        event.canonical_type,
                    )
                else:
                    value = hit
                for name in (event.signal, ir_csr.csr_event_port_name(event)):
                    self._assign(name, value, "event")

    def _publish_split_views(self, block: ir_csr.CsrBlock) -> None:
        for view in block.split_views:
            low_state = self._state_by_field.get(view.low_field_id)
            high_state = self._state_by_field.get(view.high_field_id)
            if low_state is None or high_state is None:
                raise CsrSimulationLoweringError(
                    f"CSR split view '{view.name}' does not name stored halves"
                )
            value = expr.Concat(
                (
                    expr.RegisterRef(high_state.name, high_state.type),
                    expr.RegisterRef(low_state.name, low_state.type),
                ),
                view.canonical_type,
                origin=view.source_origin,
            )
            self._assign(
                ir_csr.csr_split_port_name(block, view),
                value,
                "split-view",
            )

    def publish_access_response(self) -> None:
        read_data: expr.Expression = expr.Constant(0, self._data_type)
        for hit, value in reversed(self._read_values):
            read_data = expr.Mux(hit, value, read_data, self._data_type)
        transaction = _bit_binary(
            expr.BinaryOperator.BIT_OR,
            self._read,
            self._write,
        )
        ready = _bit_binary(
            expr.BinaryOperator.BIT_AND,
            transaction,
            _or_all(self._address_matches),
        )
        self._assignments.extend(
            (
                Assignment(self._ports[self._access.read_data_port], read_data),
                Assignment(self._ports[self._access.ready_port], ready),
            )
        )


def lower_csr_module(module: Module) -> Module:
    """Erase CSR decode/policies into ordinary typed registers and equations."""

    if not module.csr_blocks:
        return module
    access = module.csr_access
    if access is None:
        raise CsrSimulationLoweringError("typed CSR access interface is missing")
    if any(port.protocol is not InterfaceProtocol.WIRE for port in module.ports):
        raise CsrSimulationLoweringError(
            "CSR lowering requires ordinary scalar wire ports"
        )

    ports = {port.name: port for port in module.ports}
    for name, type_ in (*access.input_types, *access.output_types):
        port = ports.get(name)
        expected_direction = (
            PortDirection.INPUT
            if (name, type_) in access.input_types
            else PortDirection.OUTPUT
        )
        if (
            port is None
            or port.direction is not expected_direction
            or port.type != type_
        ):
            raise CsrSimulationLoweringError(
                f"CSR access port '{name}' has an inconsistent physical type"
            )

    address_type = UIntType(access.address_width)
    data_type = UIntType(access.data_width)
    address = expr.InputRef(access.address_port, address_type)
    write = expr.InputRef(access.write_port, BitType())
    write_data = expr.InputRef(access.write_data_port, data_type)
    read = expr.InputRef(access.read_port, BitType())
    generated_registers: list[Register] = []
    generated_next: list[NextAssignment] = []
    generated_assignments: list[Assignment] = []
    state_by_field: dict[ir_csr.CsrFieldIdentity, Register] = {}
    fields_by_identity: dict[
        ir_csr.CsrFieldIdentity,
        tuple[ir_csr.CsrBlock, ir_csr.CsrRegister, ir_csr.CsrField],
    ] = {}
    address_matches: list[expr.Expression] = []
    read_values: list[tuple[expr.Expression, expr.Expression]] = []

    for block_index, block in enumerate(module.csr_blocks):
        try:
            ir_csr.validate_state_bindings(block)
        except ir_csr.CsrStateBindingError as error:
            raise CsrSimulationLoweringError(str(error)) from error
        domain = block.domain or module.clock
        reset = block.reset or module.reset
        if domain is None or reset is None:
            raise CsrSimulationLoweringError(
                f"CSR block '{block.name}' has no exact clock/reset domain"
            )
        for register_index, csr_register in enumerate(block.registers):
            absolute_address = block.base_address + csr_register.offset
            if not 0 <= absolute_address < (1 << access.address_width):
                raise CsrSimulationLoweringError(
                    f"CSR register '{block.name}.{csr_register.name}' address "
                    "does not fit the access interface"
                )
            address_match = _equal(
                address,
                expr.Constant(absolute_address, address_type),
                address_type,
            )
            address_matches.append(address_match)
            write_hit = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                write,
                address_match,
            )
            register_value: expr.Expression = expr.Constant(0, data_type)
            for field_index, field in enumerate(csr_register.fields):
                if field.identity is None:
                    raise CsrSimulationLoweringError(
                        f"CSR field '{block.name}.{csr_register.name}."
                        f"{field.name}' has no structured identity"
                    )
                if field.width != field.type.width:
                    raise CsrSimulationLoweringError(
                        f"CSR field '{field.name}' width disagrees with its type"
                    )
                fields_by_identity[field.identity] = (
                    block,
                    csr_register,
                    field,
                )
                incoming = expr.Slice(
                    write_data,
                    field.msb,
                    field.lsb,
                    field.type,
                    origin=field.source_origin,
                )
                if _stored(field.access):
                    state_register = Register(
                        _field_state_name(
                            block_index,
                            register_index,
                            field_index,
                            field,
                        ),
                        field.type,
                        expr.Constant(
                            field.reset,
                            field.type,
                            origin=field.source_origin,
                        ),
                        domain,
                    )
                    generated_registers.append(state_register)
                    state_by_field[field.identity] = state_register
                    current: expr.Expression = expr.RegisterRef(
                        state_register.name,
                        state_register.type,
                        origin=field.source_origin,
                    )
                    zero = expr.Constant(0, field.type)
                    if field.access is ir_csr.CsrAccess.PULSE:
                        next_value = expr.Mux(
                            write_hit,
                            incoming,
                            zero,
                            field.type,
                        )
                    elif field.access is ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR:
                        clear = expr.Mux(
                            write_hit,
                            incoming,
                            zero,
                            field.type,
                        )
                        inverted_clear = _binary(
                            expr.BinaryOperator.BIT_XOR,
                            clear,
                            expr.Constant((1 << field.width) - 1, field.type),
                            field.type,
                        )
                        cleared = _binary(
                            expr.BinaryOperator.BIT_AND,
                            current,
                            inverted_clear,
                            field.type,
                        )
                        binding = field.binding
                        if (
                            binding is not None
                            and binding.kind is ir_csr.CsrBindingKind.STICKY
                        ):
                            hardware = ir_csr.csr_hardware_source(
                                binding,
                                field.type,
                                origin=field.source_origin,
                            )
                            if binding.priority is ir_csr.CsrPriority.SOFTWARE:
                                set_value = _binary(
                                    expr.BinaryOperator.BIT_OR,
                                    current,
                                    hardware,
                                    field.type,
                                )
                                next_value = _binary(
                                    expr.BinaryOperator.BIT_AND,
                                    set_value,
                                    inverted_clear,
                                    field.type,
                                )
                            else:
                                next_value = _binary(
                                    expr.BinaryOperator.BIT_OR,
                                    cleared,
                                    hardware,
                                    field.type,
                                )
                        else:
                            next_value = cleared
                    else:
                        next_value = expr.Mux(
                            write_hit,
                            incoming,
                            current,
                            field.type,
                        )
                    generated_next.append(
                        NextAssignment(state_register, next_value)
                    )
                    if (
                        field.binding is not None
                        and field.binding.kind is ir_csr.CsrBindingKind.COMMAND
                    ):
                        target = ports.get(field.binding.signal)
                        if (
                            target is None
                            or target.direction is not PortDirection.OUTPUT
                            or target.type != field.type
                        ):
                            raise CsrSimulationLoweringError(
                                f"CSR command binding '{field.binding.signal}' "
                                "has no exact output port"
                            )
                        generated_assignments.append(Assignment(target, current))

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
                    field_value: expr.Expression = ir_csr.csr_hardware_source(
                        field.binding,
                        field.type,
                        origin=field.source_origin,
                    )
                elif field.access is ir_csr.CsrAccess.READ_ONLY:
                    field_value = expr.Constant(
                        field.reset,
                        field.type,
                        origin=field.source_origin,
                    )
                else:
                    field_value = expr.RegisterRef(
                        state_by_field[field.identity].name,
                        field.type,
                        origin=field.source_origin,
                    )
                widened = expr.Extend(field_value, data_type)
                shifted = _binary(
                    expr.BinaryOperator.SHIFT_LEFT,
                    widened,
                    expr.Constant(field.lsb, data_type),
                    data_type,
                )
                register_value = _binary(
                    expr.BinaryOperator.BIT_OR,
                    register_value,
                    shifted,
                    data_type,
                )
            read_hit = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                read,
                address_match,
            )
            read_values.append((read_hit, register_value))

    publisher = _CsrOutputPublisher(
        module,
        access,
        ports,
        address=address,
        write=write,
        write_data=write_data,
        read=read,
        address_type=address_type,
        data_type=data_type,
        state_by_field=state_by_field,
        fields_by_identity=fields_by_identity,
        address_matches=address_matches,
        read_values=read_values,
        assignments=generated_assignments,
    )
    publisher.publish_state_bindings()
    publisher.publish_blocks()
    publisher.publish_access_response()

    return replace(
        module,
        assignments=(*module.assignments, *generated_assignments),
        registers=(*module.registers, *generated_registers),
        next_assignments=(*module.next_assignments, *generated_next),
        csr_blocks=(),
        csr_access=None,
        module_signature=None,
        semantic_expression_arena_statistics=None,
        semantic_expression_provenance=None,
    )


__all__ = ["CsrSimulationLoweringError", "lower_csr_module"]
