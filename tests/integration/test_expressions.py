from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from tests.toolchain import CLASH_ENVIRONMENT, CLASH_EXECUTABLE
from zlang.compiler import compile_source
from zlang.simulate import simulate


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples/alu.zhl").read_text()


class ExpressionIntegrationTests(unittest.TestCase):
    def test_alu_behavior(self) -> None:
        module = compile_source(SOURCE).ir
        vectors = [
            (7, 5, 0, 12),
            (0xFFFF_FFFF, 1, 0, 0),
            (3, 5, 1, 0xFFFF_FFFE),
            (0b1100, 0b1010, 2, 0b1000),
            (0b1100, 0b1010, 3, 0b1110),
            (1, 2, 7, 0),
        ]
        for a, b, op, expected in vectors:
            with self.subTest(a=a, b=b, op=op):
                self.assertEqual(simulate(module, a=a, b=b, op=op), {"y": expected})

    def test_mux_comparison_multiply_and_shifts(self) -> None:
        minimum = compile_source(
            "module Min { in a:u8 in b:u8 out y:u8 y=mux(a < b,a,b) }"
        ).ir
        multiply = compile_source(
            "module Mul { in a:u8 in b:u8 out y:u16 y=a*b }"
        ).ir
        left_shift = compile_source(
            "module Shift { in a:u8 in n:u3 out y:u8 y=a << n }"
        ).ir
        right_shift = compile_source(
            "module Shift { in a:s8 in n:u3 out y:s8 y=a >> n }"
        ).ir
        self.assertEqual(simulate(minimum, a=9, b=4), {"y": 4})
        self.assertEqual(simulate(multiply, a=255, b=255), {"y": 65025})
        self.assertEqual(simulate(left_shift, a=0x81, n=1), {"y": 2})
        self.assertEqual(simulate(right_shift, a=-8, n=2), {"y": -2})

    @unittest.skipUnless(
        CLASH_EXECUTABLE and shutil.which("iverilog") and shutil.which("vvp"),
        "Clash and Icarus Verilog are required",
    )
    def test_generated_alu_rtl_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory)
            subprocess.run(
                [
                    CLASH_EXECUTABLE,
                    "--verilog",
                    str(ROOT / "examples/generated/ALU.hs"),
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
                      reg [31:0] a, b;
                      reg [2:0] op;
                      wire [31:0] y;
                      ALU dut(.a(a), .b(b), .op(op), .y(y));
                      task check;
                        input [31:0] av, bv;
                        input [2:0] opv;
                        input [31:0] expected;
                        begin
                          a=av; b=bv; op=opv; #1;
                          if (y !== expected) $fatal(1, "unexpected ALU result");
                        end
                      endtask
                      initial begin
                        check(7, 5, 0, 12);
                        check(32'hffffffff, 1, 0, 0);
                        check(3, 5, 1, 32'hfffffffe);
                        check(12, 10, 2, 8);
                        check(12, 10, 3, 14);
                        check(1, 2, 7, 0);
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
