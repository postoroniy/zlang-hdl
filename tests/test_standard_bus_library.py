import unittest

from zlang.parser import parse
from zlang.semantic import analyze, SemanticError
from zlang.standard_bus import Axi4LiteToRegBus, AxiLiteInput, AxiAw, AxiW, ApbToRegBus, ApbInput, RegResponse

class StandardBusLibraryTests(unittest.TestCase):
    def test_std_import_and_schema_identity(self):
        ir = analyze(parse("import std.bus.axi_lite module Top { in x : bit out y : bit y = x }"))
        self.assertEqual(ir.protocol_schemas[0].library_path, "std.bus.axi_lite")
        self.assertEqual(ir.protocol_schemas[0].name, "AXI4Lite")

    def test_import_diagnostics(self):
        with self.assertRaisesRegex(SemanticError, "unknown standard"):
            analyze(parse("import std.bus.nope module Top { in x : bit out y : bit y = x }"))
        with self.assertRaisesRegex(SemanticError, "duplicate import"):
            analyze(parse("import std.bus.reg import std.bus.reg module Top { in x : bit out y : bit y = x }"))

    def test_axi_aw_w_interleavings(self):
        regs = {}
        def access(r):
            if r.write:
                regs[r.addr] = r.wdata
                return RegResponse()
            return RegResponse(regs.get(r.addr, 0))
        bridge = Axi4LiteToRegBus(access)
        bridge.step(AxiLiteInput(reset=True))
        bridge.step(AxiLiteInput(aw_valid=True, aw=AxiAw(4)))
        self.assertTrue(bridge.step(AxiLiteInput(w_valid=True, w=AxiW(0x55))).b_valid)
        bridge.step(AxiLiteInput(b_ready=True))
        bridge.step(AxiLiteInput(w_valid=True, w=AxiW(0x66)))
        self.assertTrue(bridge.step(AxiLiteInput(aw_valid=True, aw=AxiAw(8))).b_valid)

    def test_axi_payload_held_and_read(self):
        bridge = Axi4LiteToRegBus(lambda r: RegResponse(0x1234) if not r.write else RegResponse())
        bridge.step(AxiLiteInput(reset=True))
        out = bridge.step(AxiLiteInput(ar_valid=True, ar=AxiAw(0)))
        self.assertTrue(out.r_valid)
        held = out.r
        self.assertEqual(bridge.step(AxiLiteInput(r_ready=False)).r, held)
        bridge.step(AxiLiteInput(r_ready=True))

    def test_apb_wait_and_completion(self):
        bridge = ApbToRegBus(lambda r: RegResponse(0xA5))
        bridge.step(ApbInput(reset=True))
        bridge.step(ApbInput(psel=True, penable=False, paddr=4))
        self.assertEqual(bridge.phase, "ACCESS")
        bridge.step(ApbInput(psel=True, penable=True, paddr=4, pready=False))
        self.assertEqual(bridge.phase, "ACCESS")
        out = bridge.step(ApbInput(psel=True, penable=True, paddr=4, pready=True))
        self.assertTrue(out.pready)
        self.assertEqual(bridge.phase, "IDLE")
