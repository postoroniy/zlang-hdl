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


def access_owns_state(access: CsrAccess) -> bool:
    """Return whether one access policy owns persistent compiler state.

    Keep this predicate authoritative.  Direct-SV and simulation have always
    stored ``wo`` fields; omitting them from the semantic state binding made
    hierarchy observation disagree with execution.
    """

    return access in {
        CsrAccess.READ_WRITE,
        CsrAccess.WRITE_ONLY,
        CsrAccess.WRITE_ONE_TO_CLEAR,
        CsrAccess.PULSE,
    }


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


@dataclass(frozen=True, order=True)
class CsrEventIdentity:
    register: CsrRegisterIdentity
    declaration_ordinal: int

    def render(self) -> str:
        return f"{self.register.render()}:event:{self.declaration_ordinal}"


class CsrEventKind(str, Enum):
    READ = "on_read"
    WRITE = "on_write"


@dataclass(frozen=True)
class CsrAccessObservation:
    """Storage-independent compiler-qualified access observation."""

    csr_field_id: CsrFieldIdentity
    access: CsrAccess
    canonical_type: HardwareType
    field_width: int
    source_origin: SourceOrigin | None
    clock_domain: str | None = None

    @property
    def read_hit_id(self) -> str:
        return f"csr-field:{self.csr_field_id.render()}:read-hit"

    @property
    def write_hit_id(self) -> str:
        return f"csr-field:{self.csr_field_id.render()}:write-hit"

    @property
    def write_value_id(self) -> str:
        return f"csr-field:{self.csr_field_id.render()}:write-value"

    @property
    def value_id(self) -> str:
        return f"csr-field:{self.csr_field_id.render()}:value"


@dataclass(frozen=True)
class CsrEventBinding:
    identity: CsrEventIdentity
    name: str
    kind: CsrEventKind
    canonical_type: HardwareType
    msb: int
    lsb: int
    signal: str
    source_origin: SourceOrigin | None
    clock_domain: str | None = None

    @property
    def semantic_value_id(self) -> str:
        return f"csr-event:{self.identity.render()}:value"


@dataclass(frozen=True)
class CsrSplitView:
    """One logical value backed by independently writable CSR words."""

    name: str
    field_name: str
    canonical_type: HardwareType
    low_field_id: CsrFieldIdentity
    high_field_id: CsrFieldIdentity
    low_first: bool
    source_origin: SourceOrigin | None

    @property
    def semantic_value_id(self) -> str:
        return (
            f"csr-split:{self.low_field_id.register.block.render()}:"
            f"{self.name}:{self.field_name}"
        )


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
    return csr_state_port_name_from_ordinals(
        field.register.block.declaration_ordinal,
        field.register.declaration_ordinal,
        field.declaration_ordinal,
    )


def csr_state_port_name_from_ordinals(
    block_ordinal: int,
    register_ordinal: int,
    field_ordinal: int,
) -> str:
    """Return the stable physical ABI name without constructing typed state.

    Hierarchy analysis must expose compiler-generated CSR outputs before parent
    immutable bindings are checked. At that point the child syntax has exact
    declaration ordinals and types, but its final ``CsrFieldStateBinding`` does
    not exist yet. Keeping the spelling here prevents the early declaration
    pass and final typed IR from acquiring separate ABI formulas.
    """

    if min(block_ordinal, register_ordinal, field_ordinal) < 0:
        raise ValueError("CSR declaration ordinals must be non-negative")
    return f"csr_field_{block_ordinal}_{register_ordinal}_{field_ordinal}_state"


def csr_write_hit_port_name(binding: CsrFieldStateBinding) -> str:
    return f"{csr_state_port_name(binding)}_write_hit"


def csr_write_value_port_name(binding: CsrFieldStateBinding) -> str:
    return f"{csr_state_port_name(binding)}_write_value"


def csr_read_hit_port_name(observation: CsrAccessObservation) -> str:
    field = observation.csr_field_id
    return (
        f"csr_field_{field.register.block.declaration_ordinal}_"
        f"{field.register.declaration_ordinal}_{field.declaration_ordinal}_read_hit"
    )


def csr_observation_write_hit_port_name(
    observation: CsrAccessObservation,
) -> str:
    field = observation.csr_field_id
    if access_owns_state(observation.access):
        return (
            f"csr_field_{field.register.block.declaration_ordinal}_"
            f"{field.register.declaration_ordinal}_{field.declaration_ordinal}_"
            "state_write_hit"
        )
    return (
        f"csr_field_{field.register.block.declaration_ordinal}_"
        f"{field.register.declaration_ordinal}_{field.declaration_ordinal}_write_hit"
    )


def csr_observation_write_value_port_name(
    observation: CsrAccessObservation,
) -> str:
    if access_owns_state(observation.access):
        field = observation.csr_field_id
        return (
            f"csr_field_{field.register.block.declaration_ordinal}_"
            f"{field.register.declaration_ordinal}_{field.declaration_ordinal}_"
            "state_write_value"
        )
    return f"{csr_observation_write_hit_port_name(observation)}_value"


def csr_observation_value_port_name(observation: CsrAccessObservation) -> str:
    field = observation.csr_field_id
    return (
        f"csr_field_{field.register.block.declaration_ordinal}_"
        f"{field.register.declaration_ordinal}_{field.declaration_ordinal}_value"
    )


def csr_event_port_name(binding: CsrEventBinding) -> str:
    event = binding.identity
    return (
        f"csr_event_{event.register.block.declaration_ordinal}_"
        f"{event.register.declaration_ordinal}_{event.declaration_ordinal}_value"
    )


def csr_split_port_name(block: CsrBlock, view: CsrSplitView) -> str:
    index = block.split_views.index(view)
    return f"csr_split_{block.identity.declaration_ordinal}_{index}_value"


def csr_named_state_path(
    block: CsrBlock,
    binding: CsrFieldStateBinding,
) -> tuple[str, str, str]:
    """Return the stable source-level path for one stored CSR field.

    Physical component ports retain the ordinal ABI for compatibility.  The
    semantic parent projection uses declaration names and resolves to that
    same port, so inserting an unrelated field does not leak ordinals into
    integration source.
    """

    field_id = binding.csr_field_id
    if field_id.register.block != block.identity:
        raise CsrStateBindingError(
            f"CSR state binding {field_id.render()} does not belong to "
            f"block {block.name}"
        )
    register_index = field_id.register.declaration_ordinal
    field_index = field_id.declaration_ordinal
    try:
        register = block.registers[register_index]
        field = register.fields[field_index]
    except IndexError as error:
        raise CsrStateBindingError(
            f"CSR state binding {field_id.render()} has an invalid declaration ordinal"
        ) from error
    if register.identity != field_id.register or field.identity != field_id:
        raise CsrStateBindingError(
            f"CSR state binding {field_id.render()} does not match declaration identity"
        )
    return block.name, register.name, field.name


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
        *(csr_read_hit_port_name(binding)
          for block in blocks for binding in block.access_observations),
        *(csr_observation_write_hit_port_name(binding)
          for block in blocks for binding in block.access_observations),
        *(csr_observation_write_value_port_name(binding)
          for block in blocks for binding in block.access_observations),
        *(csr_observation_value_port_name(binding)
          for block in blocks for binding in block.access_observations),
        *(csr_event_port_name(binding)
          for block in blocks for register in block.registers
          for binding in register.events),
        *(csr_split_port_name(block, view)
          for block in blocks for view in block.split_views),
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
    events: tuple[CsrEventBinding, ...] = ()
    identity: CsrRegisterIdentity | None = None
    source_origin: SourceOrigin | None = None
    projection_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class CsrBlock:
    name: str
    base_address: int
    registers: tuple[CsrRegister, ...]
    identity: CsrBlockIdentity | None = None
    source_origin: SourceOrigin | None = None
    state_bindings: tuple[CsrFieldStateBinding, ...] = ()
    access_observations: tuple[CsrAccessObservation, ...] = ()
    split_views: tuple[CsrSplitView, ...] = ()
    domain: str | None = None
    reset: str | None = None


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
            if not access_owns_state(field.access):
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


def derived_access_observations(
    block: CsrBlock,
    *,
    module_identity: str,
    block_ordinal: int = 0,
    clock_domain: str | None = None,
) -> tuple[CsrAccessObservation, ...]:
    """Return exact field observations for legacy constructed typed IR."""

    if block.access_observations:
        return block.access_observations
    block_id = block.identity or CsrBlockIdentity(module_identity, block_ordinal)
    result: list[CsrAccessObservation] = []
    for register_ordinal, register in enumerate(block.registers):
        register_id = register.identity or CsrRegisterIdentity(
            block_id, register_ordinal
        )
        for field_ordinal, field in enumerate(register.fields):
            if field.access is CsrAccess.RESERVED:
                continue
            field_id = field.identity or CsrFieldIdentity(register_id, field_ordinal)
            result.append(CsrAccessObservation(
                field_id,
                field.access,
                field.type,
                field.width,
                field.source_origin or register.source_origin or block.source_origin,
                clock_domain,
            ))
    return tuple(result)


def validate_state_bindings(block: CsrBlock) -> None:
    """Validate the frozen one-to-one stored-field implementation mapping."""
    stored = {
        field.identity: field
        for register in block.registers
        for field in register.fields
        if access_owns_state(field.access)
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
