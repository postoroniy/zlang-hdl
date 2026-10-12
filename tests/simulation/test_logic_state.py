"""Per-bit 0/1/U/X native simulation behavior."""

from __future__ import annotations

from itertools import product
from pathlib import Path
from typing import Callable

import pytest

import zlang
from zlang.sim import LogicVector, SimulationRuntimeError
from zlang.sim_cli import main as sim_main
from zlang.simulation_logic import (
    LogicBit,
    logic_and,
    logic_add,
    logic_compare,
    logic_equal,
    logic_mul,
    logic_not,
    logic_or,
    logic_select,
    logic_shift,
    logic_sub,
    logic_truthy,
    logic_xor,
)


def _source(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / f"{name}.zhl"
    path.write_text(body, encoding="utf-8")
    return path


def _concrete_values(value: LogicVector) -> tuple[int, ...]:
    unknown_bits = [
        index for index in range(value.width) if value.unknown & (1 << index)
    ]
    concrete: list[int] = []
    base = value.value & ~value.unknown
    for assignments in product((0, 1), repeat=len(unknown_bits)):
        item = base
        for index, bit_value in zip(unknown_bits, assignments, strict=True):
            item |= bit_value << index
        concrete.append(item)
    return tuple(concrete)


def _abstract_concrete_results(
    results: tuple[int, ...], width: int, unknown_kind: LogicBit
) -> LogicVector:
    known_value = 0
    unknown = 0
    unknown_value = 0
    for index in range(width):
        bits = {(result >> index) & 1 for result in results}
        if len(bits) == 1:
            known_value |= next(iter(bits)) << index
        else:
            unknown |= 1 << index
            if unknown_kind is LogicBit.UNKNOWN:
                unknown_value |= 1 << index
    return LogicVector(width, known_value | unknown_value, unknown)


def _same_unknown_kind_vectors(width: int, kind: LogicBit) -> tuple[LogicVector, ...]:
    marker = kind.value
    return tuple(
        LogicVector.parse("".join(bits), width)
        for bits in product(("0", "1", marker), repeat=width)
    )


def _assert_sound_abstract_binary(
    logic_operation: Callable[[LogicVector, LogicVector], LogicVector],
    concrete_operation: Callable[[int, int], int],
    left: LogicVector,
    right: LogicVector,
    *,
    width: int,
    unknown_kind: LogicBit,
) -> None:
    concrete = tuple(
        concrete_operation(left_value, right_value) & ((1 << width) - 1)
        for left_value in _concrete_values(left)
        for right_value in _concrete_values(right)
    )
    expected = _abstract_concrete_results(concrete, width, unknown_kind)
    actual = logic_operation(left, right)
    falsely_known = expected.unknown & ~actual.unknown
    assert falsely_known == 0, (left.to_bits(), right.to_bits(), actual, expected)
    known_mask = actual.mask & ~actual.unknown
    assert (actual.value & known_mask) == (expected.value & known_mask), (
        left.to_bits(),
        right.to_bits(),
        actual,
        expected,
    )
    if unknown_kind is LogicBit.UNINITIALIZED:
        assert actual.value & actual.unknown == 0
    else:
        assert actual.value & actual.unknown == actual.unknown


def test_controlling_values_resolve_u_and_x_per_bit(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "logic_controls",
        """
        module LogicControls {
          in value:u4
          out with_ones:u4
          out with_zeroes:u4
          with_ones = value | 15
          with_zeroes = value & 0
        }
        """,
    )
    with zlang.sim.load(source, top="LogicControls", logic_state=True) as instance:
        instance.set_logic("value", "uxux")
        outputs = instance.eval()
        assert outputs["with_ones"] == LogicVector.parse("1111", 4)
        assert outputs["with_zeroes"] == LogicVector.parse("0000", 4)


def test_one_bit_logic_truth_tables_preserve_u_x_provenance() -> None:
    bits = tuple(LogicBit)
    tables = {
        logic_and: (
            "0000",
            "01ux",
            "0uux",
            "0xxx",
        ),
        logic_or: (
            "01ux",
            "1111",
            "u1ux",
            "x1xx",
        ),
        logic_xor: (
            "01ux",
            "10ux",
            "uuux",
            "xxxx",
        ),
    }
    for operation, rows in tables.items():
        for left_index, left in enumerate(bits):
            for right_index, right in enumerate(bits):
                actual = operation(
                    LogicVector.filled(left, 1), LogicVector.filled(right, 1)
                )
                assert actual.to_bits() == rows[left_index][right_index]

    assert {
        bit: logic_not(LogicVector.filled(bit, 1)).to_bits() for bit in bits
    } == {
        LogicBit.ZERO: "1",
        LogicBit.ONE: "0",
        LogicBit.UNINITIALIZED: "u",
        LogicBit.UNKNOWN: "x",
    }
    assert {
        bit: logic_truthy(LogicVector.filled(bit, 1)).to_bits() for bit in bits
    } == {
        LogicBit.ZERO: "0",
        LogicBit.ONE: "1",
        LogicBit.UNINITIALIZED: "u",
        LogicBit.UNKNOWN: "x",
    }


@pytest.mark.parametrize("unknown_kind", [LogicBit.UNINITIALIZED, LogicBit.UNKNOWN])
def test_small_logic_arithmetic_is_sound_over_all_concretizations(
    unknown_kind: LogicBit,
) -> None:
    values = _same_unknown_kind_vectors(2, unknown_kind)
    operations = (
        (lambda left, right: logic_add(left, right, 2), lambda left, right: left + right),
        (lambda left, right: logic_sub(left, right, 2), lambda left, right: left - right),
        (lambda left, right: logic_mul(left, right, 2), lambda left, right: left * right),
        (logic_and, lambda left, right: left & right),
        (logic_or, lambda left, right: left | right),
        (logic_xor, lambda left, right: left ^ right),
    )
    for logic_operation, concrete_operation in operations:
        for left in values:
            for right in values:
                _assert_sound_abstract_binary(
                    logic_operation,
                    concrete_operation,
                    left,
                    right,
                    width=2,
                    unknown_kind=unknown_kind,
                )


@pytest.mark.parametrize("unknown_kind", [LogicBit.UNINITIALIZED, LogicBit.UNKNOWN])
def test_small_logic_comparisons_and_shifts_are_sound_over_all_concretizations(
    unknown_kind: LogicBit,
) -> None:
    values = _same_unknown_kind_vectors(2, unknown_kind)
    comparisons = (
        (logic_equal, lambda left, right: int(left == right)),
        (
            lambda left, right: logic_compare(
                left, right, signed=False, or_equal=False
            ),
            lambda left, right: int(left < right),
        ),
        (
            lambda left, right: logic_compare(
                left, right, signed=False, or_equal=True
            ),
            lambda left, right: int(left <= right),
        ),
    )
    for logic_operation, concrete_operation in comparisons:
        for left in values:
            for right in values:
                _assert_sound_abstract_binary(
                    logic_operation,
                    concrete_operation,
                    left,
                    right,
                    width=1,
                    unknown_kind=unknown_kind,
                )

    shifts = (
        (
            lambda value, amount: logic_shift(
                value, amount, 2, left=True, arithmetic=False
            ),
            lambda value, amount: value << amount,
        ),
        (
            lambda value, amount: logic_shift(
                value, amount, 2, left=False, arithmetic=False
            ),
            lambda value, amount: value >> amount,
        ),
    )
    for logic_operation, concrete_operation in shifts:
        for value in values:
            for amount in values:
                _assert_sound_abstract_binary(
                    logic_operation,
                    concrete_operation,
                    value,
                    amount,
                    width=2,
                    unknown_kind=unknown_kind,
                )


@pytest.mark.parametrize("unknown_kind", [LogicBit.UNINITIALIZED, LogicBit.UNKNOWN])
def test_small_logic_select_matches_all_concretizations(
    unknown_kind: LogicBit,
) -> None:
    conditions = _same_unknown_kind_vectors(1, unknown_kind)
    values = _same_unknown_kind_vectors(2, unknown_kind)
    for condition in conditions:
        concrete_conditions = _concrete_values(condition)
        for yes in values:
            for no in values:
                concrete = tuple(
                    yes_value if condition_value else no_value
                    for condition_value in concrete_conditions
                    for yes_value in _concrete_values(yes)
                    for no_value in _concrete_values(no)
                )
                expected = _abstract_concrete_results(concrete, 2, unknown_kind)
                assert logic_select(condition, yes, no) == expected


def test_logic_arithmetic_shift_compare_and_select_are_width_exact() -> None:
    known_three = LogicVector.parse("0011", 4)
    assert logic_add(known_three, LogicVector.parse("0101", 4), 4).to_bits() == "1000"
    assert logic_sub(known_three, LogicVector.parse("0101", 4), 4).to_bits() == "1110"
    assert logic_mul(known_three, LogicVector.parse("0010", 4), 4).to_bits() == "0110"
    assert logic_shift(
        known_three, LogicVector.parse("01", 2), 4, left=True, arithmetic=False
    ).to_bits() == "0110"
    assert logic_equal(LogicVector.parse("u0", 2), LogicVector.parse("u1", 2)).to_bits() == "0"
    assert logic_compare(
        LogicVector.parse("0011", 4), LogicVector.parse("0100", 4),
        signed=False, or_equal=False,
    ).to_bits() == "1"
    assert logic_select(
        LogicVector.parse("u", 1),
        LogicVector.parse("0u", 2),
        LogicVector.parse("0u", 2),
    ).to_bits() == "0u"


def test_known_logic_mode_matches_the_binary_native_path_exhaustively(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "known_logic_differential",
        """
        module KnownLogicDifferential {
          in a,b:u4 in shift:u2 in choose:bit
          out sum:u5=a+b
          out difference:u4=a-b
          out product:u8=a*b
          out mixed:u4=(a ^ b) | (a & b)
          out shifted_left:u4=a << shift
          out shifted_right:u4=a >> shift
          out equal:bit=a == b
          out less:bit=a < b
          out selected:u4=choose ? a : b
        }
        """,
    )
    binary_program = zlang.sim.compile(source, top="KnownLogicDifferential")
    logic_program = zlang.sim.compile(
        source, top="KnownLogicDifferential", logic_state=True
    )
    with binary_program.create() as binary, logic_program.create() as logic:
        for a in range(16):
            for b in range(16):
                inputs = {
                    "a": a,
                    "b": b,
                    "shift": (a ^ b) & 3,
                    "choose": (a + b) & 1,
                }
                for name, value in inputs.items():
                    binary.set(name, value)
                    logic.set(name, value)
                expected = binary.eval()
                actual = {
                    name: value.require_binary(name=name)
                    for name, value in logic.eval().items()
                }
                assert actual == expected


def test_unknown_runtime_packed_selection_merges_all_legal_choices(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "logic_runtime_selection",
        """
        module LogicRuntimeSelection {
          in raw:bits<8> in bit_index:u3 in offset:u2
          out selected:bit out window:bits<3>
          selected=raw[bit_index]
          window=raw[offset +: 3]
        }
        """,
    )
    with zlang.sim.load(
        source, top="LogicRuntimeSelection", logic_state=True
    ) as instance:
        instance.set("raw", 0xFF)
        instance.set_logic("bit_index", "uuu")
        instance.set_logic("offset", "uu")
        assert instance.eval() == {
            "selected": LogicVector.parse("1", 1),
            "window": LogicVector.parse("111", 3),
        }

        instance.set("raw", 0x96)
        assert instance.eval() == {
            "selected": LogicVector.parse("u", 1),
            "window": LogicVector.parse("uuu", 3),
        }

        instance.set_logic("bit_index", "xxx")
        instance.set_logic("offset", "xx")
        assert instance.eval() == {
            "selected": LogicVector.parse("x", 1),
            "window": LogicVector.parse("xxx", 3),
        }


def test_uninitialized_hold_then_accepted_write_becomes_x(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "logic_register",
        """
        module LogicRegister {
          clock clk reset rst
          in load:bit in value:u4 out y:u4
          reg q:u4
          when load { q <- value }
          y=q
        }
        """,
    )
    with zlang.sim.load(source, top="LogicRegister", logic_state=True) as instance:
        assert instance.get_logic("y").to_bits() == "uuuu"
        instance.set("load", 0)
        instance.edge("clk")
        assert instance.get_logic("q").to_bits() == "uuuu"
        instance.set("load", 1)
        instance.set_logic("value", "uuuu")
        instance.edge("clk")
        assert instance.get_logic("q").to_bits() == "xxxx"


def test_explicit_self_write_refreshes_u_to_x(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "self_refresh",
        """
        module SelfRefresh {
          clock clk reset rst out y:u4 reg q:u4
          q <- q
          y=q
        }
        """,
    )
    with zlang.sim.load(source, top="SelfRefresh", logic_state=True) as instance:
        assert instance.get_logic("q").to_bits() == "uuuu"
        instance.edge("clk")
        assert instance.get_logic("q").to_bits() == "xxxx"


def test_unknown_stateful_control_fails_without_committing(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "unknown_control",
        """
        module UnknownControl {
          clock clk reset rst
          in load:bit in value:u4 out y:u4
          reg q:u4
          when load { q <- value }
          y=q
        }
        """,
    )
    with zlang.sim.load(source, top="UnknownControl", logic_state=True) as instance:
        instance.enable_trace(("q",))
        instance.drain_logic_trace()
        instance.set_logic("load", "x")
        instance.set("value", 7)
        with pytest.raises(SimulationRuntimeError, match="write condition is X"):
            instance.edge("clk")
        assert instance.get_logic("q").to_bits() == "uuuu"
        assert instance.drain_logic_trace() == []
        instance.set("load", 1)
        instance.edge("clk")
        assert instance.get_logic("q").to_bits() == "0111"


def test_logic_memory_sync_writes_only_changed_cell(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "logic_memory_sync",
        """
        module LogicMemorySync {
          clock clk reset rst
          in write:bit in address:u10 in data:u8 out y:u8
          memory table:mem<u8,1024> { read_latency 1 collision write_first }
          rule store when write { table.write(address,data) }
          y=0
        }
        """,
    )

    class RecordingNative:
        def __init__(self, wrapped: object) -> None:
            self.wrapped = wrapped
            self.batches: list[list[tuple[object, ...]]] = []

        def __getattr__(self, name: str) -> object:
            return getattr(self.wrapped, name)

        def write_state_batch(self, edits: list[tuple[object, ...]]) -> None:
            self.batches.append(edits)
            self.wrapped.write_state_batch(edits)  # type: ignore[attr-defined]

    with zlang.sim.load(source, top="LogicMemorySync", logic_state=True) as instance:
        native = RecordingNative(instance._native)
        instance._native = native
        instance.set("write", 1)
        instance.set("address", 7)
        instance.set_logic("data", "uuuuuuuu")
        instance.edge("clk")
        memory_edits = [
            edit
            for batch in native.batches
            for edit in batch
            if edit[0] == "memory"
        ]
        assert len(memory_edits) == 1
        assert memory_edits[0][:3] == ("memory", "table", 7)
        native.batches.clear()
        instance.edge("clk")
        repeated_edits = [
            edit
            for batch in native.batches
            for edit in batch
            if edit[0] == "memory"
        ]
        assert len(repeated_edits) == 1
        assert repeated_edits[0][:3] == ("memory", "table", 7)


def test_native_edge_failure_rolls_back_logic_state_and_trace(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "logic_edge_rollback",
        """
        module LogicEdgeRollback {
          clock clk reset rst in value:u4 out y:u4 reg q:u4
          q <- value
          y=q
        }
        """,
    )

    class FailingEdgeNative:
        def __init__(self, wrapped: object) -> None:
            self.wrapped = wrapped

        def __getattr__(self, name: str) -> object:
            return getattr(self.wrapped, name)

        def edge(self, clock: str) -> None:
            raise RuntimeError(f"injected native edge failure on {clock}")

    with zlang.sim.load(source, top="LogicEdgeRollback", logic_state=True) as instance:
        instance.set("value", 9)
        instance.enable_trace(("q",))
        instance.drain_logic_trace()
        native = instance._native
        instance._native = FailingEdgeNative(native)
        with pytest.raises(SimulationRuntimeError, match="injected native edge failure"):
            instance.edge("clk")
        assert instance.get_logic("q").to_bits() == "uuuu"
        assert instance.drain_logic_trace() == []
        instance._native = native
        instance.edge("clk")
        assert instance.get_logic("q").to_bits() == "1001"


def test_initial_register_override_is_atomic_and_msb_first(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "initial_override",
        """
        module InitialOverride {
          clock clk reset rst out y:u4 reg q:u4=3 y=q
        }
        """,
    )
    program = zlang.sim.compile(source, top="InitialOverride", logic_state=True)
    with program.create(initial_registers={"InitialOverride.q": "01ux"}) as instance:
        assert instance.get_logic("q").to_bits() == "01ux"
    with pytest.raises(SimulationRuntimeError, match="unknown or non-register"):
        program.create(initial_registers={"missing": "0"})
    binary_program = zlang.sim.compile(source, top="InitialOverride")
    with pytest.raises(SimulationRuntimeError, match="requires logic_state=True"):
        binary_program.create(initial_registers={"q": "0011"})


def test_initial_register_override_resolves_hierarchical_state(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "hierarchical_initial_override",
        """
        module LogicChild {
          clock clk reset rst out y:u4 reg q:u4 y=q
        }
        module LogicParent {
          clock clk reset rst out y:u4
          inst child:LogicChild
          y=child.y
        }
        """,
    )
    program = zlang.sim.compile(source, top="LogicParent", logic_state=True)
    with program.create(initial_registers={"child.q": "10ux"}) as instance:
        assert instance.get_logic("y").to_bits() == "10ux"


def test_event_set_logic_and_selective_vcd_preserve_u_x(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "logic_trace",
        """
        module LogicTrace {
          clock clk reset rst
          in load:bit in value:u4 out y:u4 out known:u4
          reg q:u4
          when load { q <- value }
          y=q
          known=9
        }
        """,
    )
    events = tmp_path / "events.jsonl"
    events.write_text(
        '{"set":{"load":1},"set_logic":{"value":"uuuu"},"edges":["clk"]}\n',
        encoding="utf-8",
    )
    trace = tmp_path / "trace.vcd"
    assert sim_main([
        str(source), "--top", "LogicTrace", "--logic-state",
        "--events", str(events), "--trace", str(trace),
        "--trace-signal", "y", "--json",
    ]) == 0
    text = trace.read_text(encoding="utf-8")
    assert "$scope module __zlang_meta $end" in text
    assert "y_u_mask" in text
    assert "known $end" not in text
    assert " clk $end" in text
    assert " rst $end" in text
    assert "#0" in text
    assert "buuuu" not in text
    assert "bxxxx" in text
    assert "b1111" in text


def test_edge_free_logic_event_advances_trace_time_once(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "logic_eval_event",
        """
        module LogicEvalEvent { in value:u4 out y:u4 y=value }
        """,
    )
    with zlang.sim.load(source, top="LogicEvalEvent", logic_state=True) as instance:
        instance.enable_trace(("y",))
        assert instance.drain_logic_trace()[0]["$event"] == 0
        outputs = instance.run_events(({"set_logic": {"value": "u1x0"}},))
        assert outputs == [{"y": LogicVector.parse("u1x0", 4)}]
        trace = instance.drain_logic_trace()
        assert trace == [{"$event": 1, "y": LogicVector.parse("u1x0", 4)}]


def test_typed_reads_require_binary_but_logic_reads_remain_available(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "typed_read",
        """
        module TypedRead { clock clk reset rst out y:u4 reg q:u4 y=q }
        """,
    )
    with zlang.sim.load(source, top="TypedRead", logic_state=True) as instance:
        with pytest.raises(SimulationRuntimeError, match="use get_logic"):
            instance.get("y")
        assert instance.get_logic("y").to_bits() == "uuuu"


def test_logic_plan_and_runtime_use_v12() -> None:
    from zlang.simulation_plan_policy import (
        SIMULATION_PLAN_SCHEMA,
        SIMULATION_RUNTIME_ABI,
    )

    assert SIMULATION_PLAN_SCHEMA == "zlang-simulation-plan-v12"
    assert SIMULATION_RUNTIME_ABI == "zlang-native-simulation-abi-v12"
