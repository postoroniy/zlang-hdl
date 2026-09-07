import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from zlang.backend.systemverilog import emit_artifact, emit_experimental
from zlang.backend.systemverilog import SystemVerilogEmissionError
from zlang.compiler import compile_source
from zlang.formal import build_recursive_formal_design
from zlang.parser import parse
from zlang.semantic import analyze


ROOT = Path(__file__).resolve().parents[2]

@unittest.skipUnless(shutil.which("verilator"), "Verilator is required")
class ComposedDirectSystemVerilogTests(unittest.TestCase):
    def emit(self, source: str, top: str) -> str:
        return emit_experimental(compile_source((ROOT / "examples" / source).read_text(), top=top).ir)

    def test_real_design_hierarchy_lints(self) -> None:
        cases = (
            ("hierarchical_protocol_m40.zhl", "ProtocolTop"),
            ("hierarchical_request_response_m40.zhl", "HierarchicalRequestResponse"),
            ("simple_dma_m40.zhl", "SimpleDMA"),
            ("axi_csr_top.zhl", "AxiCsrTop"),
            ("apb_csr_top.zhl", "ApbCsrTop"),
        )
        for source, top in cases:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / f"{top}.sv"
                path.write_text(self.emit(source, top))
                completed = subprocess.run(
                    ("verilator", "--lint-only", "-Wno-fatal", "-Wno-DECLFILENAME",
                     "-Wno-UNUSED", "-Wno-UNDRIVEN", "--top-module", top, str(path)),
                    capture_output=True, text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_nested_fft_examples_use_explicit_selected_tops(self) -> None:
        source = (ROOT / "examples/fft/complex_multiply_pipeline_auto.zhl").read_text()
        for top in ("FFTComplexMultiplyRealAuto", "FFTComplexMultiplyImagAuto"):
            with self.subTest(top=top), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / f"{top}.sv"
                path.write_text(emit_experimental(compile_source(source, top=top).ir))
                completed = subprocess.run(
                    ("verilator", "--lint-only", "-Wno-fatal", "-Wno-DECLFILENAME",
                     "-Wno-UNUSED", "-Wno-UNDRIVEN", "--top-module", top, str(path)),
                    capture_output=True, text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

        sdf = (ROOT / "examples/fft/sdf_stage_numeric.zhl").read_text()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "FFTSDFStageNumericD4.sv"
            path.write_text(
                emit_experimental(
                    compile_source(sdf, top="FFTSDFStageNumericD4").ir
                )
            )
            completed = subprocess.run(
                (
                    "verilator", "--lint-only", "-Wno-fatal", "-Wno-DECLFILENAME",
                    "-Wno-UNUSED", "-Wno-UNDRIVEN", "--top-module",
                    "FFTSDFStageNumericD4", str(path),
                ),
                capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_recursive_manifest_uses_published_hierarchical_locators(self) -> None:
        module = compile_source(
            (ROOT / "examples/simple_dma_m40.zhl").read_text(), top="SimpleDMA"
        ).ir
        design = build_recursive_formal_design(module)
        artifact = emit_artifact(module, recursive_design=design)
        child_state = next(
            item for item in artifact.recursive_bindings
            if item.local_semantic_id == "register:count"
        )
        self.assertEqual(child_state.backend, "direct_systemverilog")
        self.assertEqual(child_state.rtl_path, ("engine",))
        self.assertEqual(child_state.signal_token, "count")
        self.assertIsNone(child_state.formal_observation_token)

    def test_instance_array_uses_compile_time_indexed_bindings(self) -> None:
        source = (
            "module Child { in x:u8 out y:u8 y=x } "
            "module Top { in values:vec<2,u8> out y:u8 c[2]:Child "
            "generate(i in 0..2) { c[i].x=values[i] } y=c[1].y }"
        )
        module = analyze(parse(source))
        text = emit_experimental(module)
        self.assertIn(" c_0 (", text)
        self.assertIn(" c_1 (", text)

    def _simulate(self, source: str, top: str, harness_text: str) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rtl = root / f"{top}.sv"
            harness = root / "test.cpp"
            rtl.write_text(self.emit(source, top))
            harness.write_text(harness_text)
            obj = root / "obj"
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            completed = subprocess.run(
                ("verilator", "--cc", "--exe", "--build", "--Mdir", str(obj),
                 "--top-module", top, str(rtl), str(harness)),
                cwd=root, env=environment, capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            executable = obj / f"V{top}"
            run = subprocess.run((str(executable),), capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr or run.stdout)

    def _simulate_systemverilog(self, source: str, top: str, testbench: str) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rtl = root / f"{top}.sv"
            bench = root / "tb.sv"
            rtl.write_text(self.emit(source, top))
            bench.write_text(testbench)
            obj = root / "obj"
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            completed = subprocess.run(
                ("verilator", "--binary", "--timing", "-Wno-fatal", "--Mdir", str(obj),
                 "--top-module", "tb", str(rtl), str(bench)),
                cwd=root, env=environment, capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            run = subprocess.run((str(obj / "Vtb"),), capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr or run.stdout)

    def test_all_syntax_nested_state_staging_simulates(self) -> None:
        self._simulate_systemverilog(
            "all_syntax.zhl", "StateSyntax", r"""
module tb;
  logic clk=0,rst=1,enable=0; logic [7:0] value;
  StateSyntax dut(clk,rst,enable,value);
  task tick; begin #1 clk=1; #1 clk=0; end endtask
  initial begin
    tick; rst=0; enable=1;
    tick; if(value != 0) $fatal(1,"first staged value must be reset value");
    tick; if(value != 1) $fatal(1,"nested stage did not capture prior count");
    enable=0; tick; if(value != 2) $fatal(1,"delay branch lost alignment");
    $finish;
  end
endmodule
""",
        )

    def test_direct_scalar_async_fifo_preserves_stalled_payload(self) -> None:
        self._simulate_systemverilog(
            "cdc_async_fifo.zhl", "CdcAsyncFifo", r"""
module tb;
  logic source_clock=0,source_reset=1,destination_clock=0,destination_reset=1;
  logic [7:0] source_payload=0,destination_payload;
  logic source_valid=0,source_ready,destination_valid,destination_ready=0;
  CdcAsyncFifo dut(.*);
  task source_tick; begin #1 source_clock=1; #1 source_clock=0; end endtask
  task destination_tick; begin #1 destination_clock=1; #1 destination_clock=0; end endtask
  initial begin
    source_tick; destination_tick;
    source_reset=0; destination_reset=0; #1;
    if(!source_ready) $fatal(1,"source not ready after reset");
    source_payload=8'h2a; source_valid=1; source_tick; source_valid=0;
    repeat(3) destination_tick;
    if(!destination_valid || destination_payload != 8'h2a)
      $fatal(1,"payload did not cross");
    repeat(2) begin
      destination_tick;
      if(!destination_valid || destination_payload != 8'h2a)
        $fatal(1,"payload changed while stalled");
    end
    destination_ready=1; destination_tick; destination_ready=0;
    destination_tick;
    if(destination_valid) $fatal(1,"consumed payload was duplicated");
    $finish;
  end
endmodule
""",
        )

    def test_direct_aggregate_async_fifo_transfers_one_atomic_beat(self) -> None:
        self._simulate_systemverilog(
            "all_syntax.zhl", "AggregateProtocolSyntax", r"""
module tb;
  logic clkI=0,rstI=1,clkO=0,rstO=1;
  logic [31:0] i_t_payload_data=0,o_t_payload_data;
  logic [3:0] i_t_payload_keep=0,o_t_payload_keep;
  logic [3:0] i_t_payload_strb=0,o_t_payload_strb;
  logic i_t_payload_last=0,o_t_payload_last;
  logic i_t_valid=0,i_t_ready,o_t_valid,o_t_ready=0;
  AggregateProtocolSyntax dut(.*);
  task input_tick; begin #1 clkI=1; #1 clkI=0; end endtask
  task output_tick; begin #1 clkO=1; #1 clkO=0; end endtask
  initial begin
    input_tick; output_tick; rstI=0; rstO=0; #1;
    i_t_payload_data=32'h1234_5678; i_t_payload_keep=4'ha;
    i_t_payload_strb=4'h5; i_t_payload_last=1;
    i_t_valid=1; input_tick; i_t_valid=0;
    repeat(3) output_tick;
    if(!o_t_valid || o_t_payload_data != 32'h1234_5678 ||
       o_t_payload_keep != 4'ha || o_t_payload_strb != 4'h5 || !o_t_payload_last)
      $fatal(1,"aggregate beat was not transferred atomically");
    output_tick;
    if(o_t_payload_data != 32'h1234_5678 || o_t_payload_keep != 4'ha ||
       o_t_payload_strb != 4'h5 || !o_t_payload_last)
      $fatal(1,"aggregate beat changed under backpressure");
    o_t_ready=1; output_tick; o_t_ready=0; output_tick;
    if(o_t_valid) $fatal(1,"aggregate beat was duplicated");
    $finish;
  end
endmodule
""",
        )

    def test_direct_memory_reset_clears_cells_and_registered_output(self) -> None:
        self._simulate_systemverilog(
            "all_syntax.zhl", "MemorySyntax", r"""
module tb;
  logic clk=0,rst=1,write_enable=0;
  logic [3:0] read_address=3,write_address=3;
  logic [7:0] write_data=0,read_data;
  MemorySyntax dut(.*);
  task tick; begin #1 clk=1; #1 clk=0; end endtask
  initial begin
    tick; rst=0;
    write_enable=1; write_data=8'ha5; tick;
    write_enable=0; tick;
    if(read_data != 8'ha5) $fatal(1,"memory write/read failed");
    rst=1; tick;
    if(read_data != 0) $fatal(1,"registered read output did not reset");
    rst=0; tick;
    if(read_data != 0) $fatal(1,"memory cell survived reset");
    $finish;
  end
endmodule
""",
        )

    def test_all_syntax_packet_arbiter_locks_until_last_transfer(self) -> None:
        self._simulate_systemverilog(
            "all_syntax.zhl", "ArbitrationSyntax", r"""
module tb;
  logic clk=0,rst=1;
  logic [7:0] high_payload=8'h11,low_payload=8'h22,tx_payload;
  logic high_valid=0,high_last=1,high_ready;
  logic low_valid=1,low_last=0,low_ready;
  logic tx_valid,tx_last,tx_ready=0;
  ArbitrationSyntax dut(clk,rst,high_payload,high_valid,high_last,high_ready,
                        low_payload,low_valid,low_last,low_ready,
                        tx_payload,tx_valid,tx_last,tx_ready);
  task tick; begin #1 clk=1; #1 clk=0; end endtask
  initial begin
    tick; rst=0; #1;
    if(!tx_valid || tx_payload != 8'h22 || low_ready)
      $fatal(1,"low-priority source was not presented under stall");
    tick; high_valid=1; tx_ready=1; #1;
    if(tx_payload != 8'h22 || !low_ready || high_ready)
      $fatal(1,"packet grant changed while locked");
    tick; low_payload=8'h23; low_last=1; tick; #1;
    if(tx_payload != 8'h11 || !high_ready || low_ready)
      $fatal(1,"grant did not move after final beat");
    $finish;
  end
endmodule
""",
        )

    def test_round_robin_packet_arbiter_rotates_at_packet_boundaries(self) -> None:
        self._simulate(
            "packet_round_robin.zhl", "PacketRoundRobin",
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

    def test_ready_valid_fifo_hierarchy_simulates(self) -> None:
        self._simulate(
            "hierarchical_protocol_m40.zhl", "ProtocolTop",
            '#include "VProtocolTop.h"\n'
            'static void tick(VProtocolTop& d){d.clk=0;d.eval();d.clk=1;d.eval();d.clk=0;d.eval();}\n'
            'int main(){VProtocolTop d{};d.rst=1;tick(d);tick(d);d.rst=0;for(int i=0;i<4;i++)tick(d);return d.seen==7?0:1;}\n',
        )

    def test_request_response_hierarchy_simulates(self) -> None:
        self._simulate(
            "hierarchical_request_response_m40.zhl", "HierarchicalRequestResponse",
            '#include "VHierarchicalRequestResponse.h"\n'
            'static void tick(VHierarchicalRequestResponse& d){d.clk=0;d.eval();d.clk=1;d.eval();d.clk=0;d.eval();}\n'
            'int main(){VHierarchicalRequestResponse d{};d.rst=1;d.data=9;tick(d);tick(d);d.rst=0;d.accept_request=1;d.fire=1;d.eval();if(!d.response_seen)return 1;tick(d);d.fire=0;d.accept_request=0;tick(d);if(d.response_seen)return 2;d.accept_response=1;tick(d);return d.response_seen?3:0;}\n',
        )

    def test_simple_dma_buffers_and_completes(self) -> None:
        self._simulate(
            "simple_dma_m40.zhl", "SimpleDMA",
            '#include "VSimpleDMA.h"\n'
            'static void tick(VSimpleDMA& d){d.clk=0;d.eval();d.clk=1;d.eval();d.clk=0;d.eval();}\n'
            'int main(){VSimpleDMA d{};d.base=10;d.data=0x5a;d.start=0;d.accept=0;d.rst=1;tick(d);tick(d);d.rst=0;tick(d);if(d.busy)return 1;d.start=1;tick(d);if(!d.busy)return 2;tick(d);d.accept=1;tick(d);if(!d.busy)return 3;d.start=0;for(int i=0;i<4;i++)tick(d);return d.busy?4:0;}\n',
        )

    def test_source_authored_apb_and_axi_csr_transactions_simulate(self) -> None:
        self._simulate_systemverilog(
            "apb_csr_top.zhl", "ApbCsrTop", r"""
module tb;
  logic clk=0,rst=1,done,psel=0,penable=0,pwrite=0,pready,pslverr;
  logic [31:0] paddr=0,pwdata=0,prdata;
  ApbCsrTop dut(clk,rst,done,psel,penable,pwrite,paddr,pwdata,pready,prdata,pslverr);
  task tick; begin #1 clk=1; #1 clk=0; end endtask
  integer seen_done=0,seen_ready=0,i;
  initial begin
    tick; tick; rst=0; tick;
    psel=1; pwrite=1; paddr=0; pwdata=1; tick;
    penable=1; #1; if(done) seen_done=1; clk=1; #1; clk=0; if(pready) seen_ready=1;
    for(i=0;i<8;i=i+1) begin tick; if(done) seen_done=1; if(pready) seen_ready=1; end
    if(!seen_done || !seen_ready || pslverr) $fatal(1,"APB transaction failed");
    $finish;
  end
endmodule
""",
        )
        self._simulate_systemverilog(
            "axi_csr_top.zhl", "AxiCsrTop", r"""
module tb;
  logic clk=0,rst=1,done;
  logic [31:0] aw_addr=0,ar_addr=0,w_data=1;
  logic [3:0] w_strb=4'hf;
  logic aw_valid=0,w_valid=0,ar_valid=0,b_ready=1,r_ready=1;
  wire aw_ready,w_ready,ar_ready,b_valid,r_valid;
  wire [1:0] b_resp,r_resp; wire [31:0] r_data;
  AxiCsrTop dut(
    .clk(clk), .rst(rst), .done(done),
    .axi_aw_payload_addr(aw_addr), .axi_aw_valid(aw_valid), .axi_aw_ready(aw_ready),
    .axi_w_payload_data(w_data), .axi_w_payload_strb(w_strb),
    .axi_w_valid(w_valid), .axi_w_ready(w_ready),
    .axi_b_payload_resp(b_resp), .axi_b_valid(b_valid), .axi_b_ready(b_ready),
    .axi_ar_payload_addr(ar_addr), .axi_ar_valid(ar_valid), .axi_ar_ready(ar_ready),
    .axi_r_payload_data(r_data), .axi_r_payload_resp(r_resp),
    .axi_r_valid(r_valid), .axi_r_ready(r_ready)
  );
  task tick; begin #1 clk=1; #1 clk=0; end endtask
    integer seen_b=0,i;
  initial begin
    tick; tick; rst=0; tick; aw_valid=1; w_valid=1;
    for(i=0;i<12;i=i+1) begin
      tick; if(aw_ready) aw_valid=0; if(w_ready) w_valid=0;
      if(b_valid) seen_b=1;
    end
    if(!seen_b || b_resp!=0) $fatal(1,"AXI-Lite transaction failed");
    $finish;
  end
endmodule
""",
        )


if __name__ == "__main__":
    unittest.main()
