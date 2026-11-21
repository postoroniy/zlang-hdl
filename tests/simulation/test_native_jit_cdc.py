from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

import zlang.sim
from zlang.compiler import compile_file
from zlang.native_simulation import simulate_cdc_steps
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
    tmp_path: Path,
) -> None:
    path = ROOT / "examples" / filename
    resets = [{SOURCE, DESTINATION}, *([set()] * (len(inputs) - 1))]
    expected = simulate_cdc_steps(
        compile_file(path, top=top).ir,
        inputs,
        edges,
        resets,
    )
    native = _persistent_pre_edge_trace(path, top, "native", inputs, edges, resets)
    assert native == expected

    if top in ("CdcLevel", "CdcPulse"):
        _check_scalar_reset_trace(path, top, tmp_path)
    elif top == "CdcHandshake":
        _check_handshake_queue(path, tmp_path / "handshake")


def _check_handshake_queue(path: Path, directory: Path, simulator: str = "iverilog") -> None:
    from zlang.simulation_compare import compare_program

    # Directed schedule: writer runs while the reader is paused, both clocks
    # run on retirement, and a second outstanding token is discarded by reset.
    # The last four columns specify accepted/retired transfers and the
    # externally observable ready/valid after that edge group.
    both = (SOURCE, DESTINATION)
    rows = (
        (both, True, 0, 0, 0, False, False, 0, 0),
        (both, False, 1, 42, 0, True, False, 0, 0),
        ((SOURCE,), False, 0, 99, 0, False, False, 0, 0),
        ((DESTINATION,), False, 0, 99, 0, False, False, 0, 0),
        ((DESTINATION,), False, 0, 99, 0, False, False, 0, 1),
        (both, False, 1, 99, 0, False, False, 0, 1),
        (both, False, 1, 99, 1, False, True, 0, 0),
        ((SOURCE,), False, 1, 99, 0, False, False, 0, 0),
        ((SOURCE,), False, 1, 99, 0, False, False, 1, 0),
        (both, False, 1, 99, 0, True, False, 0, 0),
        ((DESTINATION,), False, 0, 0, 0, False, False, 0, 0),
        ((DESTINATION,), False, 0, 0, 0, False, False, 0, 1),
        (both, True, 0, 0, 0, False, False, 0, 0),
        (both, False, 0, 0, 1, False, False, 1, 0),
    )
    events = []
    expected = []
    slot = None
    for edges, reset, valid, payload, ready, put, take, src_ready, dst_valid in rows:
        events.append({
            "edges": list(edges), "reset": {"source_reset": reset, "destination_reset": reset},
            "set": _rv(payload, valid, ready),
        })
        if reset:
            slot = None
        if take:
            assert slot is not None
            slot = None
        if put:
            assert slot is None
            slot = payload
        expected.append((src_ready, dst_valid, slot if dst_valid else None))

    comparison = compare_program(zlang.sim.compile(path, top="CdcHandshake"), events,
                                 simulator=simulator, artifact_directory=directory)
    for trace in (comparison.native_trace, comparison.rtl_trace):
        observations = [(f["source_ready"], f["destination_valid"],
                         f["destination_payload"] if f["destination_valid"] else None) for f in trace]
        assert observations == expected, "one-slot handshake queue trace"


def _check_scalar_reset_trace(path: Path, top: str, tmp_path: Path) -> None:
    from zlang.simulation_compare import compare_program

    level = top == "CdcLevel"
    source = "level" if level else "pulse"
    output = "synced" if level else "crossed_pulse"
    on = {"source_reset": True, "destination_reset": True}
    off = dict.fromkeys(on, False)
    # Event 4 masks a live bit without clocking either domain. Releasing at
    # event 5 exposes the same bit. Later edges under reset actually clear it.
    events = [
        {"reset": on, "set": {source: 0}, "edges": [SOURCE, DESTINATION]},
        {"reset": off, "set": {source: 1}, "edges": [SOURCE]},
        {"set": {source: int(level)}, "edges": [DESTINATION]},
        {"edges": [DESTINATION]},
        {"reset": on},
        {"reset": off},
        {"reset": on, "edges": [SOURCE]},
        {"edges": [DESTINATION]},
        {"reset": off},
        {"set": {source: 1}, "edges": [SOURCE, DESTINATION]},
        {"set": {source: int(level)}, "edges": [DESTINATION]},
        {"edges": [DESTINATION]},
        {"edges": [DESTINATION]},
    ]
    expected = (0, 0, 0, 1, 0, 1, 0, 0, 0, 0, int(level), 1, int(level))
    binary = zlang.sim.compile(path, top=top)
    with binary.create() as instance:
        assert tuple(frame[output] for frame in instance.run_events(events)) == expected
    compared = compare_program(binary, events, simulator="iverilog", artifact_directory=tmp_path / "reset")
    assert tuple(frame[output] for frame in compared.rtl_trace) == expected

    with zlang.sim.load(path, top=top, logic_state=True) as instance:
        for event, bit in zip(events, expected, strict=True):
            instance.run_events([event])
            assert instance.get_logic(output).to_bits() == str(bit)
        instance.set_logic(source, "X")
        instance.edge(SOURCE)
        instance.edge(DESTINATION)
        instance.edge(DESTINATION)
        assert instance.get_logic(output).to_bits() == "x"
        instance.run_events([{"reset": on}])
        assert instance.get_logic(output).to_bits() == "0"
        instance.run_events([{"reset": off}])
        assert instance.get_logic(output).to_bits() == "x"


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
def test_async_memory_coincident_edges_are_exact_in_native_engine(
    tmp_path: Path,
    collision: str,
    coincident: int,
) -> None:
    source = tmp_path / f"async_memory_{collision}.zhl"
    source.write_text(_async_memory_source(collision))
    instance = zlang.sim.load(source, top="AsyncMemory", engine="native")
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
    assert (first, second) == (coincident, 55)


def test_event_load_names_only_a_declared_clock(tmp_path: Path) -> None:
    source = tmp_path / "async_memory_new.zhl"
    source.write_text(_async_memory_source("new"))
    payload = deepcopy(
        zlang.sim.compile(
            source, top="AsyncMemory", engine="native"
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
        engine="native",
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


def test_async_fifo_matches_native_and_direct_sv(tmp_path: Path) -> None:
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
    assert trace.native == trace.direct_sv


def test_async_memory_matches_native_and_direct_sv(tmp_path: Path) -> None:
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
    assert trace.native == trace.direct_sv
