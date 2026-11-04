# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path

import pytest

from zlang.sim_cli import main

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("rtl", [False, True])
def test_documented_ready_valid_example_and_typed_sidecar(tmp_path, capsys, rtl):
    output = tmp_path / "result.jsonl"
    args = [
        "examples/simulation_io.zhl",
        "--top",
        "SimulationIoExample",
        "--simulation",
        "vectors",
        "--sim-input",
        "stimulus=examples/simulation_io.vectors.jsonl",
        "--sim-output",
        f"results={output}",
        "--events",
        "examples/simulation_io.events.jsonl",
        "--json",
    ]
    if rtl:
        args.extend(
            ["--compare-with", "iverilog", "--compare-artifacts", str(tmp_path / "rtl")]
        )
    assert main(args) == 0
    records = [json.loads(s) for s in output.read_text().splitlines()]
    assert [r["event"] for r in records] == [3, 4, 8]
    metadata = json.loads(Path(str(output) + ".io.json").read_text())
    assert metadata["outputs"]["results"]["fields"]["sample"] == {
        "kind": "fixed",
        "width": 16,
        "fraction": 14,
        "overflow": "wrap",
    }
    assert metadata["outputs"]["results"]["sha256"]
    assert capsys.readouterr().err == ""


def test_channels_require_explicit_activation_and_no_console_json_collision(capsys):
    base = ["examples/simulation_io.zhl", "--sim-output", "results=-", "--json"]
    assert main(base) != 0
    assert "require" in capsys.readouterr().err
    assert main([*base, "--simulation", "vectors"]) != 0
    assert "stdout" in capsys.readouterr().err
