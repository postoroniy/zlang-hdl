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

    def __post_init__(self) -> None:
        if not self.semantic_id:
            raise ValueError("memory semantic identity must not be empty")
        if self.read_latency not in {0, 1}:
            raise ValueError("memory read latency must be zero or one")
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
            raise ValueError("scheduled memory requires read latency one")
        if self.read_address is not None and (
            (self.write_mask_width is None) != (self.write_mask is None)
        ):
            raise ValueError("global masked memory requires a write-mask expression")

    @property
    def scheduled(self) -> bool:
        return self.read_address is None

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
