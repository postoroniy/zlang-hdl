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


class AutomaticPipelineVerilatorTests(unittest.TestCase):
    @unittest.skipUnless(
        CLASH_EXECUTABLE and VERILATOR,
        "Clash and Verilator are required",
    )
    def test_selected_pipeline_runs_with_reported_latency_in_verilator(self) -> None:
        compilation = compile_source(
            (ROOT / "examples/auto_pipeline_products.zl").read_text()
        )
        self.assertEqual(
            compilation.ir.pipeline_explorations[0].selected_candidate.latency,
            3,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = generate_verilog(
                compilation.clash,
                compilation.ir.name,
                root / "rtl",
                CLASH_EXECUTABLE,
            )
            harness = root / "auto_pipeline_test.cpp"
            harness.write_text(
                "#include \"VAutoPipelineProducts.h\"\n"
                "static void tick(VAutoPipelineProducts& d) {\n"
                "  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();\n"
                "}\n"
                "static void drive(VAutoPipelineProducts& d, int n) {\n"
                "  d.a=n+1; d.b=2; d.c=n+2; d.d=3;\n"
                "  d.e=n+3; d.f=4; d.g=n+4; d.h=5;\n"
                "}\n"
                "int main() {\n"
                "  VAutoPipelineProducts d; d.clk=0; d.rst=1; drive(d,0); tick(d);\n"
                "  if (d.y != 0) return 1;\n"
                "  d.rst=0; drive(d,0); tick(d); if (d.y != 0) return 2;\n"
                "  drive(d,1); tick(d); if (d.y != 0) return 3;\n"
                "  drive(d,2); tick(d); if (d.y != 40) return 4;\n"
                "  drive(d,3); tick(d); return d.y == 54 ? 0 : 5;\n"
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
                    "AutoPipelineProducts",
                    "--Mdir",
                    str(object_directory),
                    "-o",
                    "auto_pipeline_sim",
                    *(str(path) for path in files),
                    str(harness),
                ],
                check=True,
                cwd=ROOT,
                env=environment,
            )
            subprocess.run(
                [str(object_directory / "auto_pipeline_sim")],
                check=True,
            )


if __name__ == "__main__":
    unittest.main()
