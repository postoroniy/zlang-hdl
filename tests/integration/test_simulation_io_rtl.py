# SPDX-License-Identifier: Apache-2.0
import json

import pytest

from tests.simulation.test_simulation_io import SOURCE
from zlang import sim
from zlang.simulation_io_compare import compare_io_program
from zlang.simulation_compare import SimulationComparisonError


@pytest.mark.parametrize("simulator", ["iverilog", "verilator"])
def test_independent_guarded_input_and_pre_edge_logs(tmp_path, simulator):
    path = tmp_path / "pilot.zhl"
    path.write_text(SOURCE)
    vectors = tmp_path / "vectors.jsonl"
    vectors.write_text('{"sample":7}\n{"sample":11}\n{"sample":255}\n')
    events = [
        {"set": {"enable": 1}, "reset": {"rst": True}, "edges": ["clk"]},
        {"reset": {"rst": False}, "edges": ["clk"]},
        {"set": {"enable": 0}, "edges": ["clk"]},
        {"edges": ["clk"]},
        {"set": {"enable": 1}, "edges": ["clk"]},
        {"edges": ["clk"]},
        {"edges": ["clk"]},
    ]
    result = compare_io_program(
        sim.compile(path, top="Pilot"),
        events,
        simulation="vectors",
        io_inputs={"stimulus": vectors},
        io_outputs={"results": tmp_path / "result.jsonl"},
        simulator=simulator,
        artifact_directory=tmp_path / "rtl",
    )
    assert result.native_trace == result.rtl_trace
    receipt = json.loads((tmp_path / "rtl/comparison.json").read_text())
    assert receipt["status"] == "passed"
    assert receipt["native_reads"] == [[1, 0], [4, 1], [5, 2]]
    assert receipt["native_records"] == receipt["rtl_records"]


@pytest.mark.parametrize(
    "type_,value,expected",
    [
        ("s8", -128, "10000000"),
        ("fixed<8,4>", -5, "11111011"),
        ("u64", (1 << 64) - 1, "1" * 64),
        ("vec<2,u8>", {"bits": "1000000011111111"}, "1000000011111111"),
    ],
)
def test_exact_type_and_width_in_actual_rtl(tmp_path, type_, value, expected):
    path = tmp_path / "typed.zhl"
    path.write_text(f"""module T {{clock clk reset rst in a:{type_} out y:{type_} y=a
simulation io @clk {{read src {{v -> a}} else {{finish}} write dst {{v=y}}}}
}}""")
    vectors = tmp_path / "v.jsonl"
    vectors.write_text(json.dumps({"v": value}) + "\n")
    compare_io_program(
        sim.compile(path),
        [{"edges": ["clk"]}, {"edges": ["clk"]}],
        simulation="io",
        io_inputs={"src": vectors},
        io_outputs={"dst": tmp_path / "out.jsonl"},
        simulator="iverilog",
        artifact_directory=tmp_path / "rtl",
    )
    assert (
        json.loads((tmp_path / "out.jsonl").read_text())["values"]["v"]["bits"]
        == expected
    )


@pytest.mark.parametrize("mutation", ["pre_edge", "cursor"])
def test_rtl_sampling_and_consumption_mutations_rejected(
    tmp_path, monkeypatch, mutation
):
    from zlang.simulation_io_rtl import SimulationIOTestbench

    path = tmp_path / "pilot.zhl"
    path.write_text(SOURCE)
    vectors = tmp_path / "v.jsonl"
    vectors.write_text('{"sample":7}\n{"sample":11}\n')
    original = SimulationIOTestbench.after_edge

    def mutated(self, index):
        lines = original(self, index)
        if mutation == "cursor":
            lines.append(f"{self.prefix}cursor=0;")
        else:
            # Actual post-edge DUT value, in place of the saved pre-edge sample.
            lines = [
                line.replace(
                    f"{self.prefix}log_0_1);",
                    f"{self.probes[id(self.writes[0].bindings[1][1])]});",
                )
                for line in lines
            ]
        return lines

    monkeypatch.setattr(SimulationIOTestbench, "after_edge", mutated)
    with pytest.raises(SimulationComparisonError, match="transcript|advancement"):
        compare_io_program(
            sim.compile(path, top="Pilot"),
            [{"set": {"enable": 1}, "edges": ["clk"]}] * 3,
            simulation="vectors",
            io_inputs={"stimulus": vectors},
            io_outputs={"results": tmp_path / "out.jsonl"},
            simulator="iverilog",
            artifact_directory=tmp_path / "rtl",
        )
    receipt = json.loads((tmp_path / "rtl/comparison.json").read_text())
    assert receipt["status"] == "failed"
