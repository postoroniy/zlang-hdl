from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

import zlang.sim
from zlang.compiler import compile_file
from zlang.simulate import simulate_cdc_steps
from zlang.simulation_plan import SimulationPlan, SimulationPlanError
from tests.simulation.differential import run_differential


ROOT = Path(__file__).resolve().parents[2]
SOURCE = "source_clock"
DESTINATION = "destination_clock"


def _rv(payload: int = 0, valid: int = 0, ready: int = 0) -> dict[str, object]:
    return {
        "source": {"payload": payload, "valid": valid},
        "destination": {"ready": ready},
    }


def _persistent_pre_edge_trace(
    path: Path,
    top: str,
    engine: str,
    inputs: list[dict[str, object]],
    edges: list[set[str]],
    resets: list[set[str]],
) -> list[dict[str, object]]:
    instance = zlang.sim.load(path, top=top, engine=engine)
    result: list[dict[str, object]] = []
    reset_state = {SOURCE: False, DESTINATION: False}
    reset_names = {SOURCE: "source_reset", DESTINATION: "destination_reset"}
    for values, active_edges, active_resets in zip(
        inputs, edges, resets, strict=True
    ):
        for clock, reset in reset_names.items():
            asserted = clock in active_resets
            if asserted != reset_state[clock]:
                instance.reset(reset, asserted=asserted)
                reset_state[clock] = asserted
        for name, value in values.items():
            instance.set(name, value)
        observed = instance.eval()
        for name, domain in (("source", SOURCE), ("destination", DESTINATION)):
            endpoint = observed.get(name)
            if isinstance(endpoint, dict) and "transfer" in endpoint:
                endpoint["transfer"] = int(
                    bool(endpoint["transfer"]) and domain in active_edges
                )
        result.append(observed)
        if active_edges:
            instance.edge_many(sorted(active_edges))
    return result


@pytest.mark.parametrize(
    ("filename", "top", "inputs", "edges"),
    (
        (
            "cdc_level.zhl",
            "CdcLevel",
            [{"level": value} for value in (0, 1, 1, 1, 1)],
            [{SOURCE, DESTINATION}, {SOURCE}, {DESTINATION}, {DESTINATION}, set()],
        ),
        (
            "cdc_pulse.zhl",
            "CdcPulse",
            [{"pulse": value} for value in (0, 1, 0, 0, 0, 0)],
            [
                {SOURCE, DESTINATION},
                {SOURCE},
                {DESTINATION},
                {DESTINATION},
                {DESTINATION},
                set(),
            ],
        ),
        (
            "cdc_handshake.zhl",
            "CdcHandshake",
            [
                _rv(),
                _rv(42, 1),
                _rv(99, 1),
                _rv(99, 1),
                _rv(99, 1),
                _rv(99, 1, 1),
                _rv(0, 0, 1),
                _rv(0, 0, 1),
                _rv(),
            ],
            [
                {SOURCE, DESTINATION},
                {SOURCE},
                {DESTINATION},
                {DESTINATION},
                set(),
                {DESTINATION},
                {SOURCE},
                {SOURCE},
                set(),
            ],
        ),
        (
            "cdc_async_fifo.zhl",
            "CdcAsyncFifo",
            [
                _rv(),
                _rv(1, 1),
                _rv(2, 1),
                _rv(3, 1),
                _rv(4, 1),
                _rv(),
                _rv(),
                _rv(0, 0, 1),
                _rv(0, 0, 1),
                _rv(0, 0, 1),
                _rv(),
                _rv(),
                _rv(),
            ],
            [
                {SOURCE, DESTINATION},
                {SOURCE},
                {SOURCE},
                {SOURCE},
                {SOURCE},
                {DESTINATION},
                {DESTINATION},
                {DESTINATION},
                {DESTINATION},
                {SOURCE, DESTINATION},
                {SOURCE},
                {SOURCE},
                set(),
            ],
        ),
    ),
)
def test_cdc_lowering_matches_existing_oracle_and_native_runtime(
    filename: str,
    top: str,
    inputs: list[dict[str, object]],
    edges: list[set[str]],
) -> None:
    path = ROOT / "examples" / filename
    resets = [{SOURCE, DESTINATION}, *([set()] * (len(inputs) - 1))]
    expected = simulate_cdc_steps(
        compile_file(path, top=top).ir,
        inputs,
        edges,
        resets,
    )
    reference = _persistent_pre_edge_trace(
        path, top, "reference", inputs, edges, resets
    )
    native = _persistent_pre_edge_trace(path, top, "jit", inputs, edges, resets)
    assert reference == expected
    assert native == expected


def _async_memory_source(collision: str) -> str:
    return f"""
module AsyncMemory {{
  clock write_clk reset write_rst @write_clk
  clock read_clk reset read_rst @read_clk
  in write_enable:bit @write_clk
  in write_address:u2 @write_clk
  in write_data:u8 @write_clk
  in read_address:u2 @read_clk
  out read_data:u8 @read_clk
  memory table:async_mem<u8,4> {{
    write_port wr @write_clk
    read_port rd @read_clk
    read_latency 1
    collision {collision}
    reset {{ contents preserve read_data clear }}
  }}
  table.wr.enable = write_enable
  table.wr.address = write_address
  table.wr.data = write_data
  table.rd.address = read_address
  read_data = table.rd.data
}}
"""


@pytest.mark.parametrize(
    ("collision", "coincident"),
    (("old", 0), ("new", 55), ("no_change", 0)),
)
def test_async_memory_coincident_edges_are_exact_in_both_engines(
    tmp_path: Path,
    collision: str,
    coincident: int,
) -> None:
    source = tmp_path / f"async_memory_{collision}.zhl"
    source.write_text(_async_memory_source(collision))
    traces = []
    for engine in ("reference", "jit"):
        instance = zlang.sim.load(source, top="AsyncMemory", engine=engine)
        instance.reset("write_rst", asserted=True)
        instance.reset("read_rst", asserted=True)
        instance.edge_many(["write_clk", "read_clk"])
        instance.reset("write_rst", asserted=False)
        instance.reset("read_rst", asserted=False)
        instance.set("write_enable", 1)
        instance.set("write_address", 0)
        instance.set("write_data", 55)
        instance.set("read_address", 0)
        first = instance.edge_many(["write_clk", "read_clk"])["read_data"]
        instance.set("write_enable", 0)
        second = instance.edge("read_clk")["read_data"]
        traces.append((first, second))
    assert traces == [(coincident, 55), (coincident, 55)]


def test_event_load_names_only_a_declared_clock(tmp_path: Path) -> None:
    source = tmp_path / "async_memory_new.zhl"
    source.write_text(_async_memory_source("new"))
    payload = deepcopy(
        zlang.sim.compile(
            source, top="AsyncMemory", engine="reference"
        ).plan.payload
    )
    event = next(node for node in payload["nodes"] if node["op"] == "load_event")
    event["attributes"]["name"] = "not_a_clock"
    payload["identity"] = ""
    unsigned = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    payload["identity"] = hashlib.sha256(unsigned).hexdigest()
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    with pytest.raises(SimulationPlanError, match="unknown clock"):
        SimulationPlan.from_bytes(encoded)


def test_cdc_plan_contains_only_generic_primitive_operations() -> None:
    plan = zlang.sim.compile(
        ROOT / "examples" / "cdc_async_fifo.zhl",
        top="CdcAsyncFifo",
        engine="jit",
    ).plan.payload
    operations = {node["op"] for node in plan["nodes"]}
    assert not operations & {
        "sync_level",
        "pulse_toggle",
        "handshake",
        "async_fifo",
        "gray_pointer",
    }
    assert len(plan["memories"]) == 1
    assert {memory["domain"] for memory in plan["memories"]} == {SOURCE}


def test_async_fifo_matches_reference_native_and_direct_sv(tmp_path: Path) -> None:
    events = (
        {
            "set": _rv(),
            "reset": {"source_reset": True, "destination_reset": True},
            "edges": [SOURCE, DESTINATION],
        },
        {
            "set": _rv(1, 1),
            "reset": {"source_reset": False, "destination_reset": False},
            "edges": [SOURCE],
        },
        {"set": _rv(2, 1), "edges": [SOURCE]},
        {"set": _rv(), "edges": [DESTINATION]},
        {"set": _rv(), "edges": [DESTINATION]},
        {"set": _rv(0, 0, 1), "edges": [DESTINATION]},
        {"set": _rv(0, 0, 1), "edges": [SOURCE, DESTINATION]},
        {"set": _rv(), "edges": [SOURCE]},
        {"set": _rv(), "edges": [SOURCE]},
    )
    trace = run_differential(
        ROOT / "examples" / "cdc_async_fifo.zhl",
        top="CdcAsyncFifo",
        events=events,
        directory=tmp_path / "async_fifo_rtl",
    )
    assert trace.reference == trace.native == trace.direct_sv


def test_async_memory_matches_reference_native_and_direct_sv(tmp_path: Path) -> None:
    source = tmp_path / "async_memory.zhl"
    # Generic synthesizable SV intentionally promises only the structural
    # old-data model; exact ``new`` coincident behavior is covered above by
    # the two plan executors and requires an exact target binding in RTL.
    source.write_text(_async_memory_source("old"))
    events = (
        {
            "set": {
                "write_enable": 0,
                "write_address": 0,
                "write_data": 0,
                "read_address": 0,
            },
            "reset": {"write_rst": True, "read_rst": True},
            "edges": ["write_clk", "read_clk"],
        },
        {
            "set": {
                "write_enable": 1,
                "write_address": 0,
                "write_data": 55,
                "read_address": 0,
            },
            "reset": {"write_rst": False, "read_rst": False},
            "edges": ["write_clk", "read_clk"],
        },
        {"set": {"write_enable": 0}, "edges": ["read_clk"]},
    )
    trace = run_differential(
        source,
        top="AsyncMemory",
        events=events,
        directory=tmp_path / "async_memory_rtl",
    )
    assert trace.reference == trace.native == trace.direct_sv
