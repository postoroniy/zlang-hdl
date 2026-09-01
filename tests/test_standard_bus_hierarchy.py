import subprocess
import tempfile
import unittest
import os
from pathlib import Path

from zlang import compile_source
from zlang.semantic import SemanticError
from zlang.backend.clash import emit_artifact
from zlang.toolchain import clash_subprocess_environment, find_clash_executable, lint_with_verilator

AXI = """import std.bus.reg\nimport std.bus.axi_lite\nmodule AxiCsrTop { clock clk reset rst interface axi : AXI4Lite<32,32>.slave inst frontend : AXI4LiteToRegBus<32,32> inst csr : RegBusCSRTarget<32,32> connect axi -> frontend.axi connect frontend.regbus -> csr.regbus out done:bit done=csr.done }"""
APB = """import std.bus.reg\nimport std.bus.apb\nmodule ApbCsrTop { clock clk reset rst interface apb : APB<32,32>.slave inst frontend : APBToRegBus<32,32> inst csr : RegBusCSRTarget<32,32> connect apb -> frontend.apb connect frontend.regbus -> csr.regbus out done:bit done=csr.done }"""

class StandardBusHierarchyTests(unittest.TestCase):
    def test_imported_instances_and_specializations(self):
        result = compile_source(AXI)
        self.assertEqual([item.module for item in result.ir.instances], ["AXI4LiteToRegBus", "RegBusCSRTarget"])
        self.assertEqual([item.specializations for item in result.ir.instances][0][0].value, 32)
        self.assertEqual(len(result.ir.children), 2)

    def test_library_instance_requires_import(self):
        with self.assertRaisesRegex(SemanticError, "unknown instantiated module"):
            compile_source("module T { inst frontend : AXI4LiteToRegBus<32,32> }")

    def test_top_manifest_bindings(self):
        artifact = emit_artifact(compile_source(AXI).ir)
        ids = {item.semantic_signal_id for item in artifact.bindings}
        self.assertIn("aggregate:AxiCsrTop.axi.aw.valid", ids)
        self.assertIn("aggregate:AxiCsrTop.axi.r.payload.resp", ids)

    @unittest.skipUnless(find_clash_executable(), "Clash unavailable")
    def test_axi_and_apb_source_level_clash_and_lint(self):
        for source, name in ((AXI, "AxiCsrTop"), (APB, "ApbCsrTop")):
            result = compile_source(source)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source_path = root / f"{name}.hs"
                output = root / "verilog"
                output.mkdir()
                source_path.write_text(result.clash)
                exe = find_clash_executable()
                completed = subprocess.run((exe, "--verilog", str(source_path), "-outputdir", str(output)), env=clash_subprocess_environment(exe), capture_output=True, text=True)
                self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
                files = tuple(output.rglob("*.v"))
                self.assertTrue(files)
                lint_with_verilator(files, name)

    @unittest.skipUnless(find_clash_executable(), "Clash unavailable")
    def test_source_level_verilator_simulation_reset(self):
        cases = ((AXI, "AxiCsrTop", [32,1,32,4,1,1,32,1,1], [1,1,1,2,1,1,32,2,1]),
                 (APB, "ApbCsrTop", [1,1,1,32,32], [1,1,32,1]))
        for source, name, in_widths, out_widths in cases:
            result = compile_source(source)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                hs = root / f"{name}.hs"
                output = root / "verilog"
                output.mkdir()
                hs.write_text(result.clash)
                exe = find_clash_executable()
                completed = subprocess.run((exe, "--verilog", str(hs), "-outputdir", str(output)), env=clash_subprocess_environment(exe), capture_output=True, text=True)
                self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
                rtl = next(output.rglob("*.v"))
                decl = "reg clk=0; reg rst=1; " + " ".join(f"reg [ {w-1}:0 ] i{i}=0;" if w > 1 else f"reg i{i}=0;" for i,w in enumerate(in_widths)) + " " + " ".join(f"wire [ {w-1}:0 ] o{i};" if w > 1 else f"wire o{i};" for i,w in enumerate(out_widths))
                ports = ",".join(["clk", "rst"] + [f"i{i}" for i in range(len(in_widths))] + [f"o{i}" for i in range(len(out_widths))])
                tb = root / "tb.sv"
                tb.write_text(f"module tb; {decl} always #1 clk=~clk; {name} dut({ports}); initial begin #5 rst=0; #15 $finish; end endmodule\n")
                env = os.environ.copy()
                env["CCACHE_DISABLE"] = "1"
                built = subprocess.run(("verilator", "--binary", "--timing", "--top-module", "tb", str(rtl), str(tb)), cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(built.returncode, 0, built.stderr or built.stdout)
                sim = subprocess.run((str(root / "obj_dir" / "Vtb"),), cwd=root, capture_output=True, text=True)
                self.assertEqual(sim.returncode, 0, sim.stderr or sim.stdout)
