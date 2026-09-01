import subprocess
import tempfile
import unittest
from pathlib import Path

from zlang import compile_source
from zlang.backend.clash import emit, emit_artifact
from zlang.toolchain import clash_subprocess_environment, find_clash_executable
from zlang.toolchain import lint_with_verilator


SOURCE = """
import std.bus.reg
import std.bus.axi_lite
module AxiCsrTop {
  clock clk reset rst
  interface axi : AXI4Lite<32,32>.slave
  inst frontend : AXI4LiteToRegBus<32,32>
  inst csr : RegBusCSRTarget<32,32>
  connect axi -> frontend.axi
  connect frontend.regbus -> csr.regbus
}
"""


class StandardBusClashTests(unittest.TestCase):
    def test_components_are_emitted_from_source_modules(self):
        result = compile_source(SOURCE)
        children = {child.name: child for child in result.ir.children}
        self.assertEqual(set(children), {"AXI4LiteToRegBus", "RegBusCSRTarget"})
        for name, child in children.items():
            text = emit(child)
            self.assertIn(f"module {name} where", text)
            self.assertNotIn("axi4LiteToRegBus", text)
            self.assertNotIn("apbToRegBus", text)

    def test_source_component_manifests_have_public_bindings(self):
        result = compile_source(SOURCE)
        frontend = next(child for child in result.ir.children if child.name == "AXI4LiteToRegBus")
        artifact = emit_artifact(frontend, selected_ir_identity="std.bus:axi4lite:32:32:source-v1")
        self.assertTrue(artifact.bindings)
        self.assertTrue(any(item.semantic_signal_id.startswith("aggregate:AXI4LiteToRegBus")
                            for item in artifact.bindings))

    @unittest.skipUnless(find_clash_executable(), "Clash unavailable")
    def test_real_clash_generation_from_zl_hierarchy(self):
        result = compile_source(SOURCE)
        exe = find_clash_executable()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "AxiCsrTop.hs"
            output = root / "verilog"
            output.mkdir()
            source.write_text(result.clash)
            completed = subprocess.run(
                [exe, "--verilog", str(source), "-outputdir", str(output)],
                capture_output=True, text=True,
                env=clash_subprocess_environment(exe), check=False,
            )
            self.assertEqual(completed.returncode, 0,
                             completed.stderr or completed.stdout)
            files = tuple(output.rglob("*.v"))
            self.assertTrue(files)
            lint_with_verilator(files, "AxiCsrTop")


if __name__ == "__main__":
    unittest.main()
