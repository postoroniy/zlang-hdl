from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact, emit_experimental
from zlang.compiler import compile_source
from zlang.formal import run_verilog_formal
from zlang.ir.formal import FormalStatus
from zlang.toolchain import find_clash_executable, generate_verilog


ROOT = Path(__file__).resolve().parents[2]


class M37BackendTests(unittest.TestCase):
    def compile(self, name):
        return compile_source((ROOT / "examples" / name).read_text())

    def test_both_backends_publish_versioned_artifact_manifests(self):
        result = self.compile("alu.zhl")
        for artifact in (emit_clash_artifact(result.ir), emit_sv_artifact(result.ir)):
            self.assertEqual(artifact.manifest_version, 2)
            self.assertEqual(len(artifact.artifact_hash), 64)
            artifact.binding_map().validate(required=())
            data = artifact.to_json()
            self.assertIn('"selected_ir_identity"', data)
            self.assertIn('"source_origin"', data)
            self.assertEqual({item.backend for item in artifact.bindings}, {artifact.backend})

    def test_direct_reductions_are_lowered_from_typed_ir(self):
        for name in ("dot_product.zhl", "generated_reduce.zhl", "mapped_sum.zhl"):
            with self.subTest(name=name):
                text = emit_experimental(self.compile(name).ir)
                self.assertNotIn("unsupported direct SystemVerilog expression Reduce", text)
                self.assertIn("assign y", text)

    def test_direct_fifo_is_emitted_and_lints_when_available(self):
        text = emit_experimental(self.compile("fifo_bridge.zhl").ir)
        self.assertIn("queue_storage", text)
        self.assertIn("queue_count", text)
        verilator = shutil.which("verilator")
        if verilator:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "FifoBridge.sv"
                path.write_text(text)
                completed = subprocess.run((verilator, "--lint-only", "-Wall", "--top-module", "FifoBridge", str(path)),
                                           text=True, capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_direct_fifo_renders_typed_read_side_projection(self):
        result = compile_source(
            """
            module FifoProjection {
                clock clk
                reset rst
                in rx : rv<vec<2,u8>>
                out tx : rv<vec<3,u8>>
                fifo queue : fifo<vec<2,u8>,1>
                queue.data = rx.payload
                queue.push = rx.transfer
                queue.pop = tx.transfer
                rx.ready = queue.ready
                tx.valid = queue.valid
                tx.payload = generate(i in 0..3) queue.front[i & 1]
            }
            """,
            top="FifoProjection",
            include_clash=False,
        )
        text = emit_experimental(result.ir)
        self.assertIn("logic [15:0] queue_front;", text)
        self.assertIn("assign queue_front = queue_storage[queue_rd];", text)
        self.assertIn("assign tx_payload = {", text)
        self.assertNotIn("assign tx_payload = queue_storage", text)
        verilator = shutil.which("verilator")
        if verilator:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "FifoProjection.sv"
                path.write_text(text)
                completed = subprocess.run(
                    (
                        verilator,
                        "--lint-only",
                        "-Wall",
                        "-Wno-DECLFILENAME",
                        "--top-module",
                        "FifoProjection",
                        str(path),
                    ),
                    text=True,
                    capture_output=True,
                )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_direct_vector_unpack_reduction_uses_legal_compound_slices(self):
        result = compile_source(
            """
            fn parity7(value:u7) -> bit {
                reduce(^, unpack<vec<7,bit>>(pack(value)))
            }
            module PackedParity { in value:u7 out parity:bit parity=parity7(value) }
            """,
            top="PackedParity",
            include_clash=False,
        )
        text = emit_experimental(result.ir)
        self.assertNotRegex(text, r"\$unsigned\([^\n]+\)\[[0-9]+\]")
        verilator = shutil.which("verilator")
        if verilator:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "PackedParity.sv"
                path.write_text(text)
                completed = subprocess.run(
                    (
                        verilator,
                        "--lint-only",
                        "-Wall",
                        "--top-module",
                        "PackedParity",
                        str(path),
                    ),
                    text=True,
                    capture_output=True,
                )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_direct_mangles_systemverilog_sequence_identifier(self):
        result = compile_source(
            """
            module ReservedSequence {
                clock clk
                reset rst
                in advance:bit
                out y:u7
                reg sequence:u7=0
                rule step when advance { sequence <- truncate<7>(sequence + 1) }
                y=sequence
            }
            """,
            top="ReservedSequence",
            include_clash=False,
        )
        text = emit_experimental(result.ir)
        self.assertIn("logic [6:0] zlang_sequence;", text)
        self.assertNotRegex(text, r"\blogic \[6:0\] sequence;")
        verilator = shutil.which("verilator")
        if verilator:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "ReservedSequence.sv"
                path.write_text(text)
                completed = subprocess.run(
                    (
                        verilator,
                        "--lint-only",
                        "-Wall",
                        "--top-module",
                        "ReservedSequence",
                        str(path),
                    ),
                    text=True,
                    capture_output=True,
                )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_direct_mangles_systemverilog_join_instance_identifier(self):
        result = compile_source(
            """
            module Child { in x:u8 out y:u8 y=x }
            module ReservedJoin {
                in x:u8
                out y:u8
                inst join:Child { x }
                y=join.y
            }
            """,
            top="ReservedJoin",
            include_clash=False,
        )
        text = emit_experimental(result.ir)
        self.assertIn("Child_s", text)
        self.assertRegex(text, r"\bzlang_join \(")
        self.assertNotRegex(text, r"\n\s+join \(")
        verilator = shutil.which("verilator")
        if verilator:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "ReservedJoin.sv"
                path.write_text(text)
                completed = subprocess.run(
                    (
                        verilator,
                        "--lint-only",
                        "-Wall",
                        "-Wno-DECLFILENAME",
                        "--top-module",
                        "ReservedJoin",
                        str(path),
                    ),
                    text=True,
                    capture_output=True,
                )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_direct_and_clash_artifacts_have_real_formal_smoke(self):
        result = self.compile("alu.zhl")
        wrapper = """
module M37Formal(input [31:0] a, input [31:0] b, input [2:0] op);
  wire [31:0] y;
  ALU dut(.a(a), .b(b), .op(op), .y(y));
  always @* assert(y == ((op == 0) ? (a+b) : ((op == 1) ? (a-b) :
                    ((op == 2) ? (a&b) : ((op == 3) ? (a|b) : 0)))));
endmodule
"""
        direct = run_verilog_formal(emit_sv_artifact(result.ir).text + wrapper,
                                     top="M37Formal", property_id="m37.direct.alu",
                                     systemverilog=True)
        self.assertNotEqual(direct.status, FormalStatus.FAILED)
        clash_executable = find_clash_executable()
        if clash_executable:
            with tempfile.TemporaryDirectory() as directory:
                files = generate_verilog(result.clash, "ALU", Path(directory), clash_executable)
                clash_text = "\n".join(path.read_text() for path in files)
                clash = run_verilog_formal(clash_text + wrapper, top="M37Formal",
                                            property_id="m37.clash.alu")
                self.assertNotEqual(clash.status, FormalStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
