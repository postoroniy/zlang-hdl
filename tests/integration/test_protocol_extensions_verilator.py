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


class ProtocolExtensionVerilatorTests(unittest.TestCase):
    def run_rtl(self, example: str, module_name: str, harness_source: str) -> None:
        result = compile_source((ROOT / "examples" / example).read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = generate_verilog(
                result.clash,
                result.ir.name,
                root / "rtl",
                CLASH_EXECUTABLE,
            )
            harness = root / "protocol_test.cpp"
            harness.write_text(harness_source)
            object_directory = root / "obj"
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            subprocess.run(
                [
                    VERILATOR,
                    "--cc",
                    "--exe",
                    "--build",
                    "-Wno-WIDTHTRUNC",
                    "--top-module",
                    module_name,
                    "--Mdir",
                    str(object_directory),
                    "-o",
                    "protocol_sim",
                    *(str(path) for path in files),
                    str(harness),
                ],
                check=True,
                cwd=ROOT,
                env=environment,
            )
            subprocess.run([str(object_directory / "protocol_sim")], check=True)

    @unittest.skipUnless(
        CLASH_EXECUTABLE and VERILATOR,
        "Clash and Verilator are required",
    )
    def test_fixed_priority_packet_lock_runs_in_verilator(self) -> None:
        self.run_rtl(
            "packet_fixed_arbiter.zhl",
            "PacketFixedArbiter",
            r'''#include "VPacketFixedArbiter.h"
static void tick(VPacketFixedArbiter& d) {
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}
int main() {
  VPacketFixedArbiter d; d.clk = 0; d.rst = 1; d.tx_ready = 0;
  d.high_priority_payload = 0; d.high_priority_valid = 0; d.high_priority_last = 1;
  d.low_priority_payload = 0; d.low_priority_valid = 0; d.low_priority_last = 1;
  tick(d); d.rst = 0;
  d.high_priority_payload = 10; d.high_priority_valid = 1; d.high_priority_last = 0;
  d.low_priority_payload = 20; d.low_priority_valid = 1; d.low_priority_last = 1;
  d.eval(); if (!d.tx_valid || d.tx_payload != 10) return 1;
  tick(d);
  d.tx_ready = 1; d.eval();
  if (!d.high_priority_ready || d.low_priority_ready) return 2;
  tick(d);
  d.high_priority_payload = 11; d.high_priority_last = 1; d.eval();
  if (d.tx_payload != 11 || !d.high_priority_ready) return 3;
  tick(d);
  d.high_priority_valid = 0; d.eval();
  if (!d.low_priority_ready || d.tx_payload != 20 || !d.tx_last) return 4;
  return 0;
}
''',
        )

    @unittest.skipUnless(
        CLASH_EXECUTABLE and VERILATOR,
        "Clash and Verilator are required",
    )
    def test_round_robin_fairness_runs_in_verilator(self) -> None:
        self.run_rtl(
            "packet_round_robin.zhl",
            "PacketRoundRobin",
            r'''#include "VPacketRoundRobin.h"
static void tick(VPacketRoundRobin& d) {
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}
int main() {
  VPacketRoundRobin d; d.clk = 0; d.rst = 1; d.tx_ready = 1;
  d.source_a_payload = 0; d.source_a_valid = 0; d.source_a_last = 1;
  d.source_b_payload = 0; d.source_b_valid = 0; d.source_b_last = 1;
  tick(d); d.rst = 0;
  d.source_a_payload = 1; d.source_a_valid = 1;
  d.source_b_payload = 2; d.source_b_valid = 1; d.eval();
  if (!d.source_a_ready || d.source_b_ready || d.tx_payload != 1) return 1;
  tick(d);
  d.source_a_payload = 3; d.eval();
  if (d.source_a_ready || !d.source_b_ready || d.tx_payload != 2) return 2;
  tick(d);
  d.source_b_payload = 4; d.eval();
  if (!d.source_a_ready || d.source_b_ready || d.tx_payload != 3) return 3;
  return 0;
}
''',
        )

    @unittest.skipUnless(
        CLASH_EXECUTABLE and VERILATOR,
        "Clash and Verilator are required",
    )
    def test_per_vc_credit_gating_runs_in_verilator(self) -> None:
        self.run_rtl(
            "vc_credit_source.zhl",
            "VcCreditSource",
            r'''#include "VVcCreditSource.h"
static void tick(VVcCreditSource& d) {
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}
int main() {
  VVcCreditSource d; d.clk = 0; d.rst = 1; d.payload = 0;
  d.channel = 0; d.request = 0; d.tx_return = 0; d.tx_return_vc = 0;
  tick(d); if (d.tx_send) return 1;
  d.rst = 0; d.request = 1; d.payload = 10; d.eval();
  if (!d.tx_send || d.tx_vc != 0) return 2;
  tick(d); d.payload = 11; tick(d); d.payload = 12; d.eval();
  if (d.tx_send) return 3;
  d.channel = 1; d.payload = 20; d.eval();
  if (!d.tx_send || d.tx_vc != 1) return 4;
  tick(d);
  d.channel = 0; d.tx_return = 1; d.tx_return_vc = 0; d.eval();
  if (d.tx_send) return 5;
  tick(d); d.tx_return = 0; d.eval();
  if (!d.tx_send) return 6;
  return 0;
}
''',
        )


if __name__ == "__main__":
    unittest.main()
