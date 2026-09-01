from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from tests.toolchain import CLASH_ENVIRONMENT, CLASH_EXECUTABLE
from zlang.compiler import compile_source
from zlang.simulate import SimulationError, simulate


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples/add.zl").read_text()


class PipelineTests(unittest.TestCase):
    def test_representative_add_values_including_carry(self) -> None:
        module = compile_source(SOURCE).ir
        vectors = [
            (0, 0, 0),
            (1, 2, 3),
            (255, 1, 256),
            (255, 255, 510),
        ]
        for a, b, expected in vectors:
            with self.subTest(a=a, b=b):
                self.assertEqual(simulate(module, a=a, b=b), {"y": expected})

    def test_out_of_range_input_is_rejected(self) -> None:
        module = compile_source(SOURCE).ir
        with self.assertRaisesRegex(SimulationError, "does not fit u8"):
            simulate(module, a=256, b=0)

    @unittest.skipUnless(CLASH_EXECUTABLE, "Clash executable is not available")
    def test_generated_clash_compiles_to_verilog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory)
            subprocess.run(
                [
                    CLASH_EXECUTABLE,
                    "--verilog",
                    str(ROOT / "examples/generated/Add.hs"),
                    "-outputdir",
                    str(output_directory),
                ],
                check=True,
                cwd=ROOT,
                env=CLASH_ENVIRONMENT,
            )
            self.assertTrue(list(output_directory.rglob("*.v")))

    @unittest.skipUnless(
        CLASH_EXECUTABLE and shutil.which("iverilog") and shutil.which("vvp"),
        "Clash and Icarus Verilog are required",
    )
    def test_generated_rtl_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory)
            subprocess.run(
                [
                    CLASH_EXECUTABLE,
                    "--verilog",
                    str(ROOT / "examples/generated/Add.hs"),
                    "-outputdir",
                    str(output_directory),
                ],
                check=True,
                cwd=ROOT,
                env=CLASH_ENVIRONMENT,
            )
            testbench = output_directory / "tb.v"
            testbench.write_text(
                textwrap.dedent(
                    """
                    module tb;
                      reg [7:0] a;
                      reg [7:0] b;
                      wire [8:0] y;
                      Add dut(.a(a), .b(b), .y(y));

                      task check;
                        input [7:0] av;
                        input [7:0] bv;
                        input [8:0] expected;
                        begin
                          a = av; b = bv; #1;
                          if (y !== expected) $fatal(1, "unexpected result");
                        end
                      endtask

                      initial begin
                        check(0, 0, 0);
                        check(1, 2, 3);
                        check(255, 1, 256);
                        check(255, 255, 510);
                        $finish;
                      end
                    endmodule
                    """
                )
            )
            executable = output_directory / "simulation"
            verilog_files = [str(path) for path in output_directory.rglob("*.v")]
            subprocess.run(
                ["iverilog", "-g2012", "-s", "tb", "-o", str(executable), *verilog_files],
                check=True,
            )
            subprocess.run(["vvp", str(executable)], check=True)


if __name__ == "__main__":
    unittest.main()
