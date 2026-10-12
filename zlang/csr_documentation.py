"""Immutable software-documentation view derived from typed CSR IR."""

from __future__ import annotations

from dataclasses import dataclass

from zlang.ir.module import Module


@dataclass(frozen=True)
class CsrHardwareDocumentation:
    kind: str
    signal: str
    priority: str | None


@dataclass(frozen=True)
class CsrFieldDocumentation:
    name: str
    type_name: str
    access: str
    msb: int
    lsb: int
    width: int
    reset: int
    hardware: CsrHardwareDocumentation | None


@dataclass(frozen=True)
class CsrEventDocumentation:
    name: str
    kind: str
    phase: str
    type_name: str
    msb: int
    lsb: int
    signal: str


@dataclass(frozen=True)
class CsrRegisterDocumentation:
    name: str
    logical_path: tuple[str, ...]
    offset: int
    address: int
    fields: tuple[CsrFieldDocumentation, ...]
    events: tuple[CsrEventDocumentation, ...]

    @property
    def logical_name(self) -> str:
        return ".".join(self.logical_path) if self.logical_path else self.name


@dataclass(frozen=True)
class CsrSplitViewDocumentation:
    name: str
    field_name: str
    type_name: str
    order: str
    physical_registers: tuple[str, str]


@dataclass(frozen=True)
class CsrBlockDocumentation:
    name: str
    base_address: int
    registers: tuple[CsrRegisterDocumentation, ...]
    split_views: tuple[CsrSplitViewDocumentation, ...]


@dataclass(frozen=True)
class CsrDocumentation:
    module_name: str
    data_width: int
    address_width: int
    blocks: tuple[CsrBlockDocumentation, ...]

    @property
    def has_hardware_bindings(self) -> bool:
        return any(
            field.hardware is not None
            for block in self.blocks
            for register in block.registers
            for field in register.fields
        )


def build_csr_documentation(module: Module) -> CsrDocumentation:
    """Build the one software-facing view consumed by all CSR renderers."""

    blocks: list[CsrBlockDocumentation] = []
    for block in module.csr_blocks:
        registers = tuple(
            CsrRegisterDocumentation(
                register.name,
                register.projection_path,
                register.offset,
                block.base_address + register.offset,
                tuple(
                    CsrFieldDocumentation(
                        field.name,
                        str(field.type),
                        field.access.value,
                        field.msb,
                        field.lsb,
                        field.width,
                        field.reset,
                        (
                            CsrHardwareDocumentation(
                                field.binding.kind.value,
                                field.binding.signal,
                                (
                                    field.binding.priority.value
                                    if field.binding.priority is not None
                                    else None
                                ),
                            )
                            if field.binding is not None
                            else None
                        ),
                    )
                    for field in register.fields
                ),
                tuple(
                    CsrEventDocumentation(
                        event.name,
                        event.kind.value,
                        event.phase.value,
                        str(event.canonical_type),
                        event.msb,
                        event.lsb,
                        event.signal,
                    )
                    for event in register.events
                ),
            )
            for register in block.registers
        )
        split_views = tuple(
            CsrSplitViewDocumentation(
                view.name,
                view.field_name,
                str(view.canonical_type),
                "low_first" if view.low_first else "high_first",
                (
                    next(
                        register.name
                        for register in block.registers
                        if any(
                            field.identity == view.low_field_id
                            for field in register.fields
                        )
                    ),
                    next(
                        register.name
                        for register in block.registers
                        if any(
                            field.identity == view.high_field_id
                            for field in register.fields
                        )
                    ),
                ),
            )
            for view in block.split_views
        )
        blocks.append(
            CsrBlockDocumentation(
                block.name,
                block.base_address,
                registers,
                split_views,
            )
        )
    return CsrDocumentation(module.name, 32, 32, tuple(blocks))


__all__ = [
    "CsrBlockDocumentation",
    "CsrDocumentation",
    "CsrEventDocumentation",
    "CsrFieldDocumentation",
    "CsrHardwareDocumentation",
    "CsrRegisterDocumentation",
    "CsrSplitViewDocumentation",
    "build_csr_documentation",
]
