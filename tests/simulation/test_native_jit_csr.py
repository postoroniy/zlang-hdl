"""CSR semantics are erased before the primitive simulation boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

import zlang
from zlang.compiler import compile_source
from zlang.simulate import simulate_csr_cycles


ROOT = Path(__file__).resolve().parents[2]
CONTROL = 0x5000_0000
STATUS = 0x5000_0004


def _source(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / f"{name}.zhl"
    path.write_text(text, encoding="utf-8")
    return path


def _persistent_trace(
    source: str | Path,
    *,
    top: str,
    engine: str,
    cycles: list[dict[str, int]],
) -> list[dict[str, object]]:
    with zlang.sim.load(source, top=top, engine=engine) as instance:
        trace = []
        for inputs in cycles:
            for name, value in inputs.items():
                instance.set(name, value)
            trace.append(instance.eval())
            instance.edge("clk")
        return trace


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_csr_access_policies_match_the_independent_oracle(engine: str) -> None:
    path = ROOT / "examples/control_csr.zhl"
    cycles = [
        {"addr": 0x4000_0004, "write": 0, "wdata": 0, "read": 1},
        {"addr": 0x4000_0000, "write": 1, "wdata": 0xBB, "read": 0},
        {"addr": 0x4000_0000, "write": 0, "wdata": 0, "read": 1},
        {"addr": 0x4000_0004, "write": 1, "wdata": 2, "read": 0},
        {"addr": 0x4000_0004, "write": 0, "wdata": 0, "read": 1},
        {"addr": 0x4000_0008, "write": 0, "wdata": 0, "read": 1},
    ]
    expected = simulate_csr_cycles(compile_source(path.read_text()).ir, cycles)
    actual = _persistent_trace(
        path,
        top="ControlCsr",
        engine=engine,
        cycles=cycles,
    )

    assert [
        (item["rdata"], item["ready"]) for item in actual
    ] == [
        (item["rdata"], item["ready"]) for item in expected
    ]
    assert all(
        not name.startswith("csr_field_")
        for item in actual
        for name in item
    )


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_csr_hardware_bindings_match_the_independent_oracle(engine: str) -> None:
    path = ROOT / "examples/engine_csr.zhl"
    cycles = [
        {
            "addr": STATUS,
            "write": 0,
            "wdata": 0,
            "read": 1,
            "engine_busy": 1,
            "engine_error": 0,
        },
        {
            "addr": CONTROL,
            "write": 1,
            "wdata": 1,
            "read": 0,
            "engine_busy": 0,
            "engine_error": 0,
        },
        {
            "addr": CONTROL,
            "write": 0,
            "wdata": 0,
            "read": 1,
            "engine_busy": 0,
            "engine_error": 0,
        },
        {
            "addr": 0,
            "write": 0,
            "wdata": 0,
            "read": 0,
            "engine_busy": 0,
            "engine_error": 1,
        },
        {
            "addr": STATUS,
            "write": 0,
            "wdata": 0,
            "read": 1,
            "engine_busy": 0,
            "engine_error": 0,
        },
    ]
    expected = simulate_csr_cycles(compile_source(path.read_text()).ir, cycles)
    actual = _persistent_trace(
        path,
        top="EngineCsr",
        engine=engine,
        cycles=cycles,
    )

    public = ("rdata", "ready", "engine_start")
    assert [tuple(item[name] for name in public) for item in actual] == [
        tuple(item[name] for name in public) for item in expected
    ]


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_software_priority_clear_wins_without_runtime_csr_semantics(
    engine: str,
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "software_wins",
        "module SoftwareWins { clock clk reset rst in event:bit "
        "csr x @0 { R @0 { error bit w1c <- sticky(event) "
        "priority software } } }",
    )
    cycles = [
        {"addr": 0, "write": 0, "wdata": 0, "read": 0, "event": 1},
        {"addr": 0, "write": 1, "wdata": 1, "read": 0, "event": 1},
        {"addr": 0, "write": 0, "wdata": 0, "read": 1, "event": 0},
    ]

    trace = _persistent_trace(
        source,
        top="SoftwareWins",
        engine=engine,
        cycles=cycles,
    )
    assert trace[-1] == {"rdata": 0, "ready": 1}


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_csr_and_user_state_share_one_atomic_edge_program(
    engine: str,
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "csr_counter",
        """
        module CsrCounter {
          clock clk reset rst in tick:bit out count:u8 out command:bit
          reg counter:u8=0
          rule increment when tick { counter <- truncate<8>(counter + 1) }
          count=counter
          csr control @0 { CONTROL @0 {
            enable bit @0 rw = 0
            start bit @1 pulse -> command
            reserved bits<30> @31:2 reserved
          } }
        }
        """,
    )
    cycles = [
        {"tick": 1, "addr": 0, "write": 0, "wdata": 0, "read": 0},
        {"tick": 1, "addr": 0, "write": 1, "wdata": 3, "read": 0},
        {"tick": 0, "addr": 0, "write": 0, "wdata": 0, "read": 0},
        {"tick": 0, "addr": 0, "write": 0, "wdata": 0, "read": 1},
    ]

    assert _persistent_trace(
        source,
        top="CsrCounter",
        engine=engine,
        cycles=cycles,
    ) == [
        {"command": 0, "count": 0, "rdata": 0, "ready": 0},
        {"command": 0, "count": 1, "rdata": 0, "ready": 1},
        {"command": 1, "count": 2, "rdata": 0, "ready": 0},
        {"command": 0, "count": 2, "rdata": 1, "ready": 1},
    ]


def test_csr_plan_is_deterministic_and_contains_only_primitive_state() -> None:
    path = ROOT / "examples/engine_csr.zhl"
    first = zlang.sim.compile(path, top="EngineCsr", engine="reference").plan
    second = zlang.sim.compile(path, top="EngineCsr", engine="reference").plan

    assert first.to_bytes() == second.to_bytes()
    assert set(first.payload) == {
        "schema",
        "runtime_abi",
        "packing_layout_schema",
        "canonical_ir_identity",
        "native_target",
        "module",
        "identity",
        "ports",
        "nodes",
        "regions",
        "outputs",
        "registers",
        "memories",
        "events",
        "domains",
        "edge_programs",
    }
    assert "csr_blocks" not in first.to_json()
    assert "csr_access" not in first.to_json()
    assert {item["op"] for item in first.payload["nodes"]} <= {
        "constant",
        "load_input",
        "load_state",
        "load_event",
        "load_memory",
        "add",
        "sub",
        "mul",
        "and",
        "or",
        "xor",
        "not",
        "shl",
        "lshr",
        "ashr",
        "eq",
        "ult",
        "ule",
        "slt",
        "sle",
        "select",
        "extract_bits",
        "insert_bits",
        "concat_bits",
    }


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_csr_reset_and_default_trace_keep_internal_abi_private(
    engine: str,
) -> None:
    path = ROOT / "examples/engine_csr.zhl"
    with zlang.sim.load(path, top="EngineCsr", engine=engine) as instance:
        instance.enable_trace()
        instance.set("addr", CONTROL)
        instance.set("write", 1)
        instance.set("wdata", 1)
        instance.edge("clk")
        assert instance.get("engine_start") == 1

        instance.reset("rst", asserted=True)
        assert instance.edge("clk")["engine_start"] == 0
        instance.reset("rst", asserted=False)
        trace = instance.drain_trace()

    assert trace
    assert all(
        not name.startswith("csr_field_")
        for event in trace
        for name in event
    )


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_child_csr_is_flattened_before_the_primitive_plan(
    engine: str,
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "csr_hierarchy",
        """
        module CsrLeaf {
          clock clk reset rst
          csr control @0 { CONTROL @0 {
            enable bit @0 rw = 0
            reserved bits<31> @31:1 reserved
          } }
        }
        module CsrTop {
          clock clk reset rst
          in addr:u32 in write:bit in wdata:u32 in read:bit
          out rdata:u32 out ready:bit
          inst bank:CsrLeaf
          bank.addr=addr
          bank.write=write
          bank.wdata=wdata
          bank.read=read
          rdata=bank.rdata
          ready=bank.ready
        }
        """,
    )
    cycles = [
        {"addr": 0, "write": 1, "wdata": 1, "read": 0},
        {"addr": 0, "write": 0, "wdata": 0, "read": 1},
    ]

    with zlang.sim.load(source, top="CsrTop", engine=engine) as instance:
        trace = []
        for values in cycles:
            for name, value in values.items():
                instance.set(name, value)
            trace.append(instance.eval())
            instance.edge("clk")
        assert trace == [
            {"rdata": 0, "ready": 1},
            {"rdata": 1, "ready": 1},
        ]
        assert instance.program.plan.payload[
            "canonical_ir_identity"
        ].startswith("hierarchical:")
