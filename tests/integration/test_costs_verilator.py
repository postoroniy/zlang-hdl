import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.compiler import compile_source
from zlang.toolchain import generate_verilog


ROOT = Path(__file__).resolve().parents[2]
VERILATOR = shutil.which("verilator")


class CostExtractionVerilatorTests(unittest.TestCase):
    @unittest.skipUnless(
        CLASH_EXECUTABLE and VERILATOR,
        "Clash and Verilator are required",
    )
    def test_estimate_selected_candidate_runs_at_declared_latency(self) -> None:
        compilation = compile_source((ROOT / "examples/cost_mac.zl").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = generate_verilog(
                compilation.clash,
                compilation.ir.name,
                root / "rtl",
                CLASH_EXECUTABLE,
            )
            harness = root / "cost_mac_test.cpp"
            harness.write_text(
                "#include \"VCostMac.h\"\n"
                "static void tick(VCostMac& d) {\n"
                "  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();\n"
                "}\n"
                "int main() {\n"
                "  VCostMac d; d.clk = 0; d.rst = 1;\n"
                "  d.a = 2; d.b = 3; d.c = 4; tick(d);\n"
                "  if (d.y != 0) return 1;\n"
                "  d.rst = 0; d.a = 5; d.b = 6; d.c = 7; d.eval();\n"
                "  if (d.y != 0) return 2; tick(d);\n"
                "  if (d.y != 37) return 3;\n"
                "  d.a = 8; d.b = 9; d.c = 10; d.eval();\n"
                "  if (d.y != 37) return 4; tick(d);\n"
                "  return d.y == 82 ? 0 : 5;\n"
                "}\n"
            )
            object_directory = root / "obj"
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            subprocess.run(
                [
                    VERILATOR,
                    "--cc",
                    "--exe",
                    "--build",
                    "--top-module",
                    "CostMac",
                    "--Mdir",
                    str(object_directory),
                    "-o",
                    "cost_mac_sim",
                    *(str(path) for path in files),
                    str(harness),
                ],
                check=True,
                cwd=ROOT,
                env=environment,
            )
            subprocess.run([str(object_directory / "cost_mac_sim")], check=True)


if __name__ == "__main__":
    unittest.main()
