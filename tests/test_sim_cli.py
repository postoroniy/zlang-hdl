"""Persistent simulation command-line surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zlang.cli import main


ROOT = Path(__file__).resolve().parents[1]


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


def test_sim_help_exposes_canonical_engines_and_examples(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["sim", "--help"])

    assert raised.value.code == 0
    output = capsys.readouterr().out
    assert "--engine {native,reference}" in output
    assert "--events EVENTS" in output
    assert "zlang sim counter.zhl" in output


def _source(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / f"{name}.zhl"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("alias", "canonical"),
    (("jit", "native"), ("python", "reference")),
)
def test_sim_cli_keeps_legacy_engine_aliases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    alias: str,
    canonical: str,
) -> None:
    source = _source(tmp_path, "alias", "module Alias { out value:u8 value=7 }")
    selected: list[str] = []

    class _Instance:
        def __enter__(self) -> "_Instance":
            return self

        def __exit__(self, *_arguments: object) -> None:
            return None

        def eval(self) -> dict[str, int]:
            return {"value": 7}

    def fake_load(*_arguments: object, engine: str, **_keywords: object) -> _Instance:
        selected.append(engine)
        return _Instance()

    monkeypatch.setattr("zlang.sim_cli.sim.load", fake_load)

    assert main(["sim", str(source), "--engine", alias, "--json"]) == 0
    assert selected == [canonical]
    assert json.loads(capsys.readouterr().out) == {"value": 7}


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_sim_cycles_json_uses_one_persistent_instance(
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
            "reference",
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
            "reference",
            "--events",
            str(events),
        ]
    ) == 2
    assert f"{events}:1: invalid event JSON" in capsys.readouterr().err
