"""Typed storage resources and their externally visible signals."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from zlang.ir.types import HardwareType, UIntType
from zlang.source import SourceOrigin

if TYPE_CHECKING:
    from zlang.ir.expressions import Expression


class FifoSignal(str, Enum):
    DATA = "data"
    PUSH = "push"
    POP = "pop"
    FRONT = "front"
    FULL = "full"
    EMPTY = "empty"
    READY = "ready"
    VALID = "valid"
    COUNT = "count"
    OVERFLOW = "overflow"
    UNDERFLOW = "underflow"


class MemorySignal(str, Enum):
    READ_ADDRESS = "read_address"
    WRITE_ENABLE = "write_enable"
    WRITE_ADDRESS = "write_address"
    WRITE_DATA = "write_data"
    WRITE_MASK = "write_mask"
    READ_DATA = "read_data"


class RomSignal(str, Enum):
    READ_ADDRESS = "read_address"
    READ_DATA = "read_data"


class MemoryCollision(str, Enum):
    READ_FIRST = "read_first"
    WRITE_FIRST = "write_first"
    NO_CHANGE = "no_change"


class MemoryPortKind(str, Enum):
    READ = "read"
    WRITE = "write"
    READ_WRITE = "read_write"


@dataclass(frozen=True)
class MemoryPort:
    """One fully typed logical memory access port."""

    name: str
    semantic_id: str
    kind: MemoryPortKind
    domain: str
    address: Expression
    read_enable: Expression | None = None
    write_enable: Expression | None = None
    write_data: Expression | None = None
    write_mask: Expression | None = None
    source_origin: SourceOrigin | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.semantic_id or not self.domain:
            raise ValueError("memory port name, identity, and domain are required")
        if not isinstance(self.kind, MemoryPortKind):
            raise ValueError("memory port kind is invalid")
        readable = self.kind in {MemoryPortKind.READ, MemoryPortKind.READ_WRITE}
        writable = self.kind in {MemoryPortKind.WRITE, MemoryPortKind.READ_WRITE}
        if readable != (self.read_enable is not None):
            raise ValueError("readable memory port requires one read-enable expression")
        if writable != (self.write_enable is not None and self.write_data is not None):
            raise ValueError("writable memory port requires write enable and data")
        if not writable and self.write_mask is not None:
            raise ValueError("read-only memory port cannot carry a write mask")


class MemoryResetPolicy(str, Enum):
    CLEAR = "clear"
    PRESERVE = "preserve"


def memory_byte_mask_width(element_width: int) -> int:
    """Return the byte-lane mask width for one positive packed word width."""

    if element_width < 1:
        raise ValueError("a memory element width must be positive")
    return (element_width + 7) // 8


@dataclass(frozen=True)
class Fifo:
    name: str
    element_type: HardwareType
    depth: int
    data: Expression | None
    push: Expression | None
    pop: Expression | None
    source_origin: object | None = None
    domain: str | None = None

    @property
    def scheduled(self) -> bool:
        return self.data is None

    @property
    def count_width(self) -> int:
        return max(1, self.depth.bit_length())


@dataclass(frozen=True)
class Memory:
    name: str
    semantic_id: str
    element_type: HardwareType
    depth: int
    read_latency: int
    collision: MemoryCollision
    read_address: Expression | None
    write_enable: Expression | None
    write_address: Expression | None
    write_data: Expression | None
    source_origin: SourceOrigin | None = None
    write_mask_width: int | None = None
    write_mask: Expression | None = None
    # Appended to preserve the historical positional constructor ABI.
    contents_reset: MemoryResetPolicy = MemoryResetPolicy.CLEAR
    read_data_reset: MemoryResetPolicy = MemoryResetPolicy.CLEAR
    domain: str | None = None
    ports: tuple[MemoryPort, ...] = ()
    async_memory: bool = False
    write_priority: tuple[str, ...] = ()
    initial_value: Expression | None = None

    def __post_init__(self) -> None:
        if not self.semantic_id:
            raise ValueError("memory semantic identity must not be empty")
        if not 0 <= self.read_latency <= 16:
            raise ValueError("memory read latency must be in 0..16")
        if self.initial_value is not None and self.initial_value.type != self.element_type:
            raise ValueError("memory initial value must have the exact element type")
        for label, policy in (
            ("contents", self.contents_reset),
            ("read data", self.read_data_reset),
        ):
            if not isinstance(policy, MemoryResetPolicy):
                raise ValueError(f"memory {label} reset policy is invalid")
        controls = (
            self.read_address, self.write_enable,
            self.write_address, self.write_data,
        )
        if any(item is None for item in controls) and not all(
            item is None for item in controls
        ):
            raise ValueError("memory controls must be all present or all absent")
        if self.write_mask_width is not None:
            if self.write_mask_width != memory_byte_mask_width(
                self.element_type.width
            ):
                raise ValueError("memory write-mask width must match the byte-lane count")
        if self.write_mask is not None and self.write_mask_width is None:
            raise ValueError("memory write-mask expression requires mask metadata")
        if self.read_address is None and self.write_mask is not None:
            raise ValueError("scheduled memory masks belong to write actions")
        if self.read_address is None and self.read_latency == 0:
            if not self.ports:
                raise ValueError("scheduled memory requires read latency one")
        if self.read_address is not None and (
            (self.write_mask_width is None) != (self.write_mask is None)
        ):
            raise ValueError("global masked memory requires a write-mask expression")
        if self.ports:
            if any(item is not None for item in controls) or self.write_mask is not None:
                raise ValueError("ported memory cannot also use legacy controls")
            if len(self.ports) > 8:
                raise ValueError("ported memory supports at most eight logical ports")
            names = tuple(port.name for port in self.ports)
            if len(names) != len(set(names)):
                raise ValueError("memory port names must be unique")
            writable = tuple(
                port.name for port in self.ports
                if port.kind in {MemoryPortKind.WRITE, MemoryPortKind.READ_WRITE}
            )
            if len(writable) > 1 and set(self.write_priority) != set(writable):
                raise ValueError("multi-writer memory requires a complete write priority")
            if self.async_memory:
                kinds = tuple(port.kind for port in self.ports)
                if kinds.count(MemoryPortKind.WRITE) != 1 or kinds.count(MemoryPortKind.READ) != 1 or len(kinds) != 2:
                    raise ValueError("async memory requires exactly one write and one read port")
                if len({port.domain for port in self.ports}) != 2:
                    raise ValueError("async memory ports must use different domains")
                if self.read_latency < 1:
                    raise ValueError("async memory requires at least one read cycle")
            elif len({port.domain for port in self.ports}) != 1:
                raise ValueError("ordinary ported memory requires one clock domain")
        elif self.async_memory or self.write_priority:
            raise ValueError("async/priority memory metadata requires named ports")

    @property
    def scheduled(self) -> bool:
        return self.read_address is None and not self.ports

    @property
    def ported(self) -> bool:
        return bool(self.ports)

    @property
    def address_width(self) -> int:
        return max(1, (self.depth - 1).bit_length())


@dataclass(frozen=True)
class Rom:
    """Immutable, compile-time initialized, one-cycle synchronous ROM."""

    name: str
    semantic_id: str
    element_type: HardwareType
    depth: int
    address_type: HardwareType
    read_latency: int
    contents: tuple[Expression, ...]
    read_address: Expression
    initialization_identity: str
    dependency_identity: tuple[tuple[str, str], ...]
    evaluator_schema: str
    content_hash: str
    source_origin: SourceOrigin | None = None
    domain: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ROM name must not be empty")
        if not self.semantic_id:
            raise ValueError("ROM semantic identity must not be empty")
        if self.depth < 1:
            raise ValueError("ROM depth must be positive")
        if self.read_latency != 1:
            raise ValueError("ROM read latency must be exactly one")
        expected_address_type = UIntType(self.address_width)
        if self.address_type != expected_address_type:
            raise ValueError(
                f"ROM address type must be {expected_address_type}, got {self.address_type}"
            )
        if self.read_address.type != self.address_type:
            raise ValueError("ROM read-address expression has the wrong type")
        if len(self.contents) != self.depth:
            raise ValueError(
                f"ROM contents contain {len(self.contents)} words, expected {self.depth}"
            )
        if any(word.type != self.element_type for word in self.contents):
            raise ValueError("ROM contents must use the declared element type")
        for label, value in (
            ("initialization identity", self.initialization_identity),
            ("evaluator schema", self.evaluator_schema),
            ("content hash", self.content_hash),
        ):
            if not value:
                raise ValueError(f"ROM {label} must not be empty")

    @property
    def address_width(self) -> int:
        return max(1, (self.depth - 1).bit_length())
