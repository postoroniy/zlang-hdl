"""Per-bit 0/1/U/X native simulation behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

import zlang
import zlang.simulation_logic as logic_values
from zlang.sim import LogicVector, SimulationRuntimeError
from zlang.sim_cli import main as sim_main
from zlang.simulation_primitives import int_from_limbs as _limbs_to_int


def _source(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / f"{name}.zhl"
    path.write_text(body, encoding="utf-8")
    return path


def test_python_logic_surface_is_dto_only() -> None:
    assert logic_values.__all__ == ["LogicBit", "LogicVector"]
    assert not (Path(zlang.__file__).parent / "simulation_logic_machine.py").exists()
    assert not {
        "accepted_write",
        "logic_add",
        "logic_and",
        "logic_compare",
        "logic_equal",
        "logic_mul",
        "logic_select",
        "logic_shift",
    } & vars(logic_values).keys()


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


def test_logic_memory_write_is_owned_by_native_four_state_runtime(
    tmp_path: Path,
) -> None:
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

    with zlang.sim.load(source, top="LogicMemorySync", logic_state=True) as instance:
        instance.set("write", 1)
        instance.set("address", 7)
        instance.set_logic("data", "uuuuuuuu")
        instance.edge("clk")
        value, unknown, width = instance._native.get_logic_memory("table", 7)
        assert LogicVector(width, _limbs_to_int(value), _limbs_to_int(unknown)).to_bits() == "xxxxxxxx"
        instance.edge("clk")
        value, unknown, width = instance._native.get_logic_memory("table", 7)
        assert LogicVector(width, _limbs_to_int(value), _limbs_to_int(unknown)).to_bits() == "xxxxxxxx"


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


def test_logic_plan_v12_uses_native_runtime_abi_v13() -> None:
    from zlang.simulation_plan_policy import (
        SIMULATION_PLAN_SCHEMA,
        SIMULATION_RUNTIME_ABI,
    )

    assert SIMULATION_PLAN_SCHEMA == "zlang-simulation-plan-v12"
    assert SIMULATION_RUNTIME_ABI == "zlang-native-simulation-abi-v13"
