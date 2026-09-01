"""Typed control/status-register semantic model."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from zlang.ir.types import BitType, HardwareType, UIntType
from zlang.source import SourceOrigin


class CsrAccess(str, Enum):
    READ_WRITE = "rw"
    READ_ONLY = "ro"
    WRITE_ONLY = "wo"
    WRITE_ONE_TO_CLEAR = "w1c"
    PULSE = "pulse"
    RESERVED = "reserved"


class CsrBindingKind(str, Enum):
    STATUS = "status"
    STICKY = "sticky"
    COMMAND = "command"


class CsrPriority(str, Enum):
    HARDWARE = "hardware"
    SOFTWARE = "software"


class CsrStateBindingError(ValueError):
    """Derived CSR storage metadata is missing, duplicated, or inconsistent."""


@dataclass(frozen=True)
class CsrHardwareBinding:
    kind: CsrBindingKind
    signal: str
    priority: CsrPriority | None = None


@dataclass(frozen=True, order=True)
class CsrBlockIdentity:
    module_identity: str
    declaration_ordinal: int

    def render(self) -> str:
        return f"csr-block:{self.module_identity}:{self.declaration_ordinal}"


@dataclass(frozen=True, order=True)
class CsrRegisterIdentity:
    block: CsrBlockIdentity
    declaration_ordinal: int

    def render(self) -> str:
        return f"{self.block.render()}:register:{self.declaration_ordinal}"


@dataclass(frozen=True, order=True)
class CsrFieldIdentity:
    register: CsrRegisterIdentity
    declaration_ordinal: int

    def render(self) -> str:
        return f"{self.register.render()}:field:{self.declaration_ordinal}"


@dataclass(frozen=True)
class CsrFieldStateBinding:
    """Compiler-derived link from authoritative CSR state to implementation state."""

    csr_field_id: CsrFieldIdentity
    behavior: CsrAccess
    canonical_type: HardwareType
    field_width: int
    register_width: int
    reset_value: int
    source_origin: SourceOrigin | None
    implementation_state_id: str
    clock_domain: str | None = None
    reset_domain: str | None = None

    @property
    def semantic_state_id(self) -> str:
        return f"csr-field:{self.csr_field_id.render()}:state"

    @property
    def write_hit_id(self) -> str:
        return f"csr-field:{self.csr_field_id.render()}:write-hit"

    @property
    def write_value_id(self) -> str:
        return f"csr-field:{self.csr_field_id.render()}:write-value"


@dataclass(frozen=True)
class CsrAccessInterface:
    """Typed internal ABI for composing a CSR bank with an ordinary transport."""

    semantic_id: str
    address_width: int = 32
    data_width: int = 32
    address_port: str = "addr"
    write_port: str = "write"
    write_data_port: str = "wdata"
    read_port: str = "read"
    read_data_port: str = "rdata"
    ready_port: str = "ready"

    @property
    def input_types(self) -> tuple[tuple[str, HardwareType], ...]:
        return (
            (self.address_port, UIntType(self.address_width)),
            (self.write_port, BitType()),
            (self.write_data_port, UIntType(self.data_width)),
            (self.read_port, BitType()),
        )

    @property
    def output_types(self) -> tuple[tuple[str, HardwareType], ...]:
        return (
            (self.read_data_port, UIntType(self.data_width)),
            (self.ready_port, BitType()),
        )


def csr_state_port_name(binding: CsrFieldStateBinding) -> str:
    field = binding.csr_field_id
    return (
        f"csr_field_{field.register.block.declaration_ordinal}_"
        f"{field.register.declaration_ordinal}_{field.declaration_ordinal}_state"
    )


def csr_write_hit_port_name(binding: CsrFieldStateBinding) -> str:
    return f"{csr_state_port_name(binding)}_write_hit"


def csr_write_value_port_name(binding: CsrFieldStateBinding) -> str:
    return f"{csr_state_port_name(binding)}_write_value"


def csr_internal_port_names(
    access: CsrAccessInterface | None,
    blocks: tuple[CsrBlock, ...],
) -> frozenset[str]:
    if access is None:
        return frozenset()
    return frozenset({
        *(name for name, _ in access.input_types),
        *(name for name, _ in access.output_types),
        *(csr_state_port_name(binding)
          for block in blocks for binding in block.state_bindings),
        *(csr_write_hit_port_name(binding)
          for block in blocks for binding in block.state_bindings),
        *(csr_write_value_port_name(binding)
          for block in blocks for binding in block.state_bindings),
    })


@dataclass(frozen=True)
class CsrField:
    name: str
    type: HardwareType
    access: CsrAccess
    msb: int
    lsb: int
    reset: int
    binding: CsrHardwareBinding | None = None
    identity: CsrFieldIdentity | None = None
    source_origin: SourceOrigin | None = None

    @property
    def width(self) -> int:
        return self.msb - self.lsb + 1


@dataclass(frozen=True)
class CsrRegister:
    name: str
    offset: int
    fields: tuple[CsrField, ...]
    identity: CsrRegisterIdentity | None = None
    source_origin: SourceOrigin | None = None


@dataclass(frozen=True)
class CsrBlock:
    name: str
    base_address: int
    registers: tuple[CsrRegister, ...]
    identity: CsrBlockIdentity | None = None
    source_origin: SourceOrigin | None = None
    state_bindings: tuple[CsrFieldStateBinding, ...] = ()


def derived_state_bindings(
    block: CsrBlock,
    *,
    module_identity: str,
    block_ordinal: int = 0,
    clock_domain: str | None = None,
    reset_domain: str | None = None,
) -> tuple[CsrFieldStateBinding, ...]:
    """Return compiler-derived CSR storage metadata for legacy typed IR too."""
    if block.state_bindings:
        return block.state_bindings
    block_id = block.identity or CsrBlockIdentity(module_identity, block_ordinal)
    result: list[CsrFieldStateBinding] = []
    for register_ordinal, register in enumerate(block.registers):
        register_id = register.identity or CsrRegisterIdentity(
            block_id, register_ordinal
        )
        for field_ordinal, field in enumerate(register.fields):
            if field.access not in {
                CsrAccess.READ_WRITE,
                CsrAccess.WRITE_ONE_TO_CLEAR,
                CsrAccess.PULSE,
            }:
                continue
            field_id = field.identity or CsrFieldIdentity(
                register_id, field_ordinal
            )
            result.append(CsrFieldStateBinding(
                field_id, field.access, field.type, field.width, 32, field.reset,
                field.source_origin or register.source_origin or block.source_origin,
                f"csr-state:{field_id.render()}", clock_domain, reset_domain,
            ))
    return tuple(result)


def validate_state_bindings(block: CsrBlock) -> None:
    """Validate the frozen one-to-one stored-field implementation mapping."""
    stored = {
        field.identity: field
        for register in block.registers
        for field in register.fields
        if field.access in {
            CsrAccess.READ_WRITE,
            CsrAccess.WRITE_ONE_TO_CLEAR,
            CsrAccess.PULSE,
        }
    }
    if None in stored:
        raise CsrStateBindingError("stored CSR field is missing structured identity")
    mapped = [item.csr_field_id for item in block.state_bindings]
    if len(mapped) != len(set(mapped)):
        raise CsrStateBindingError("duplicate CSR field-state binding")
    if set(mapped) != set(stored):
        raise CsrStateBindingError("missing or fabricated CSR field-state binding")
    implementation_ids = [item.implementation_state_id
                          for item in block.state_bindings]
    if len(implementation_ids) != len(set(implementation_ids)):
        raise CsrStateBindingError("duplicate CSR implementation-state identity")
    for binding in block.state_bindings:
        field = stored[binding.csr_field_id]
        if (
            binding.behavior is not field.access
            or binding.canonical_type != field.type
            or binding.field_width != field.width
            or binding.register_width != 32
            or binding.reset_value != field.reset
        ):
            raise CsrStateBindingError(
                f"incompatible CSR field-state binding for {binding.csr_field_id.render()}"
            )
