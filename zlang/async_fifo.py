"""Compiler-owned physical decomposition of an explicit async FIFO crossing.

The crossing remains the semantic protocol boundary.  Its storage is a typed
1W1R asynchronous memory with one registered destination-domain read; the
one-slot prefetch controller is separate clocked state, not a memory rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

from zlang.common import stable_digest
from zlang.ir.expressions import InputRef
from zlang.ir.module import Connection, Module
from zlang.ir.storage import (
    Memory,
    MemoryCollision,
    MemoryPort,
    MemoryPortKind,
    MemoryResetPolicy,
)
from zlang.ir.packing import is_bit_packable
from zlang.ir.type_codec import canonical_type_data
from zlang.ir.types import BitType, UIntType


@dataclass(frozen=True)
class FifoLogic:
    """A bounded, width-aware FIFO-controller expression, shared by RTL and model."""

    op: str
    args: tuple[FifoLogic, ...] = ()
    width: int = 1
    name: str = ""
    value: int = 0

    def canonical(self) -> tuple[object, ...]:
        return (
            self.op,
            self.width,
            self.name,
            self.value,
            tuple(arg.canonical() for arg in self.args),
        )

    def evaluate(self, values: Mapping[str, int]) -> int:
        mask = (1 << self.width) - 1
        if self.op == "input":
            return int(values[self.name]) & mask
        if self.op == "constant":
            return self.value & mask
        operands = tuple(arg.evaluate(values) for arg in self.args)
        if self.op == "not":
            result = int(not operands[0])
        elif self.op == "and":
            result = int(bool(operands[0]) and bool(operands[1]))
        elif self.op == "or":
            result = int(bool(operands[0]) or bool(operands[1]))
        elif self.op == "eq":
            result = int(operands[0] == operands[1])
        elif self.op == "ne":
            result = int(operands[0] != operands[1])
        elif self.op == "xor":
            result = operands[0] ^ operands[1]
        elif self.op == "add":
            result = operands[0] + operands[1]
        elif self.op == "gray":
            result = (operands[0] >> 1) ^ operands[0]
        else:
            raise ValueError(f"unsupported FIFO controller operation: {self.op}")
        return result & mask

    def render_sv(self, symbols: Mapping[str, str], *, top_level: bool = False) -> str:
        if self.op == "input":
            return symbols.get(self.name, self.name)
        if self.op == "constant":
            return f"{self.width}'d{self.value}"
        operands = tuple(
            arg.render_sv(symbols, top_level=top_level and arg.op == "not")
            for arg in self.args
        )
        if self.op == "not":
            body = f"!{operands[0]}"
            return body if top_level else f"({body})"
        if self.op in {"and", "or", "eq", "ne", "xor"}:
            operator = {
                "and": "&&",
                "or": "||",
                "eq": "==",
                "ne": "!=",
                "xor": "^",
            }[self.op]
            body = f"{operands[0]} {operator} {operands[1]}"
            return body if top_level else f"({body})"
        if self.op == "add":
            return f"({operands[0]} + {self.width}'({operands[1]}))"
        if self.op == "gray":
            return f"(({operands[0]} >> 1) ^ {operands[0]})"
        raise ValueError(f"unsupported FIFO controller operation: {self.op}")


@dataclass(frozen=True)
class AsyncFifoController:
    """Ordered pure equations; register updates use their named results."""

    pointer_width: int
    equations: tuple[tuple[str, FifoLogic], ...]

    @property
    def identity(self) -> str:
        return stable_digest(
            {
                "schema": "zlang-async-fifo-controller-v1",
                "pointer_width": self.pointer_width,
                "equations": tuple(
                    (name, value.canonical()) for name, value in self.equations
                ),
            }
        )

    def evaluate(self, inputs: Mapping[str, int]) -> dict[str, int]:
        values = dict(inputs)
        for name, expression in self.equations:
            values[name] = expression.evaluate(values)
        return values

    def render_sv(self, symbols: Mapping[str, str]) -> tuple[str, ...]:
        return tuple(
            f"  assign {symbols.get(name, name)} = "
            f"{expression.render_sv(symbols, top_level=True)};"
            for name, expression in self.equations
        )


def build_async_fifo_controller(pointer_width: int) -> AsyncFifoController:
    """Define the exact Gray, full and registered-read prefetch equations once."""
    if pointer_width < 3:
        raise ValueError("async FIFO controller requires a pointer width >= 3")

    def ref(name: str, width: int = 1) -> FifoLogic:
        return FifoLogic("input", width=width, name=name)

    def node(op: str, *args: FifoLogic, width: int = 1) -> FifoLogic:
        return FifoLogic(op, args=tuple(args), width=width)

    def pointer(name: str) -> FifoLogic:
        return ref(name, pointer_width)

    inversion = FifoLogic(
        "constant",
        width=pointer_width,
        value=3 << (pointer_width - 2),
    )
    return AsyncFifoController(
        pointer_width,
        (
            (
                "zlang_source_ready",
                node(
                    "and",
                    node("not", ref("zlang_source_reset")),
                    node("not", ref("zlang_full")),
                ),
            ),
            (
                "zlang_destination_valid",
                node(
                    "and",
                    node("not", ref("zlang_destination_reset")),
                    ref("zlang_output_valid"),
                ),
            ),
            (
                "zlang_push",
                node("and", ref("zlang_source_valid"), ref("zlang_source_ready")),
            ),
            (
                "zlang_pop",
                node(
                    "and",
                    ref("zlang_destination_valid"),
                    ref("zlang_destination_ready"),
                ),
            ),
            (
                "zlang_write_binary_next",
                node(
                    "add",
                    pointer("zlang_write_binary"),
                    ref("zlang_push"),
                    width=pointer_width,
                ),
            ),
            (
                "zlang_write_gray_next",
                node("gray", pointer("zlang_write_binary_next"), width=pointer_width),
            ),
            (
                "zlang_read_binary_next",
                node(
                    "add",
                    pointer("zlang_read_binary"),
                    ref("zlang_pop"),
                    width=pointer_width,
                ),
            ),
            (
                "zlang_read_gray_next",
                node("gray", pointer("zlang_read_binary_next"), width=pointer_width),
            ),
            (
                "zlang_unread_current",
                node(
                    "ne", pointer("zlang_read_gray"), pointer("zlang_write_gray_sync2")
                ),
            ),
            (
                "zlang_unread_next",
                node(
                    "ne",
                    pointer("zlang_read_gray_next"),
                    pointer("zlang_write_gray_sync2"),
                ),
            ),
            (
                "zlang_fifo_prefetch",
                node(
                    "and",
                    node("not", ref("zlang_destination_reset")),
                    node(
                        "or",
                        node(
                            "and",
                            node("not", ref("zlang_output_valid")),
                            ref("zlang_unread_current"),
                        ),
                        node("and", ref("zlang_pop"), ref("zlang_unread_next")),
                    ),
                ),
            ),
            (
                "zlang_full_next",
                node(
                    "eq",
                    pointer("zlang_write_gray_next"),
                    node(
                        "xor",
                        pointer("zlang_read_gray_sync2"),
                        inversion,
                        width=pointer_width,
                    ),
                ),
            ),
        ),
    )


@dataclass(frozen=True)
class AsyncFifoRegister:
    """One stable, domain-owned state node in the FIFO physical graph."""

    identity: str
    name: str
    domain: str
    width: int
    role: str


@dataclass(frozen=True)
class AsyncFifoPhysicalPlan:
    identity: str
    memory: Memory
    depth: int
    address_width: int
    pointer_width: int
    source_domain: str
    destination_domain: str
    registers: tuple[AsyncFifoRegister, ...]
    controller: AsyncFifoController
    prefetch_policy: str = "one_slot_registered_read_v1"

    def validate(self) -> None:
        if self.depth < 4 or self.depth & (self.depth - 1):
            raise ValueError(
                "async FIFO depth must be a power of two and at least four"
            )
        if (
            self.address_width != (self.depth - 1).bit_length()
            or self.pointer_width != self.address_width + 1
        ):
            raise ValueError("async FIFO pointer widths do not match storage depth")
        if self.memory.depth != self.depth or self.memory.read_latency != 1:
            raise ValueError("async FIFO storage must have one exact read cycle")
        if not self.memory.async_memory or len(self.memory.ports) != 2:
            raise ValueError("async FIFO storage must be typed 1W1R async_mem")
        if self.memory.contents_reset is not MemoryResetPolicy.PRESERVE:
            raise ValueError("async FIFO cell contents must hold across reset")
        if self.memory.read_data_reset is not MemoryResetPolicy.CLEAR:
            raise ValueError("async FIFO output stage must clear on reader reset")
        if self.memory.collision is not MemoryCollision.READ_FIRST:
            raise ValueError(
                "async FIFO storage uses pre-edge old-data collision model"
            )
        writer, reader = self.memory.ports
        if (
            writer.kind is not MemoryPortKind.WRITE
            or reader.kind is not MemoryPortKind.READ
            or writer.domain != self.source_domain
            or reader.domain != self.destination_domain
            or writer.write_enable is None
            or writer.write_data is None
            or reader.read_enable is None
            or writer.address.type != UIntType(self.address_width)
            or reader.address.type != UIntType(self.address_width)
        ):
            raise ValueError("async FIFO memory ports do not match the physical graph")
        if self.prefetch_policy != "one_slot_registered_read_v1":
            raise ValueError("unsupported async FIFO prefetch policy")
        if self.controller != build_async_fifo_controller(self.pointer_width):
            raise ValueError(
                "async FIFO controller equations do not match physical graph"
            )
        if self.source_domain == self.destination_domain:
            raise ValueError("async FIFO domains must differ")
        expected = (
            ("zlang_write_binary", self.source_domain, self.pointer_width),
            ("zlang_write_gray", self.source_domain, self.pointer_width),
            ("zlang_read_gray_sync1", self.source_domain, self.pointer_width),
            ("zlang_read_gray_sync2", self.source_domain, self.pointer_width),
            ("zlang_full", self.source_domain, 1),
            ("zlang_read_binary", self.destination_domain, self.pointer_width),
            ("zlang_read_gray", self.destination_domain, self.pointer_width),
            ("zlang_write_gray_sync1", self.destination_domain, self.pointer_width),
            ("zlang_write_gray_sync2", self.destination_domain, self.pointer_width),
            ("zlang_output_valid", self.destination_domain, 1),
        )
        actual = tuple((item.name, item.domain, item.width) for item in self.registers)
        if actual != expected or any(
            item.identity != f"{self.identity}:state:{item.name}"
            for item in self.registers
        ):
            raise ValueError("async FIFO state layout does not match physical graph")


def build_async_fifo_physical_plan(
    module: Module,
    connection: Connection,
) -> AsyncFifoPhysicalPlan:
    crossing = connection.crossing
    if (
        crossing is None
        or crossing.kind.value != "async_fifo"
        or crossing.depth is None
    ):
        raise ValueError("async FIFO physical planning requires an explicit crossing")
    source, destination = connection.source, connection.destination
    if source.domain is None or destination.domain is None:
        raise ValueError("async FIFO endpoints require resolved domains")
    if source.type != destination.type or not is_bit_packable(source.type):
        raise ValueError("async FIFO storage requires one exact bit-packable payload")
    depth = crossing.depth
    address_width = (depth - 1).bit_length()
    domain_data = {
        domain.clock: (
            domain.clock,
            domain.reset,
            domain.edge.value,
            domain.reset_mode.value,
            domain.reset_polarity.value,
            domain.reset_release_mode.value,
            domain.reset_release_cycles,
        )
        for domain in module.clock_domains
    }
    try:
        domains = (domain_data[source.domain], domain_data[destination.domain])
    except KeyError as error:
        raise ValueError("async FIFO references an unavailable clock domain") from error
    identity = "async-fifo-physical:" + stable_digest(
        {
            "schema": "zlang-async-fifo-physical-v2",
            "module": module.name,
            "endpoints": (source.name, destination.name),
            "payload_type": canonical_type_data(source.type),
            "depth": depth,
            "domains": domains,
            "memory_read_latency": 1,
            "prefetch": "one_slot_registered_read_v1",
            "controller": build_async_fifo_controller(address_width + 1).identity,
        }
    )
    bit = BitType()
    address = UIntType(address_width)
    memory = Memory(
        name="fifo_storage",
        semantic_id=identity + ":storage",
        element_type=source.type,
        depth=depth,
        read_latency=1,
        collision=MemoryCollision.READ_FIRST,
        read_address=None,
        write_enable=None,
        write_address=None,
        write_data=None,
        contents_reset=MemoryResetPolicy.PRESERVE,
        read_data_reset=MemoryResetPolicy.CLEAR,
        ports=(
            MemoryPort(
                "wr",
                identity + ":wr",
                MemoryPortKind.WRITE,
                source.domain,
                InputRef("zlang_fifo_write_address", address),
                write_enable=InputRef("zlang_push", bit),
                write_data=InputRef("zlang_fifo_input_payload", source.type),
            ),
            MemoryPort(
                "rd",
                identity + ":rd",
                MemoryPortKind.READ,
                destination.domain,
                InputRef("zlang_fifo_fetch_address", address),
                read_enable=InputRef("zlang_fifo_prefetch", bit),
            ),
        ),
        async_memory=True,
    )
    plan = AsyncFifoPhysicalPlan(
        identity,
        memory,
        depth,
        address_width,
        address_width + 1,
        source.domain,
        destination.domain,
        tuple(
            AsyncFifoRegister(
                f"{identity}:state:{name}",
                name,
                domain,
                width,
                name,
            )
            for name, domain, width in (
                ("zlang_write_binary", source.domain, address_width + 1),
                ("zlang_write_gray", source.domain, address_width + 1),
                ("zlang_read_gray_sync1", source.domain, address_width + 1),
                ("zlang_read_gray_sync2", source.domain, address_width + 1),
                ("zlang_full", source.domain, 1),
                ("zlang_read_binary", destination.domain, address_width + 1),
                ("zlang_read_gray", destination.domain, address_width + 1),
                ("zlang_write_gray_sync1", destination.domain, address_width + 1),
                ("zlang_write_gray_sync2", destination.domain, address_width + 1),
                ("zlang_output_valid", destination.domain, 1),
            )
        ),
        build_async_fifo_controller(address_width + 1),
    )
    plan.validate()
    return plan


__all__ = [
    "AsyncFifoPhysicalPlan",
    "AsyncFifoRegister",
    "build_async_fifo_physical_plan",
]
