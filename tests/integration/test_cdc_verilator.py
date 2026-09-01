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


class CdcVerilatorTests(unittest.TestCase):
    def run_rtl(self, example: str, module_name: str, harness_source: str) -> None:
        result = compile_source((ROOT / "examples" / example).read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            verilog_files = generate_verilog(
                result.clash,
                result.ir.name,
                root / "rtl",
                CLASH_EXECUTABLE,
            )
            harness = root / "cdc_test.cpp"
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
                    "cdc_sim",
                    *(str(path) for path in verilog_files),
                    str(harness),
                ],
                check=True,
                cwd=ROOT,
                env=environment,
            )
            subprocess.run([str(object_directory / "cdc_sim")], check=True)

    @unittest.skipUnless(
        CLASH_EXECUTABLE and VERILATOR,
        "Clash and Verilator are required",
    )
    def test_level_and_pulse_crossings_run_in_verilator(self) -> None:
        self.run_rtl(
            "cdc_level.zl",
            "CdcLevel",
            r'''#include "VCdcLevel.h"
static void sourceTick(VCdcLevel& d) {
  d.source_clock = 0; d.eval(); d.source_clock = 1; d.eval();
  d.source_clock = 0; d.eval();
}
static void destinationTick(VCdcLevel& d) {
  d.destination_clock = 0; d.eval(); d.destination_clock = 1; d.eval();
  d.destination_clock = 0; d.eval();
}
int main() {
  VCdcLevel d; d.source_clock = 0; d.destination_clock = 0; d.level = 0;
  d.source_reset = 1; d.destination_reset = 1;
  sourceTick(d); destinationTick(d);
  d.source_reset = 0; d.destination_reset = 0; d.level = 1;
  destinationTick(d); if (d.synced) return 1;
  destinationTick(d); if (!d.synced) return 2;
  return 0;
}
''',
        )
        self.run_rtl(
            "cdc_pulse.zl",
            "CdcPulse",
            r'''#include "VCdcPulse.h"
static void sourceTick(VCdcPulse& d) {
  d.source_clock = 0; d.eval(); d.source_clock = 1; d.eval();
  d.source_clock = 0; d.eval();
}
static void destinationTick(VCdcPulse& d) {
  d.destination_clock = 0; d.eval(); d.destination_clock = 1; d.eval();
  d.destination_clock = 0; d.eval();
}
int main() {
  VCdcPulse d; d.source_clock = 0; d.destination_clock = 0; d.pulse = 0;
  d.source_reset = 1; d.destination_reset = 1;
  sourceTick(d); destinationTick(d);
  d.source_reset = 0; d.destination_reset = 0; d.pulse = 1;
  sourceTick(d); d.pulse = 0;
  destinationTick(d); if (d.crossed_pulse) return 1;
  destinationTick(d); if (!d.crossed_pulse) return 2;
  destinationTick(d); if (d.crossed_pulse) return 3;
  return 0;
}
''',
        )

    @unittest.skipUnless(
        CLASH_EXECUTABLE and VERILATOR,
        "Clash and Verilator are required",
    )
    def test_handshake_crossing_runs_in_verilator(self) -> None:
        self.run_rtl(
            "cdc_handshake.zl",
            "CdcHandshake",
            r'''#include "VCdcHandshake.h"
static void sourceTick(VCdcHandshake& d) {
  d.source_clock = 0; d.eval(); d.source_clock = 1; d.eval();
  d.source_clock = 0; d.eval();
}
static void destinationTick(VCdcHandshake& d) {
  d.destination_clock = 0; d.eval(); d.destination_clock = 1; d.eval();
  d.destination_clock = 0; d.eval();
}
int main() {
  VCdcHandshake d; d.source_clock = 0; d.destination_clock = 0;
  d.source_payload = 0; d.source_valid = 0; d.destination_ready = 0;
  d.source_reset = 1; d.destination_reset = 1;
  sourceTick(d); destinationTick(d);
  d.source_reset = 0; d.destination_reset = 0; d.eval();
  if (!d.source_ready) return 1;
  d.source_payload = 42; d.source_valid = 1; sourceTick(d);
  d.source_valid = 0; d.source_payload = 99; d.eval();
  if (d.source_ready) return 2;
  destinationTick(d); if (d.destination_valid) return 3;
  destinationTick(d);
  if (!d.destination_valid || d.destination_payload != 42) return 4;
  destinationTick(d);
  if (!d.destination_valid || d.destination_payload != 42) return 5;
  d.destination_ready = 1; destinationTick(d);
  d.destination_ready = 0; d.eval();
  if (d.destination_valid) return 6;
  sourceTick(d); if (d.source_ready) return 7;
  sourceTick(d); if (!d.source_ready) return 8;
  return 0;
}
''',
        )

    @unittest.skipUnless(
        CLASH_EXECUTABLE and VERILATOR,
        "Clash and Verilator are required",
    )
    def test_async_fifo_crossing_runs_in_verilator(self) -> None:
        self.run_rtl(
            "cdc_async_fifo.zl",
            "CdcAsyncFifo",
            r'''#include "VCdcAsyncFifo.h"
static void sourceTick(VCdcAsyncFifo& d) {
  d.source_clock = 0; d.eval(); d.source_clock = 1; d.eval();
  d.source_clock = 0; d.eval();
}
static void destinationTick(VCdcAsyncFifo& d) {
  d.destination_clock = 0; d.eval(); d.destination_clock = 1; d.eval();
  d.destination_clock = 0; d.eval();
}
static int send(VCdcAsyncFifo& d, int value) {
  d.source_payload = value; d.source_valid = 1; d.eval();
  if (!d.source_ready) return 1;
  sourceTick(d); d.source_valid = 0; d.eval(); return 0;
}
static int receive(VCdcAsyncFifo& d, int expected) {
  d.destination_ready = 0;
  for (int i = 0; i < 8 && !d.destination_valid; ++i) destinationTick(d);
  if (!d.destination_valid || d.destination_payload != expected) return 1;
  d.destination_ready = 1; destinationTick(d);
  d.destination_ready = 0; d.eval(); return 0;
}
int main() {
  VCdcAsyncFifo d; d.source_clock = 0; d.destination_clock = 0;
  d.source_payload = 0; d.source_valid = 0; d.destination_ready = 0;
  d.source_reset = 1; d.destination_reset = 1;
  sourceTick(d); destinationTick(d);
  d.source_reset = 0; d.destination_reset = 0; d.eval();
  if (!d.source_ready) return 1;
  if (send(d, 11)) return 2;
  if (send(d, 22)) return 3;
  if (receive(d, 11)) return 4;
  if (receive(d, 22)) return 5;
  return 0;
}
''',
        )


if __name__ == "__main__":
    unittest.main()
