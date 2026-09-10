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


class ArchitectureVerilatorTests(unittest.TestCase):
    @unittest.skipUnless(
        CLASH_EXECUTABLE and VERILATOR,
        "Clash and Verilator are required",
    )
    def test_selected_folded_lane_topology_runs_in_verilator(self) -> None:
        compilation = compile_source(
            (ROOT / "examples/implementation_intent.zhl").read_text(),
            top="FirArchitecture",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = generate_verilog(
                compilation.clash,
                compilation.ir.name,
                root / "rtl",
                CLASH_EXECUTABLE,
            )
            harness = root / "fir_architecture_test.cpp"
            harness.write_text(
                "#include \"VFirArchitecture.h\"\n"
                "int main() {\n"
                "  VFirArchitecture dut;\n"
                "  dut.samples = 0x01020304;\n"
                "  dut.coefficients = 0x05060708; dut.eval();\n"
                "  if (dut.y != 70) return 1;\n"
                "  dut.samples = 0xffffffff;\n"
                "  dut.coefficients = 0xffffffff; dut.eval();\n"
                "  return dut.y == 260100 ? 0 : 2;\n"
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
                    "FirArchitecture",
                    "--Mdir",
                    str(object_directory),
                    "-o",
                    "fir_architecture_sim",
                    *(str(path) for path in files),
                    str(harness),
                ],
                check=True,
                cwd=ROOT,
                env=environment,
            )
            subprocess.run(
                [str(object_directory / "fir_architecture_sim")],
                check=True,
            )


if __name__ == "__main__":
    unittest.main()
