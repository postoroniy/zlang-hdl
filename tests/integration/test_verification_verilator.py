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


class VerificationVerilatorTests(unittest.TestCase):
    @unittest.skipUnless(
        CLASH_EXECUTABLE and VERILATOR,
        "Clash and Verilator are required",
    )
    def test_assumption_reset_disable_and_guarantee_run_in_verilator(self) -> None:
        result = compile_source((ROOT / "examples/contracted_add.zhl").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rtl_files = generate_verilog(
                result.clash,
                result.ir.name,
                root / "rtl",
                CLASH_EXECUTABLE,
            )
            sva = root / "ContractedAdd.contracts.sv"
            sva.write_text(result.contracts_sva)
            harness = root / "contract_test.cpp"
            harness.write_text(
                r'''#include "VContractedAdd.h"
static void tick(VContractedAdd& d) {
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}
int main(int argc, char**) {
  VContractedAdd d;
  d.rst = 1; d.a = 15; d.b = 15; tick(d);
  d.rst = 0; d.a = argc > 1 ? 8 : 3; d.b = 4; tick(d);
  return argc > 1 ? 0 : (d.y == 7 ? 0 : 1);
}
'''
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
                    "--assert",
                    "-Wno-WIDTHTRUNC",
                    "--top-module",
                    result.ir.name,
                    "--Mdir",
                    str(object_directory),
                    "-o",
                    "contract_sim",
                    *(str(path) for path in rtl_files),
                    str(sva),
                    str(harness),
                ],
                check=True,
                cwd=ROOT,
                env=environment,
            )
            executable = object_directory / "contract_sim"
            subprocess.run([str(executable)], check=True)
            failure = subprocess.run(
                [str(executable), "violate_assumption"],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(failure.returncode, 0)
            self.assertIn(
                "operands_bounded",
                failure.stdout + failure.stderr,
            )


if __name__ == "__main__":
    unittest.main()
