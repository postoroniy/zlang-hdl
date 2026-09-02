from pathlib import Path
import os
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_target
from zlang.compiler import compile_source
from zlang.fixed_point import quantize_rational
from zlang.ir import expressions as expr


ROOT = Path(__file__).resolve().parents[2]
BASE = (ROOT / "examples/symmetric_fixed_fir.zhl").read_text()
REGISTERED = (ROOT / "examples/symmetric_fixed_fir_dsp_pipelines.zhl").read_text()
CONFIGURATIONS = (
    ("SymmetricFixedFIR", "Xilinx7SymmetricDSPCascade", 1, ()),
    ("SymmetricFixedFIRMREG", "Xilinx7SymmetricDSPCascadeMREG", 2, ("multiply",)),
    ("SymmetricFixedFIRMREGPREG", "Xilinx7SymmetricDSPCascadeMREGPREG", 3,
     ("multiply", "accumulate_output")),
    ("SymmetricFixedFIRFullyPipelined", "Xilinx7SymmetricDSPCascadeFullyPipelined", 4,
     ("input_preadd", "multiply", "accumulate_output")),
)


def _compile(top: str, architecture: str):
    source = BASE if top == "SymmetricFixedFIR" else REGISTERED
    return compile_source(
        source, top=top, target="xc7z030ffg676-1",
        architecture=architecture, architecture_mode="required",
    )


@pytest.mark.parametrize("top,architecture,latency,sites", CONFIGURATIONS)
def test_pipeline_configuration_is_selected_data_not_a_planner_branch(
    top: str, architecture: str, latency: int, sites: tuple[str, ...],
) -> None:
    result = _compile(top, architecture)
    graph = result.implementation_graph
    assert graph.latency == latency
    assert graph.active_pipeline_sites == sites
    assert graph.pipeline_configuration_identity
    rtl = emit_target(result.ir, graph, simulation_model=True)
    terminal = dict(graph.resources[-1].configuration)
    assert sum(rtl.count(f") dsp{index}_primitive (") for index in range(4)) == 4
    assert f".MREG({terminal.get('mreg', 0)})" in rtl
    expected_preg = terminal.get("terminal_preg", 0)
    assert rtl.count(f".PREG({expected_preg})") >= 1


def _oracle(samples: tuple[int, ...], coefficients: tuple[int, ...]) -> int:
    expanded = (*coefficients, *reversed(coefficients))
    return quantize_rational(
        sum(a * b for a, b in zip(samples, expanded, strict=True)),
        1 << 6, fraction=0, width=16, signed=True,
        rounding=expr.FixedRounding.NEAREST_EVEN,
        overflow=expr.FixedOverflow.SATURATE,
    )


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
@pytest.mark.parametrize("top,architecture,latency,sites", CONFIGURATIONS)
def test_each_physical_pipeline_configuration_is_bit_exact(
    tmp_path: Path, top: str, architecture: str, latency: int, sites: tuple[str, ...],
) -> None:
    result = _compile(top, architecture)
    rtl = tmp_path / f"{top}.sv"
    rtl.write_text(emit_target(result.ir, result.implementation_graph, simulation_model=True))
    vectors = (
        ((0,) * 8, (0,) * 4),
        ((1024,) * 8, (1024,) * 4),
        ((-1024, 512, -256, 128, 64, -32, 16, -8), (256, -128, 64, -32)),
        ((2047,) * 8, (2047,) * 4),
    )
    expected = tuple(_oracle(*item) for item in vectors)

    def drive(name, values):
        return "".join(
            f"{name}[{index}]=12'h{value & 0xfff:03x};"
            for index, value in enumerate(values)
        )

    checks = []
    padded = (*vectors, *((((0,) * 8), ((0,) * 4)) for _ in range(latency - 1)))
    for cycle, (samples, coefficients) in enumerate(padded):
        checks.append(
            f"{drive('samples', samples)}"
            f"{drive('coefficients', coefficients)}tick;"
        )
        index = cycle - latency + 1
        if 0 <= index < len(expected):
            value = expected[index]
            literal = f"-16'sd{-value}" if value < 0 else f"16'sd{value}"
            checks.append(
                f"if ($signed(result) !== {literal}) $fatal(1,\"vector {index}: %0d\",$signed(result));"
            )
    bench = tmp_path / "tb.sv"
    bench.write_text(
        "module tb; logic clk=0,rst=1; logic signed [11:0] samples[0:7]; "
        "logic signed [11:0] coefficients[0:3]; "
        "wire signed [15:0] result; "
        f"{top} dut(.clk,.rst,.samples,.coefficients,.result); "
        "task tick; begin #1 clk=1; #1; clk=0; #1; end endtask "
        f"initial begin tick; rst=0; {''.join(checks)} $finish; end endmodule\n"
    )
    obj = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        ("verilator", "--binary", "--timing", "-Wno-fatal", "--top-module", "tb",
         str(rtl), str(bench), "-Mdir", str(obj)),
        check=True, capture_output=True, text=True, env=environment,
    )
    subprocess.run((str(obj / "Vtb"),), check=True, capture_output=True, text=True)
