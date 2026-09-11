import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from zlang.backend.manifest import publish_artifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.backend.systemverilog import emit_experimental as emit_systemverilog
from zlang.ir.equivalence import BindingSide
from zlang.ir.expressions import InputRef
from zlang.ir.module import InstancePortBinding
from zlang.ir.types import BitType
from zlang.opt import CanonicalizationError, lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


SOURCE = """
protocol TinyBus<AW=8> {
  role initiator
  role target
  channel req:rv<uint<AW>> initiator -> target
  channel rsp:rv<uint<AW>> target -> initiator
  member irq:bit target -> initiator
}
module Producer<AW=8> {
  clock clk reset rst
  interface bus:TinyBus<AW=AW>.initiator
  out seen:bit
  bus.req.valid=1 bus.req.payload=0 bus.rsp.ready=1
  seen=bus.irq
}
module Consumer<AW=8> {
  clock clk reset rst
  interface bus:TinyBus<AW=AW>.target
  in alarm:bit
  bus.req.ready=1 bus.rsp.valid=0 bus.rsp.payload=0
  bus.irq=alarm
  out status:bit status=0
}
module Top<AW=8> {
  clock clk reset rst
  in alarm:bit
  inst p:Producer<AW=AW>
  inst c:Consumer<AW=AW> { alarm }
  connect p.bus -> c.bus
  out done:bit done=p.seen
}
"""


class ParameterizedAggregateProtocolTests(unittest.TestCase):
    def test_specialization_and_leaf_expansion(self):
        module = analyze(parse(SOURCE))
        self.assertEqual(len(module.aggregate_protocol_connections), 1)
        self.assertEqual(len(module.hierarchical_connections), 3)
        request = next(
            port for port in module.children[0].ports if port.name == "bus__req"
        )
        self.assertEqual(request.type.width, 8)
        self.assertTrue(any(item.name == "bus" for item in module.children[0].aggregate_protocol_endpoints))


    def test_direct_systemverilog_wires_reverse_scalar_member(self):
        module = analyze(parse(SOURCE))
        text = emit_systemverilog(module)
        self.assertIn("logic zlang_conn_2;", text)
        self.assertIn(".bus__irq(zlang_conn_2)", text)
        self.assertEqual(text.count(".bus__irq(zlang_conn_2)"), 2)
        artifact = emit_sv_artifact(module)
        self.assertEqual(artifact.backend, "direct_systemverilog")
        self.assertTrue(artifact.to_json())

    @unittest.skipUnless(shutil.which("verilator"), "Verilator is required")
    def test_reverse_scalar_member_simulates_in_direct_systemverilog(self):
        module = analyze(parse(SOURCE))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rtl = root / "Top.sv"
            bench = root / "tb.sv"
            rtl.write_text(emit_systemverilog(module))
            bench.write_text(r"""
module tb;
  logic clk = 0;
  logic rst = 1;
  logic alarm = 0;
  logic done;
  Top dut(.clk(clk), .rst(rst), .alarm(alarm), .done(done));
  initial begin
    #1;
    if (done !== 1'b0) $fatal(1, "reverse member low value was lost");
    alarm = 1'b1;
    #1;
    if (done !== 1'b1) $fatal(1, "reverse member high value was lost");
    $finish;
  end
endmodule
""")
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            result = subprocess.run(
                (
                    "verilator", "--binary", "--timing", "-Wno-DECLFILENAME",
                    "-Wno-UNUSED", "-Wno-UNDRIVEN", "--top-module", "tb",
                    str(rtl), str(bench),
                ),
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            run = subprocess.run(
                (str(root / "obj_dir" / "Vtb"),),
                capture_output=True,
                text=True,
            )
            self.assertEqual(run.returncode, 0, run.stderr or run.stdout)

    def test_manifest_retains_aggregate_identity(self):
        module = analyze(parse(SOURCE))
        artifact = publish_artifact(module.children[0], "tiny", backend="direct_systemverilog", selected_ir_identity="tiny-v1", side=BindingSide.IMPLEMENTATION)
        self.assertTrue(any(item.semantic_signal_id == "aggregate:Producer.bus" for item in artifact.bindings))

    def test_canonical_reverse_scalar_member_rejects_a_second_binding(self):
        canonical = lower(analyze(parse(SOURCE)))
        collision = InstancePortBinding(
            "p", "bus__irq", InputRef("alarm", BitType())
        )
        with self.assertRaisesRegex(
            CanonicalizationError,
            "p.bus__irq.*both a scalar binding and hierarchical connection",
        ):
            restore(replace(
                canonical,
                instance_bindings=(*canonical.instance_bindings, collision),
            ))

    def test_aggregate_buffering_is_rejected(self):
        source = SOURCE.replace("connect p.bus -> c.bus", "connect p.bus -> c.bus { buffer 2 }")
        with self.assertRaises(SemanticError):
            analyze(parse(source))

    def test_mismatched_specialization_is_rejected(self):
        source = SOURCE.replace("inst c:Consumer<AW=AW>", "inst c:Consumer<AW=16>")
        with self.assertRaises(SemanticError):
            analyze(parse(source))

    def test_missing_reverse_scalar_driver_fails_closed(self):
        source = SOURCE.replace("  bus.irq=alarm\n", "")
        with self.assertRaisesRegex(
            ValueError, "aggregate scalar output 'bus__irq' has no driver"
        ):
            emit_systemverilog(analyze(parse(source)))

    def test_multiple_reverse_scalar_drivers_are_rejected(self):
        source = SOURCE.replace(
            "  bus.irq=alarm\n", "  bus.irq=alarm\n  bus.irq=0\n"
        )
        with self.assertRaisesRegex(
            SemanticError, "output 'bus__irq' is assigned more than once"
        ):
            analyze(parse(source))


if __name__ == "__main__":
    unittest.main()
