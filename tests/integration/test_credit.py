from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from tests.toolchain import CLASH_ENVIRONMENT, CLASH_EXECUTABLE
from zlang.compiler import compile_source
from zlang.simulate import (
    ProtocolViolation,
    SimulationError,
    simulate_credit_cycles,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples/credit_source.zhl").read_text()


def source_cycle(
    payload: int,
    request: int,
    returned: int,
) -> dict[str, object]:
    return {
        "payload_data": payload,
        "request": request,
        "tx": {"return": returned},
    }


class CreditIntegrationTests(unittest.TestCase):
    def test_normal_zero_credit_and_return_behavior(self) -> None:
        module = compile_source(SOURCE).ir
        results = simulate_credit_cycles(
            module,
            [
                source_cycle(10, 1, 0),
                source_cycle(11, 1, 0),
                source_cycle(12, 1, 0),
                source_cycle(13, 1, 1),
                source_cycle(14, 1, 0),
            ],
        )
        self.assertEqual(
            [cycle["tx"]["credits"] for cycle in results],
            [2, 1, 0, 0, 1],
        )
        self.assertEqual(
            [cycle["tx"]["send"] for cycle in results],
            [1, 1, 0, 0, 1],
        )
        self.assertEqual(results[2]["tx"]["payload"], 12)

    def test_simultaneous_send_and_return_is_neutral_with_available_credit(self) -> None:
        module = compile_source(SOURCE).ir
        results = simulate_credit_cycles(
            module,
            [source_cycle(1, 1, 1), source_cycle(2, 1, 0)],
        )
        self.assertEqual(
            [(cycle["tx"]["credits"], cycle["tx"]["send"]) for cycle in results],
            [(2, 1), (2, 1)],
        )

    def test_reset_restores_maximum_credits_and_suppresses_transfer(self) -> None:
        module = compile_source(SOURCE).ir
        results = simulate_credit_cycles(
            module,
            [
                source_cycle(1, 1, 0),
                source_cycle(2, 1, 0),
                source_cycle(3, 1, 0),
                source_cycle(4, 1, 0),
            ],
            reset=[False, False, True, False],
        )
        self.assertEqual(
            [(cycle["tx"]["credits"], cycle["tx"]["send"]) for cycle in results],
            [(2, 1), (1, 1), (2, 0), (2, 1)],
        )

    def test_return_at_maximum_credits_is_an_overflow(self) -> None:
        module = compile_source(SOURCE).ir
        with self.assertRaisesRegex(ProtocolViolation, "return at maximum credits"):
            simulate_credit_cycles(module, [source_cycle(1, 0, 1)])

    def test_receiver_underflow_overflow_and_simultaneous_boundary(self) -> None:
        module = compile_source(
            "module CreditSink { clock clk reset rst in release:bit "
            "in rx:credit<u8,2> rx.return=release }"
        ).ir
        with self.assertRaisesRegex(ProtocolViolation, "underflow"):
            simulate_credit_cycles(
                module,
                [{"release": 1, "rx": {"payload": 0, "send": 0}}],
            )
        with self.assertRaisesRegex(ProtocolViolation, "maximum occupancy"):
            simulate_credit_cycles(
                module,
                [
                    {"release": 0, "rx": {"payload": 1, "send": 1}},
                    {"release": 0, "rx": {"payload": 2, "send": 1}},
                    {"release": 0, "rx": {"payload": 3, "send": 1}},
                ],
            )
        result = simulate_credit_cycles(
            module,
            [{"release": 1, "rx": {"payload": 7, "send": 1}}],
        )
        self.assertEqual(result[0]["rx"], {"return": 1, "transfer": 1})

    def test_credit_input_shape_is_validated(self) -> None:
        module = compile_source(SOURCE).ir
        with self.assertRaisesRegex(
            SimulationError, "credit input 'tx' requires fields: return"
        ):
            simulate_credit_cycles(
                module,
                [{"payload_data": 1, "request": 1, "tx": {"send": 0}}],
            )

    @unittest.skipUnless(
        CLASH_EXECUTABLE and shutil.which("iverilog") and shutil.which("vvp"),
        "Clash and Icarus Verilog are required",
    )
    def test_generated_credit_rtl_counter_and_assertions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory)
            subprocess.run(
                [
                    CLASH_EXECUTABLE,
                    "--verilog",
                    str(ROOT / "examples/generated/CreditSource.hs"),
                    "-outputdir",
                    str(output_directory),
                ],
                check=True,
                cwd=ROOT,
                env=CLASH_ENVIRONMENT,
            )
            generated = next(output_directory.rglob("CreditSource.v"))
            verilog = generated.read_text()
            self.assertIn("psl property tx_no_underflow", verilog)
            self.assertIn("psl property tx_no_overflow", verilog)

            testbench = output_directory / "tb.v"
            testbench.write_text(
                textwrap.dedent(
                    """
                    module tb;
                      reg clk=0;
                      reg rst=1;
                      reg [7:0] payload_data=8'h2a;
                      reg request=1;
                      reg tx_return=0;
                      wire [7:0] tx_payload;
                      wire tx_send;
                      CreditSource dut(
                        .clk(clk), .rst(rst), .payload_data(payload_data),
                        .request(request), .tx_return(tx_return),
                        .tx_payload(tx_payload), .tx_send(tx_send)
                      );
                      always #5 clk = ~clk;
                      initial begin
                        @(posedge clk); #1; rst=0; #1;
                        if (tx_send !== 1 || tx_payload !== 8'h2a)
                          $fatal(1, "initial credit unavailable");
                        @(posedge clk); #1;
                        if (tx_send !== 1) $fatal(1, "second credit unavailable");
                        @(posedge clk); #1;
                        if (tx_send !== 0) $fatal(1, "zero credits did not stall");
                        tx_return=1; #1;
                        if (tx_send !== 0) $fatal(1, "same-cycle zero reuse occurred");
                        @(posedge clk); #1; tx_return=0; #1;
                        if (tx_send !== 1) $fatal(1, "returned credit unavailable");
                        rst=1; @(posedge clk); #1;
                        if (tx_send !== 0) $fatal(1, "reset did not suppress send");
                        $finish;
                      end
                    endmodule
                    """
                )
            )
            executable = output_directory / "simulation"
            verilog_files = [str(path) for path in output_directory.rglob("*.v")]
            subprocess.run(
                [
                    "iverilog",
                    "-g2012",
                    "-s",
                    "tb",
                    "-o",
                    str(executable),
                    *verilog_files,
                ],
                check=True,
            )
            subprocess.run(["vvp", str(executable)], check=True)

    @unittest.skipUnless(CLASH_EXECUTABLE, "Clash executable is not available")
    def test_generated_credit_receiver_compiles_with_assertions(self) -> None:
        source = (
            "module CreditSink { clock clk reset rst in release:bit "
            "in rx:credit<u8,2> out observed:u8 "
            "rx.return=release observed=rx.payload }"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory)
            source_path = output_directory / "CreditSink.hs"
            source_path.write_text(compile_source(source).clash)
            subprocess.run(
                [
                    CLASH_EXECUTABLE,
                    "--verilog",
                    str(source_path),
                    "-outputdir",
                    str(output_directory / "rtl"),
                ],
                check=True,
                cwd=ROOT,
                env=CLASH_ENVIRONMENT,
            )
            verilog = next((output_directory / "rtl").rglob("CreditSink.v")).read_text()
            self.assertIn("psl property rx_no_underflow", verilog)
            self.assertIn("psl property rx_no_overflow", verilog)


if __name__ == "__main__":
    unittest.main()
