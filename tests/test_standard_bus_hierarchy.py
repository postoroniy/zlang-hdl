import subprocess
import tempfile
import unittest
import os
from pathlib import Path

from zlang import compile_source
from zlang.backend.systemverilog import emit_artifact
from zlang.semantic import SemanticError
from zlang.toolchain import lint_with_verilator

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
