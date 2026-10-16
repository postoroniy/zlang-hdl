# SPDX-License-Identifier: Apache-2.0
"""Debug presentation must consume existing exact types and packing, not guess."""

from pathlib import Path

import pytest

from zlang import sim
from zlang.backend.systemverilog import emit_artifact
from zlang.ir.packing import pack_runtime, pack_tagged_union_runtime
from zlang.ir.runtime_values import TaggedUnionValue
from zlang.ir.types import (
    BitType, BitsType, EnumType, FixedType, SIntType, StructField, StructType,
    TaggedUnionField, TaggedUnionType, TaggedUnionVariant, TupleType, UFixedType,
    UIntType, VecType,
)
from zlang.native_debug_bundle import build_native_debug_bundle
from zlang.native_debug_types import debug_type


@pytest.mark.parametrize("edge", ("rising", "falling"))
@pytest.mark.parametrize("mode,polarity", (("synchronous", "active_high"), ("asynchronous", "active_low")))
def test_clock_reset_catalog_uses_exact_plan_controls_without_new_value_ports(
    tmp_path: Path, edge: str, mode: str, polarity: str
) -> None:
    path = tmp_path / "Controls.zhl"
    reset = (f"async reset rst @clk {{ polarity {polarity} }}"
             if mode == "asynchronous" else
             f"reset rst @clk {{ mode synchronous polarity {polarity} power_up unspecified }}")
    path.write_text(f"""module Controls {{
    clock clk {{ edge {edge} }}
    {reset}
    out reg count : u8 = 0
    when 1 {{ count <- truncate(count + 1) }}
}}""", encoding="utf-8")
    ordinary = sim.compile(path, top="Controls", logic_state=True)
    bundle = build_native_debug_bundle(path, top="Controls")
    signals = {signal.name: signal for signal in bundle.signals}
    for name, kind in (("clk", "clock"), ("rst", "reset")):
        signal = signals[name]
        assert (signal.kind, signal.width, signal.binding, signal.direction) == (kind, 1, name, "input")
        assert signal.layout.kind == "bit"
    assert bundle.plan_bytes == ordinary.plan.to_bytes()
    assert bundle.selected_ir_identity == ordinary.selected_ir_identity
    assert {port["name"] for port in ordinary.plan.payload["ports"]} == {"count"}
    assert [(clock.edge, clock.reset_mode, clock.reset_polarity) for clock in bundle.clocks] == [
        (edge, mode, polarity)
    ]


def test_debug_layout_offsets_agree_with_authoritative_packing() -> None:
    struct = StructType("Pair", (StructField("a", UIntType(3)), StructField("b", SIntType(5))))
    layout = debug_type(struct)
    bits = pack_runtime(struct, {"a": 5, "b": -7})
    assert [(field.name, field.lsb, field.layout.width) for field in layout.members] == [
        ("a", 5, 3), ("b", 0, 5),
    ]
    assert [(bits >> field.lsb) & ((1 << field.layout.width)-1) for field in layout.members] == [5, 25]
    tuple_type = TupleType((UIntType(3), SIntType(5)))
    assert [field.lsb for field in debug_type(tuple_type).members] == [0, 3]
    vector = debug_type(VecType(1_000_000, BitType()))
    assert vector.length == 1_000_000 and vector.stride == 1
    assert vector.element.width == 1 and vector.members == ()


def test_debug_union_variants_retain_exact_tags_padding_and_field_slices() -> None:
    type_ = TaggedUnionType("Message", (
        TaggedUnionVariant("Idle", ()),
        TaggedUnionVariant("Data", (TaggedUnionField("value", SIntType(8)),)),
        TaggedUnionVariant("Error", (TaggedUnionField("code", BitsType(4)),)),
    ), "message-declaration")
    layout = debug_type(type_)
    assert layout.members[0].name == "tag" and layout.members[0].lsb == 8
    error = layout.members[3]
    assert error.guard == (8, 2, 2)
    assert error.layout.members[0].lsb == 4  # Narrow payload is MSB-aligned.
    packed = pack_tagged_union_runtime(TaggedUnionValue(type_, "Error", (("code", 13),)))
    assert packed == (2 << 8) | (13 << 4)


def test_debug_layout_retains_all_supported_scalar_and_aggregate_families() -> None:
    enum = EnumType("Sparse", ("Idle", "Done"), "enum-id", 130, (0, (1 << 129)+7), "uint")
    layout = debug_type(enum).to_data()
    assert layout["enum_values"][1] == {"name":"Done", "bits":format((1 << 129)+7, "0130b")}
    for type_, kind in ((BitType(), "bit"), (BitsType(65), "bits"), (UIntType(256), "uint"),
                        (SIntType(128), "sint"), (FixedType(16, 7), "fixed"), (UFixedType(8, 3), "ufixed")):
        value = debug_type(type_).to_data()
        assert value["kind"] == kind and value["width"] == type_.width
        if "fixed" in kind:
            assert value["canonical"]["fraction"] == type_.fraction


def test_debug_catalog_does_not_change_production_plan_or_rtl(tmp_path: Path) -> None:
    path = tmp_path / "Typed.zhl"
    path.write_text("""
enum Mode : u8 { Idle Ready = 10 Done = 121 }
struct Header { mode : Mode count : s8 }
union Message { Idle Data { value : s8 } Error { code : bits<4> } }
module Typed {
    clock clk reset rst
    in code : u8
    in count : s8
    in message : Message
    out forwarded : Header
    out current : Message
    reg saved : Message = Message.Idle
    when 1 { saved <- message }
    forwarded = Header { mode = enum_decode<Mode>(code, Mode.Idle) count }
    current = saved
}
""", encoding="utf-8")
    before = sim.compile(path, top="Typed", logic_state=True)
    artifact = emit_artifact(before.module)
    first = build_native_debug_bundle(path, top="Typed")
    second = build_native_debug_bundle(path, top="Typed")
    assert first.plan_bytes == before.plan.to_bytes() == second.plan_bytes
    assert first.selected_ir_identity == before.selected_ir_identity
    assert first.identity == second.identity and first.overlay_identity == second.overlay_identity
    assert emit_artifact(before.module).to_json() == artifact.to_json()
    signals = {signal.name: signal for signal in first.signals}
    assert signals["forwarded"].layout.kind == "struct"
    assert signals["forwarded"].layout.members[0].layout.kind == "enum"
    assert signals["saved"].layout.kind == "tagged_union"
    assert signals["current"].layout == signals["saved"].layout


def test_ready_valid_catalog_is_a_group_of_oppositely_directed_bindings(tmp_path: Path) -> None:
    path = tmp_path / "Flow.zhl"
    path.write_text("""
struct Payload { value : s8 flag : bit }
module Flow {
    clock clk reset rst
    in rx : rv<Payload>
    out tx : rv<Payload>
    rx -> tx
}
""", encoding="utf-8")
    bundle = build_native_debug_bundle(path, top="Flow")
    signals = {item.name: item for item in bundle.signals}
    rx = signals["rx"]
    assert rx.kind == "interface" and rx.width == 0
    fields = {item.name: item for item in rx.layout.members}
    assert fields["payload"].layout.kind == "struct"
    assert fields["valid"].direction == "input"
    assert fields["ready"].direction == "output"
    assert all(item.binding.startswith("$zlang_protocol:rx:") for item in fields.values())
    assert all(item.domain == "clk" for item in fields.values())


def test_aggregate_interface_preserves_channel_ownership_not_packed_width(tmp_path: Path) -> None:
    path = tmp_path / "Bus.zhl"
    path.write_text("""
protocol TinyBus {
    role initiator role target
    channel req : rv<u8> initiator -> target
    channel rsp : rv<u8> target -> initiator
    member irq : bit target -> initiator
}
module Bus {
    clock clk reset rst
    interface bus : TinyBus.initiator
    bus.req.valid = 0 bus.req.payload = 0 bus.rsp.ready = 1
    out alarm : bit
    alarm = bus.irq
}
""", encoding="utf-8")
    bundle = build_native_debug_bundle(path, top="Bus")
    bus = next(item for item in bundle.signals if item.name == "bus")
    assert bus.width == 0 and bus.layout.kind == "interface"
    assert {item.name for item in bus.layout.members} == {"req", "rsp", "irq"}
    members = {item.name: item for item in bus.layout.members}
    assert members["req"].layout.kind == "interface"
    assert members["irq"].direction == "input"
    request = {item.name: item for item in members["req"].layout.members}
    assert request["valid"].direction == "output"
    assert request["ready"].direction == "input"
