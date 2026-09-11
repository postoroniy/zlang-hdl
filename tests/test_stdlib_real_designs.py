import shutil
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.standard_bus import (
    AxiStreamBeat,
    RegResponse,
    WishboneInput,
    WishboneToRegBus,
    axi_stream_transfer,
)
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[1]
REAL_DESIGNS = (
    ("streaming_packet_engine.zhl", "StreamingPacketEngine"),
    ("fixed_polyphase_fir.zhl", "FixedPointPolyphaseFIR"),
    ("multichannel_dma.zhl", "MultiChannelDMA"),
    ("wishbone_csr_top.zhl", "WishboneCsrTop"),
)


class StandardLibraryRealDesignTests(unittest.TestCase):
    def _run_sv_tb(self, source_name: str, top: str, bench: str) -> None:
        result = compile_source((ROOT / "examples" / source_name).read_text(), top=top)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rtl = root / f"{top}.sv"
            tb = root / "tb.sv"
            rtl.write_text(emit_sv_artifact(result.ir, selected_ir_identity=top).text)
            tb.write_text(bench)
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            subprocess.run(("verilator", "--binary", "--top-module", "tb", "-Wno-fatal",
                            str(rtl), str(tb), "-Mdir", str(root / "obj")), check=True,
                           capture_output=True, text=True, env=environment)
            subprocess.run((str(root / "obj" / "Vtb"),), check=True,
                           capture_output=True, text=True)


    def test_wishbone_oracle_distinguishes_buffering_from_completion(self):
        seen = []
        bridge = WishboneToRegBus(lambda request: seen.append(request) or RegResponse(0x55, False))
        first = bridge.step(WishboneInput(cyc=True, stb=True, adr=4))
        self.assertFalse(first.ack)
        self.assertTrue(first.stall)
        second = bridge.step(WishboneInput(cyc=True))
        self.assertFalse(second.ack)
        third = bridge.step(WishboneInput(cyc=True))
        self.assertTrue(third.ack)
        self.assertEqual(third.dat_r, 0x55)
        self.assertEqual(len(seen), 1)
        self.assertFalse(bridge.step(WishboneInput(reset=True)).ack)

    def test_axi_stream_oracle_preserves_stall_and_packet_boundary(self):
        beat = AxiStreamBeat(0x1234, 0xF, 0xF, True)
        self.assertIsNone(axi_stream_transfer(True, False, beat))
        self.assertEqual(axi_stream_transfer(True, True, beat), beat)
        with self.assertRaisesRegex(SemanticError, "constant width division must be exact"):
            analyze(parse(
                "import std.bus.axi_stream module Bad { clock clk reset rst "
                "interface stream:AXIStream<30>.source @clk }"
            ))

    @unittest.skipUnless(shutil.which("verilator"), "Verilator is unavailable")
    def test_real_design_direct_sv_is_lint_clean(self):
        with tempfile.TemporaryDirectory() as temporary:
            for filename, top in REAL_DESIGNS:
                result = compile_source((ROOT / "examples" / filename).read_text(), top=top)
                path = Path(temporary) / f"{top}.sv"
                path.write_text(emit_sv_artifact(result.ir, selected_ir_identity=top).text)
                subprocess.run(("verilator", "--lint-only", "-Wall", "-Wno-fatal", str(path)), check=True)

    @unittest.skipUnless(shutil.which("verilator"), "Verilator is unavailable")
    def test_wishbone_csr_write_read_simulation(self):
        self._run_sv_tb("wishbone_csr_top.zhl", "WishboneCsrTop", r"""
module tb;
  logic clk=0, rst=1, cyc=0, stb=0, we=0;
  logic [31:0] adr=0, dat_w=0; logic [3:0] sel=4'hf;
  logic [31:0] last_data=0;
  wire done, ack, err, stall; wire [31:0] dat_r;
  WishboneCsrTop dut(.clk,.rst,.done,.wb_cyc(cyc),.wb_stb(stb),.wb_we(we),
    .wb_adr(adr),.wb_dat_w(dat_w),.wb_sel(sel),.wb_ack(ack),.wb_err(err),
    .wb_stall(stall),.wb_dat_r(dat_r));
  task tick; begin #1 clk=1; #1 clk=0; end endtask
  task wait_ack; integer n; begin
    n=0; while (!ack && n<12) begin tick; n=n+1; end
    if (!ack || err) $fatal(1,"Wishbone transaction did not complete cleanly");
    last_data=dat_r; tick;
  end endtask
  initial begin
    tick; tick; rst=0;
    cyc=1; stb=1; we=1; dat_w=1; tick; stb=0; wait_ack; cyc=0; tick;
    cyc=1; stb=1; we=0; dat_w=0; tick; stb=0; wait_ack;
    if (last_data[0] !== 1'b1) $fatal(1,"CSR readback mismatch");
    cyc=0; tick; $finish;
  end
endmodule
""")

    @unittest.skipUnless(shutil.which("verilator"), "Verilator is unavailable")
    def test_fixed_fir_and_dma_reset_simulation(self):
        self._run_sv_tb("fixed_polyphase_fir.zhl", "FixedPointPolyphaseFIR", r"""
module tb;
  logic clk=0, rst=1;
  logic signed [11:0] samples0 [0:7];
  logic signed [11:0] samples1 [0:7];
  logic signed [11:0] samples2 [0:7];
  logic signed [11:0] samples3 [0:7];
  logic signed [11:0] coefficients [0:7];
  wire signed [26:0] result_phase0,result_phase1,result_phase2,result_phase3;
  FixedPointPolyphaseFIR dut(.*);
  task tick; begin #1 clk=1; #1 clk=0; end endtask
  integer i;
  initial begin
    for (i=0;i<8;i=i+1) begin
      samples0[i]=0;samples1[i]=0;samples2[i]=0;samples3[i]=0;coefficients[i]=0;
    end
    tick; rst=0;
    for (i=0;i<8;i=i+1) begin
      samples0[i]=12'h400;samples1[i]=12'h400;
      samples2[i]=12'h400;samples3[i]=12'h400;coefficients[i]=12'h400;
    end
    tick; tick; tick;
    if (result_phase0 !== 27'd8388608 || result_phase1 !== 27'd8388608 ||
        result_phase2 !== 27'd8388608 || result_phase3 !== 27'd8388608)
      $fatal(1,"polyphase result mismatch");
    $finish;
  end
endmodule
""")
        self._run_sv_tb("multichannel_dma.zhl", "MultiChannelDMA", r"""
module tb;
  logic clk=0,rst=1,start0=0,start1=0,accept0=1,accept1=1,ready0=1,ready1=1;
  logic [15:0] base0=16'h10,base1=16'h80;
  wire status_busy0,status_busy1;
  MultiChannelDMA dut(.*);
  task tick; begin #1 clk=1; #1 clk=0; end endtask
  initial begin
    tick; tick; rst=0; start0=1; start1=1; tick;
    if (!status_busy0 || !status_busy1) $fatal(1,"DMA channels did not start");
    repeat(6) tick;
    rst=1; start0=0; start1=0; tick; rst=0; tick;
    if (status_busy0 || status_busy1) $fatal(1,"DMA reset/status mismatch");
    $finish;
  end
endmodule
""")



if __name__ == "__main__":
    unittest.main()
