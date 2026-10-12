"""Persistent simulation command-line surface."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import tracemalloc

import pytest

from zlang import sim
from zlang.cli import main


ROOT = Path(__file__).resolve().parents[1]


def _vcd_identifiers(text: str) -> dict[str, str]:
    return {
        line.split()[4]: line.split()[3]
        for line in text.splitlines()
        if line.startswith("$var ")
    }


def _vcd_time_blocks(text: str) -> dict[int, tuple[str, ...]]:
    result: dict[int, list[str]] = {}
    active: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("#"):
            active = result.setdefault(int(line[1:]), [])
        elif active is not None:
            active.append(line)
    return {time: tuple(lines) for time, lines in result.items()}


def test_primary_help_discovers_simulation_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--help"])

    assert raised.value.code == 0
    output = capsys.readouterr().out
    assert "zlang sim SOURCE" in output
    assert "zlang sim --help" in output
    assert "--experimental-systemverilog" not in output


def test_sim_help_exposes_native_simulation_without_engine_selector(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["sim", "--help"])

    assert raised.value.code == 0
    output = capsys.readouterr().out
    assert "--engine" not in output
    assert "--compare-with {iverilog,verilator}" in output
    assert "--events EVENTS" in output
    assert "supports bounded repeat" in " ".join(output.split())
    assert "--strict-uninitialized" in output
    assert "physical clocks/resets are always included" in " ".join(output.split())
    assert "zlang sim counter.zhl" in output


def _source(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / f"{name}.zhl"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("removed", ("reference", "python", "jit"))
def test_sim_cli_rejects_removed_engines_with_migration_diagnostic(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    removed: str,
) -> None:
    source = _source(tmp_path, "alias", "module Alias { out value:u8 value=7 }")

    with pytest.raises(SystemExit) as raised:
        main(["sim", str(source), "--engine", removed, "--json"])
    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert f"engine '{removed}' was removed" in error
    assert "--compare-with iverilog|verilator" in error


@pytest.mark.parametrize("engine", ("native",))
def test_hidden_native_selector_remains_compatible(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    engine: str,
) -> None:
    source = _source(
        tmp_path,
        "counter",
        """
        module Counter {
          clock clk reset rst in step:u8 out count:u8
          reg value:u8=0
          value <- truncate<8>(value+step)
          count=value
        }
        """,
    )

    assert main(
        [
            "sim",
            str(source),
            "--top",
            "Counter",
            "--engine",
            engine,
            "--clock",
            "clk",
            "--cycles",
            "3",
            "--set",
            "step=2",
            "--json",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {"count": 6}


def test_sim_cli_defaults_to_native_engine(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _source(
        tmp_path,
        "default_native",
        "module DefaultNative { out value:u8 value=7 }",
    )

    assert main(["sim", str(source), "--top", "DefaultNative", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"value": 7}


def test_sim_cli_strict_uninitialized_fails_on_observed_unreset_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _source(
        tmp_path,
        "strict_uninitialized",
        """
        module StrictUninitialized {
          clock clk reset rst out value:u8 reg state:u8 value=state
        }
        """,
    )

    assert main([
        "sim",
        str(source),
        "--top",
        "StrictUninitialized",
        "--strict-uninitialized",
        "--json",
    ]) == 2
    error = capsys.readouterr().err
    assert "observed 'value': value contains U (never refreshed) bits" in error
    assert "never refreshed" in error


@pytest.mark.parametrize("simulator", ("iverilog", "verilator"))
def test_sim_cli_compares_native_with_external_rtl_and_retains_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    simulator: str,
) -> None:
    if simulator == "iverilog" and (
        shutil.which("iverilog") is None or shutil.which("vvp") is None
    ):
        pytest.skip("Icarus Verilog is unavailable")
    if simulator == "verilator" and shutil.which("verilator") is None:
        pytest.skip("Verilator is unavailable")
    source = _source(
        tmp_path,
        "compare",
        "module Compare { in a,b:u8 out y:u9 y=a+b }",
    )
    evidence = tmp_path / "evidence"

    assert main(
        [
            "sim",
            str(source),
            "--top",
            "Compare",
            "--set",
            "a=200",
            "--set",
            "b=55",
            "--compare-with",
            simulator,
            "--compare-artifacts",
            str(evidence),
            "--json",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {"y": 255}
    manifest = json.loads((evidence / "comparison.json").read_text())
    assert manifest["schema"] == "zlang-simulation-comparison-v1"
    assert manifest["simulator"] == simulator
    assert manifest["native_trace"] == manifest["rtl_trace"] == [{"y": 255}]
    assert {path.name for path in evidence.iterdir()} == {
        "compare_tb.sv",
        "comparison.json",
        "compile.log",
        "design.sv",
        "run.log",
    }


def test_sim_jsonl_events_support_coincident_clocks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "set": {"level": 0},
                        "reset": {
                            "source_reset": True,
                            "destination_reset": True,
                        },
                        "edges": ["source_clock", "destination_clock"],
                    }
                ),
                json.dumps(
                    {
                        "set": {"level": 1},
                        "reset": {
                            "source_reset": False,
                            "destination_reset": False,
                        },
                        "edges": ["source_clock"],
                    }
                ),
                json.dumps({"edges": ["destination_clock"]}),
                json.dumps({"edges": ["destination_clock"]}),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    assert main(
        [
            "sim",
            str(ROOT / "examples/cdc_level.zhl"),
            "--top",
            "CdcLevel",
            "--engine",
            "native",
            "--events",
            str(events),
            "--json",
        ]
    ) == 0
    trace = json.loads(capsys.readouterr().out)
    assert trace[-1] == {"synced": 1}


def test_sim_jsonl_events_support_bounded_repeat(tmp_path: Path) -> None:
    from zlang.sim_cli import _read_events

    events = tmp_path / "events.jsonl"
    events.write_text(
        '\n'.join((
            '{"set":{"level":1},"edges":["clk"],"repeat":2}',
            '{"edges":["clk"],"repeat":3}',
        )) + '\n',
        encoding="utf-8",
    )
    schedule = _read_events(events)
    expanded = list(schedule)
    assert expanded == [
        {"set": {"level": 1}, "edges": ["clk"]},
        {"set": {"level": 1}, "edges": ["clk"]},
        {"edges": ["clk"]},
        {"edges": ["clk"]},
        {"edges": ["clk"]},
    ]
    assert all("repeat" not in event for event in expanded)
    assert len(schedule.records) == 2
    initialized = schedule.with_first_updates({"seed": 7})
    assert initialized[0]["set"] == {"seed": 7, "level": 1}
    assert initialized[1]["set"] == {"level": 1}
    assert len(initialized) == len(schedule)

    events.write_text(
        '{"edges":["clk"],"repeat":0}\n',
        encoding="utf-8",
    )
    with pytest.raises(
        sim.SimulationRuntimeError,
        match="event repeat must be a positive integer",
    ):
        _read_events(events)


def test_sim_jsonl_repeat_keeps_one_record_in_memory(tmp_path: Path) -> None:
    from zlang.sim_cli import MAX_EXPANDED_EVENTS, _read_events

    events = tmp_path / "long-idle.jsonl"
    events.write_text(
        '{"edges":["clk"],"repeat":' + str(MAX_EXPANDED_EVENTS) + '}\n',
        encoding="utf-8",
    )
    tracemalloc.start()
    schedule = _read_events(events)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(schedule) == MAX_EXPANDED_EVENTS
    assert len(schedule.records) == 1
    assert schedule[0] is schedule[-1]
    assert peak < 1_000_000

    events.write_text(
        '{"edges":["clk"],"repeat":' + str(MAX_EXPANDED_EVENTS + 1) + '}\n',
        encoding="utf-8",
    )
    with pytest.raises(
        sim.SimulationRuntimeError,
        match="expanded event schedule exceeds",
    ):
        _read_events(events)


def test_vcd_tracks_falling_edge_active_low_reset_and_repeated_events(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "falling_trace.zhl"
    source.write_text(
        "module FallingTrace { "
        "clock clk { edge falling } "
        "reset rst_n @clk { mode synchronous polarity active_low "
        "power_up unspecified } "
        "out y:u2 reg q:u2 @clk=0 y=q "
        "q <- truncate<2>(q+1) }",
        encoding="utf-8",
    )
    events = tmp_path / "events.jsonl"
    events.write_text(
        '{"reset":{"rst_n":true},"edges":["clk"],"repeat":2}\n'
        '{"reset":{"rst_n":false}}\n'
        '{"edges":["clk"]}\n',
        encoding="utf-8",
    )
    trace = tmp_path / "falling.vcd"
    assert main([
        "sim", str(source), "--top", "FallingTrace", "--events", str(events),
        "--trace", str(trace), "--json",
    ]) == 0
    json.loads(capsys.readouterr().out)
    text = trace.read_text(encoding="utf-8")
    identifiers = _vcd_identifiers(text)
    blocks = _vcd_time_blocks(text)
    clk = identifiers["clk"]
    rst_n = identifiers["rst_n"]

    assert "Event-index visualization" in text
    assert f"b1 {clk}" in blocks[0]
    assert f"b1 {rst_n}" in blocks[0]
    assert f"b0 {rst_n}" in blocks[1]
    assert f"b0 {clk}" in blocks[2]
    assert f"b1 {clk}" in blocks[3]
    assert f"b0 {clk}" in blocks[4]
    assert f"b1 {clk}" in blocks[5]
    assert f"b1 {rst_n}" in blocks[5]
    assert list(blocks) == sorted(blocks)


def test_vcd_tracks_coincident_clock_edges_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events = tmp_path / "coincident.jsonl"
    events.write_text(
        '{"set":{"control_enable":1},'
        '"reset":{"control_rst":false,"datapath_rst":false},'
        '"edges":["control_clk","datapath_clk"],"repeat":2}\n',
        encoding="utf-8",
    )
    trace = tmp_path / "coincident.vcd"
    assert main([
        "sim", str(ROOT / "examples/multi_clock_stateful.zhl"),
        "--top", "MultiClockStateful", "--events", str(events),
        "--trace", str(trace), "--json",
    ]) == 0
    json.loads(capsys.readouterr().out)
    text = trace.read_text(encoding="utf-8")
    identifiers = _vcd_identifiers(text)
    blocks = _vcd_time_blocks(text)
    clocks = (identifiers["control_clk"], identifiers["datapath_clk"])

    assert all(f"b1 {clock}" in blocks[2] for clock in clocks)
    assert all(f"b0 {clock}" in blocks[3] for clock in clocks)
    assert all(f"b1 {clock}" in blocks[4] for clock in clocks)
    assert all(f"b0 {clock}" in blocks[5] for clock in clocks)
    assert len(blocks) == len(set(blocks))


@pytest.mark.parametrize(
    "reset_declaration",
    (
        (
            "reset rst_n @clk { mode synchronous polarity active_low "
            "power_up unspecified }"
        ),
        "async reset rst_n @clk { polarity active_low }",
    ),
)
def test_vcd_reset_visibility_matches_native_event_semantics(
    reset_declaration: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "reset_trace.zhl"
    source.write_text(
        "module ResetTrace { clock clk " + reset_declaration + " "
        "out y:u2 reg q:u2 @clk=0 y=q q <- truncate<2>(q+1) }",
        encoding="utf-8",
    )
    events = tmp_path / "reset-events.jsonl"
    events.write_text(
        '{"reset":{"rst_n":false},"edges":["clk"]}\n'
        '{"reset":{"rst_n":true}}\n',
        encoding="utf-8",
    )
    trace = tmp_path / "reset.vcd"
    assert main([
        "sim", str(source), "--top", "ResetTrace", "--events", str(events),
        "--trace", str(trace), "--json",
    ]) == 0
    outputs = json.loads(capsys.readouterr().out)
    assert len(outputs) == 2
    text = trace.read_text(encoding="utf-8")
    identifier = _vcd_identifiers(text)["rst_n"]
    blocks = _vcd_time_blocks(text)
    assert f"b1 {identifier}" in blocks[0]
    assert f"b0 {identifier}" in blocks[3]


def test_sim_trace_uses_flattened_plan_widths_for_child_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    trace = tmp_path / "hierarchy.vcd"

    assert main(
        [
            "sim",
            str(ROOT / "examples/sequential_instance_array.zhl"),
            "--top",
            "StateLaneArray",
            "--engine",
            "native",
            "--clock",
            "clk",
            "--cycles",
            "2",
            "--set",
            "enables=[1,1]",
            "--set",
            "steps=[2,3]",
            "--trace",
            str(trace),
            "--json",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {"values": [4, 6]}
    text = trace.read_text(encoding="utf-8")
    assert "$var wire 8" in text
    assert "lane[0].count $end" in text
    assert "lane[1].count $end" in text
    clock_declaration = next(
        line for line in text.splitlines() if line.endswith(" clk $end")
    )
    reset_declaration = next(
        line for line in text.splitlines() if line.endswith(" rst $end")
    )
    clock_identifier = clock_declaration.split()[3]
    reset_identifier = reset_declaration.split()[3]
    assert f"b0 {clock_identifier}" in text
    assert f"b1 {clock_identifier}" in text
    assert f"b0 {reset_identifier}" in text
    lines = text.splitlines()

    def time_block(timestamp: str) -> tuple[str, ...]:
        start = lines.index(timestamp) + 1
        end = next(
            (index for index in range(start, len(lines)) if lines[index].startswith("#")),
            len(lines),
        )
        return tuple(lines[start:end])

    first_edge = time_block("#2")
    first_idle = time_block("#3")
    assert f"b1 {clock_identifier}" in first_edge
    assert f"b0 {clock_identifier}" in first_idle


def test_sim_cycles_rejects_multiclock_top(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(
        [
            "sim",
            str(ROOT / "examples/cdc_level.zhl"),
            "--top",
            "CdcLevel",
            "--engine",
            "native",
            "--clock",
            "source_clock",
            "--cycles",
            "1",
        ]
    ) == 2
    assert "--cycles is valid only for a single-clock top" in capsys.readouterr().err


def test_sim_invalid_jsonl_is_reported_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text("{bad json}\n", encoding="utf-8")

    assert main(
        [
            "sim",
            str(ROOT / "examples/cdc_level.zhl"),
            "--top",
            "CdcLevel",
            "--engine",
            "native",
            "--events",
            str(events),
        ]
    ) == 2
    assert f"{events}:1: invalid event JSON" in capsys.readouterr().err
