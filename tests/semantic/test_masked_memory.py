from dataclasses import replace

import pytest

from zlang.ir import expressions as expr
from zlang.ir.state import StateActionKind
from zlang.ir.storage import (
    Memory,
    MemoryCollision,
    MemoryResetPolicy,
    memory_byte_mask_width,
)
from zlang.ir.types import BitsType, UIntType
from zlang.opt.lowering import CanonicalizationError, lower, restore
from zlang.opt.ir import CanonicalMemory
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.simulate import simulate_cycles
from zlang.compiler import compile_source
from zlang.targets import TargetArchitectureError


def scheduled_source(collision: str = "write_first") -> str:
    return f"""
module MaskedScheduledMemory {{
  clock clk reset rst
  in op:u2 in address:u2 in data:u16 in mask:bits<2>
  out q:u16
  memory table:mem<u16,4> {{ read_latency 1 collision {collision} }}
  rule full when op == 1 {{ table.write(address,data) }}
  rule masked_collision when op == 2 {{
    table.read(address)
    table.write(address,data,mask)
  }}
  rule fetch when op == 3 {{ table.read(address) }}
  priority full > masked_collision
  priority masked_collision > fetch
  q=table.read_data
}}
"""


def global_source() -> str:
    return """
module MaskedGlobalMemory {
  clock clk reset rst
  in address:u2 in write_enable:bit in data:s16 in mask:bits<2>
  out q:s16
  memory table:mem<s16,4> { read_latency 1 collision write_first }
  table.read_address=address
  table.write_enable=write_enable
  table.write_address=address
  table.write_data=data
  table.write_mask=mask
  q=table.read_data
}
"""


def aggregate_global_source(type_: str, collision: str = "write_first") -> str:
    return f"""
module MaskedAggregateMemory {{
  clock clk reset rst
  in write_enable:bit in data:{type_} in mask:bits<2>
  out q:{type_}
  memory table:mem<{type_},2> {{ read_latency 1 collision {collision} }}
  table.read_address=0
  table.write_enable=write_enable
  table.write_address=0
  table.write_data=data
  table.write_mask=mask
  q=table.read_data
}}
"""


def arbitrary_width_source(width: int, *, scheduled: bool) -> str:
    mask_width = memory_byte_mask_width(width)
    controls = (
        "rule access when op == 1 { table.read(address) "
        "table.write(address,data,mask) }"
        if scheduled
        else "table.read_address=address table.write_enable=op == 1 "
        "table.write_address=address table.write_data=data "
        "table.write_mask=mask"
    )
    return f"""
module ArbitraryWidthMemory {{
  clock clk reset rst
  in op:u1 in address:u2 in data:bits<{width}> in mask:bits<{mask_width}>
  out q:bits<{width}>
  memory table:mem<bits<{width}>,4> {{ read_latency 1 collision write_first }}
  {controls}
  q=table.read_data
}}
"""


def scheduled_partial_width_with_full_write_source(width: int) -> str:
    mask_width = memory_byte_mask_width(width)
    return f"""
module ScheduledPartialWidthWithFullWrite {{
  clock clk reset rst
  in op:u2 in address:u2 in data:bits<{width}> in mask:bits<{mask_width}>
  out q:bits<{width}>
  memory table:mem<bits<{width}>,4> {{ read_latency 1 collision write_first }}
  rule full when op == 1 {{ table.read(address) table.write(address,data) }}
  rule masked when op == 2 {{
    table.read(address)
    table.write(address,data,mask)
  }}
  priority full > masked
  q=table.read_data
}}
"""


def test_legacy_memory_positional_constructor_keeps_source_origin_position() -> None:
    origin = object()
    memory = Memory(
        "table", "memory:table", UIntType(8), 4, 1,
        MemoryCollision.READ_FIRST,
        expr.Constant(0, UIntType(2)), expr.Constant(0, UIntType(1)),
        expr.Constant(0, UIntType(2)), expr.Constant(0, UIntType(8)), origin,
    )
    assert memory.source_origin is origin
    assert memory.write_mask_width is None
    assert memory.write_mask is None
    assert memory.contents_reset is MemoryResetPolicy.CLEAR
    assert memory.read_data_reset is MemoryResetPolicy.CLEAR
    canonical = CanonicalMemory(
        "table", "memory:table", UIntType(8), 4, 1,
        MemoryCollision.READ_FIRST, 0, 1, 2, 3, origin,
    )
    assert canonical.source_origin is origin
    assert canonical.write_mask_width is None
    assert canonical.write_mask is None
    assert canonical.contents_reset is MemoryResetPolicy.CLEAR
    assert canonical.read_data_reset is MemoryResetPolicy.CLEAR


def test_scheduled_mask_metadata_and_two_argument_compatibility() -> None:
    module = analyze(parse(scheduled_source()))
    memory = module.memories[0]
    assert memory.scheduled
    assert memory.write_mask_width == 2
    assert memory.write_mask is None
    writes = [
        action
        for group in module.resolved_transition.action_groups
        for action in group.actions
        if action.kind is StateActionKind.MEMORY_WRITE
    ]
    assert len(writes) == 2
    assert all(len(action.operands) == 3 for action in writes)
    assert all(action.operands[2].type == BitsType(2) for action in writes)
    assert writes[0].operands[2].value == 0b11


def test_arbitrary_width_memory_uses_one_mask_bit_per_partial_or_full_byte() -> None:
    for width, expected_mask_width in ((1, 1), (7, 1), (9, 2), (13, 2)):
        assert memory_byte_mask_width(width) == expected_mask_width
        for scheduled in (False, True):
            module = analyze(parse(arbitrary_width_source(width, scheduled=scheduled)))
            memory = module.memories[0]
            assert memory.write_mask_width == expected_mask_width
            if scheduled:
                write = next(
                    action
                    for group in module.resolved_transition.action_groups
                    for action in group.actions
                    if action.kind is StateActionKind.MEMORY_WRITE
                )
                assert write.operands[2].type == BitsType(expected_mask_width)
            else:
                assert memory.write_mask is not None
                assert memory.write_mask.type == BitsType(expected_mask_width)
            assert restore(lower(module)) == module


def test_partial_width_two_operand_scheduled_write_gets_full_lane_mask() -> None:
    for width in (1, 7, 9, 13):
        mask_width = memory_byte_mask_width(width)
        module = analyze(
            parse(scheduled_partial_width_with_full_write_source(width))
        )
        writes = [
            action
            for group in module.resolved_transition.action_groups
            for action in group.actions
            if action.kind is StateActionKind.MEMORY_WRITE
        ]
        assert len(writes) == 2
        assert all(action.operands[2].type == BitsType(mask_width) for action in writes)
        assert writes[0].operands[2] == expr.Constant(
            (1 << mask_width) - 1,
            BitsType(mask_width),
        )
        assert restore(lower(module)) == module


def test_partial_msb_byte_mask_is_exact_for_global_and_scheduled_memories() -> None:
    for width in (1, 7, 9, 13):
        mask_width = memory_byte_mask_width(width)
        full_value = (1 << width) - 1
        expected_after_low_lane_clear = full_value & ~0xFF
        for scheduled in (False, True):
            module = analyze(parse(arbitrary_width_source(width, scheduled=scheduled)))
            trace = simulate_cycles(
                module,
                [
                    {"op": 0, "address": 1, "data": 0, "mask": 0},
                    {
                        "op": 1,
                        "address": 1,
                        "data": full_value,
                        "mask": (1 << mask_width) - 1,
                    },
                    {"op": 1, "address": 1, "data": 0, "mask": 1},
                    {"op": 0, "address": 1, "data": 0, "mask": 0},
                ],
                reset=[True, False, False, False],
            )
            assert trace[-1]["q"] == expected_after_low_lane_clear


def test_partial_msb_byte_mask_rebuilds_a_thirteen_bit_struct() -> None:
    for scheduled in (False, True):
        controls = (
            "rule access when enable { table.read(0) table.write(0,data,mask) }"
            if scheduled
            else "table.read_address=0 table.write_enable=enable "
            "table.write_address=0 table.write_data=data table.write_mask=mask"
        )
        module = analyze(parse(f"""
struct Packed13 {{ upper:bits<5> lower:u8 }}
module AggregatePartialLane {{
  clock clk reset rst
  in enable:bit in data:Packed13 in mask:bits<2>
  out q:Packed13
  memory table:mem<Packed13,2> {{ read_latency 1 collision write_first }}
  {controls}
  q=table.read_data
}}
"""))
        trace = simulate_cycles(
            module,
            [
                {"enable": 0, "data": {"upper": 0, "lower": 0}, "mask": 0},
                {
                    "enable": 1,
                    "data": {"upper": 0x12, "lower": 0x34},
                    "mask": 0b11,
                },
                {
                    "enable": 1,
                    "data": {"upper": 0x1F, "lower": 0xAB},
                    "mask": 0b01,
                },
                {
                    "enable": 1,
                    "data": {"upper": 0x1F, "lower": 0},
                    "mask": 0b10,
                },
                {"enable": 0, "data": {"upper": 0, "lower": 0}, "mask": 0},
            ],
            reset=[True, False, False, False, False],
        )
        assert trace[-2]["q"] == {"upper": 0x12, "lower": 0xAB}
        assert trace[-1]["q"] == {"upper": 0x1F, "lower": 0xAB}
        assert restore(lower(module)) == module


def test_masked_collision_uses_the_post_mask_word_and_lsb_lane_zero() -> None:
    inputs = [
        {"op": 1, "address": 1, "data": 0x1234, "mask": 0},
        {"op": 2, "address": 1, "data": 0xABCD, "mask": 0b01},
        {"op": 0, "address": 0, "data": 0, "mask": 0},
        {"op": 2, "address": 1, "data": 0xEE00, "mask": 0b10},
        {"op": 0, "address": 0, "data": 0, "mask": 0},
        {"op": 2, "address": 1, "data": 0, "mask": 0},
        {"op": 0, "address": 0, "data": 0, "mask": 0},
    ]
    write_first = simulate_cycles(analyze(parse(scheduled_source())), inputs)
    read_first = simulate_cycles(
        analyze(parse(scheduled_source("read_first"))), inputs
    )
    assert [item["q"] for item in write_first] == [
        0, 0, 0x12CD, 0x12CD, 0xEECD, 0xEECD, 0xEECD
    ]
    assert read_first[2]["q"] == 0x1234
    assert read_first[4]["q"] == 0x12CD
    assert read_first[6]["q"] == 0xEECD


def test_global_mask_supports_signed_raw_representation_and_reset() -> None:
    module = analyze(parse(global_source()))
    memory = module.memories[0]
    assert not memory.scheduled
    assert memory.write_mask_width == 2
    assert memory.write_mask.type == BitsType(2)
    trace = simulate_cycles(
        module,
        [
            {"address": 1, "write_enable": 1, "data": -2, "mask": 3},
            {"address": 1, "write_enable": 1, "data": 0x1234, "mask": 1},
            {"address": 1, "write_enable": 0, "data": 0, "mask": 0},
            {"address": 1, "write_enable": 0, "data": 0, "mask": 0},
        ],
        [False, False, False, True],
    )
    assert trace[2]["q"] == -204
    assert trace[3]["q"] == 0


@pytest.mark.parametrize(
    (
        "type_", "collision", "zero", "first", "low_lane", "high_lane",
        "expected",
    ),
    (
        (
            "string<2>", "write_first",
            [0, 0],
            [ord("A"), ord("B")],
            [ord("X"), ord("Y")],
            [ord("M"), ord("N")],
            [[0, 0], [0, 0], [ord("A"), ord("B")],
             [ord("A"), ord("Y")], [ord("M"), ord("Y")],
             [ord("M"), ord("Y")]],
        ),
        (
            "string<2>", "read_first",
            [0, 0],
            [ord("A"), ord("B")],
            [ord("X"), ord("Y")],
            [ord("M"), ord("N")],
            [[0, 0], [0, 0], [0, 0], [ord("A"), ord("B")],
             [ord("A"), ord("Y")], [ord("M"), ord("Y")]],
        ),
        (
            "(u8,u8)", "write_first",
            (0, 0),
            (0x12, 0x34),
            (0xAB, 0xCD),
            (0xEE, 0xFF),
            [(0, 0), (0, 0), (0x12, 0x34),
             (0x12, 0xCD), (0xEE, 0xCD), (0xEE, 0xCD)],
        ),
        (
            "(u8,u8)", "read_first",
            (0, 0),
            (0x12, 0x34),
            (0xAB, 0xCD),
            (0xEE, 0xFF),
            [(0, 0), (0, 0), (0, 0), (0x12, 0x34),
             (0x12, 0xCD), (0xEE, 0xCD)],
        ),
    ),
)
def test_masked_aggregate_memory_merges_raw_lanes_then_rebuilds_exact_type(
    type_: str,
    collision: str,
    zero: object,
    first: object,
    low_lane: object,
    high_lane: object,
    expected: list[object],
) -> None:
    module = analyze(parse(aggregate_global_source(type_, collision)))
    trace = simulate_cycles(
        module,
        [
            {"write_enable": 0, "data": zero, "mask": 0},
            {"write_enable": 1, "data": first, "mask": 0b11},
            {"write_enable": 1, "data": low_lane, "mask": 0b01},
            {"write_enable": 1, "data": high_lane, "mask": 0b10},
            {"write_enable": 0, "data": zero, "mask": 0},
            {"write_enable": 0, "data": zero, "mask": 0},
        ],
        reset=[True, False, False, False, False, False],
    )
    assert [item["q"] for item in trace] == expected
    assert restore(lower(module)) == module


@pytest.mark.parametrize(
    ("source", "message"),
    (
        (
            scheduled_source().replace("mask:bits<2>", "mask:u2"),
            "write mask has type u2, expected bits<2>",
        ),
        (
            arbitrary_width_source(9, scheduled=True).replace(
                "mask:bits<2>", "mask:bits<1>"
            ),
            "write mask has type bits<1>, expected bits<2>",
        ),
        (
            global_source().replace("mask:bits<2>", "mask:bits<1>"),
            "write_mask.*type bits<1>, expected bits<2>",
        ),
    ),
)
def test_invalid_mask_shapes_are_diagnostics(source: str, message: str) -> None:
    with pytest.raises(SemanticError, match=message):
        analyze(parse(source))


def test_masked_memory_canonical_round_trip_and_validation() -> None:
    module = analyze(parse(scheduled_source()))
    canonical = lower(module)
    assert restore(canonical) == module
    memory = canonical.memories[0]
    with pytest.raises(CanonicalizationError, match="write-mask width"):
        restore(replace(canonical, memories=(replace(memory, write_mask_width=1),)))

    partial = lower(
        analyze(parse(arbitrary_width_source(13, scheduled=True)))
    )
    with pytest.raises(CanonicalizationError, match="write-mask width"):
        restore(
            replace(
                partial,
                memories=(replace(partial.memories[0], write_mask_width=1),),
            )
        )

    transition = canonical.resolved_transition
    write_group = next(
        group for group in transition.action_groups
        if any(action.kind is StateActionKind.MEMORY_WRITE for action in group.actions)
    )
    write = next(
        action for action in write_group.actions
        if action.kind is StateActionKind.MEMORY_WRITE
    )
    malformed = replace(write, operands=write.operands[:2])
    malformed_group = replace(
        write_group,
        actions=tuple(malformed if item is write else item for item in write_group.actions),
    )
    malformed_transition = replace(
        transition,
        action_groups=tuple(
            malformed_group if item is write_group else item
            for item in transition.action_groups
        ),
    )
    with pytest.raises(CanonicalizationError, match="incorrect operands"):
        restore(replace(canonical, resolved_transition=malformed_transition))

    wrong_mask = replace(write, operands=(*write.operands[:2], write.operands[1]))
    wrong_mask_group = replace(
        write_group,
        actions=tuple(
            wrong_mask if item is write else item for item in write_group.actions
        ),
    )
    wrong_mask_transition = replace(
        transition,
        action_groups=tuple(
            wrong_mask_group if item is write_group else item
            for item in transition.action_groups
        ),
    )
    with pytest.raises(CanonicalizationError, match="incorrect mask type"):
        restore(replace(canonical, resolved_transition=wrong_mask_transition))

    global_canonical = lower(analyze(parse(global_source())))
    global_memory = global_canonical.memories[0]
    with pytest.raises(CanonicalizationError, match="expression has incorrect type"):
        restore(
            replace(
                global_canonical,
                memories=(
                    replace(global_memory, write_mask=global_memory.write_data),
                ),
            )
        )


def test_target_memory_mapper_rejects_masks_without_fabricating_capability() -> None:
    with pytest.raises(TargetArchitectureError, match="does not support byte write masks"):
        compile_source(
            global_source(),
            target="xc7z030ffg676-1",
            architecture="Xilinx7BRAM36SimpleDualPort",
            architecture_mode="required",
        )


def test_masked_memory_does_not_fabricate_a_formal_property_family() -> None:
    result = compile_source(scheduled_source())
    assert not any(
        (property_.generated_from or "").startswith("memory:")
        for property_ in result.formal_design.properties
    )
