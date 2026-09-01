from pathlib import Path
import unittest

from zlang.ir.csr import CsrBindingKind, CsrPriority
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[2]


class HardwareCsrSemanticTests(unittest.TestCase):
    def test_bindings_and_safe_default_priority_reach_typed_ir(self) -> None:
        module = analyze(parse((ROOT / "examples/engine_csr.zl").read_text()))
        control, status = module.csr_blocks[0].registers
        self.assertEqual(control.fields[0].binding.kind, CsrBindingKind.COMMAND)
        self.assertEqual(control.fields[0].binding.signal, "engine_start")
        self.assertEqual(status.fields[0].binding.signal, "engine_busy")
        self.assertEqual(status.fields[1].binding.kind, CsrBindingKind.STICKY)
        self.assertEqual(status.fields[1].binding.priority, CsrPriority.HARDWARE)

    def test_binding_access_direction_type_and_name_are_checked(self) -> None:
        cases = (
            (
                "in s:bit csr x @0 { R @0 { f bit rw <- s } }",
                "status bindings require ro",
            ),
            (
                "out s:bit csr x @0 { R @0 { f bit ro <- s } }",
                "status signal 's' must be an input",
            ),
            (
                "in s:u2 csr x @0 { R @0 { f bit ro <- s } }",
                "hardware signal 's' has type u2",
            ),
            (
                "csr x @0 { R @0 { f bit ro <- missing } }",
                "unknown hardware signal 'missing'",
            ),
            (
                "in e:bit csr x @0 { R @0 { f bit ro <- sticky(e) } }",
                "sticky CSR bindings require w1c",
            ),
            (
                "in c:bit csr x @0 { R @0 { f bit pulse -> c } }",
                "command signal 'c' must be an output",
            ),
        )
        for body, message in cases:
            source = f"module Bad {{ clock clk reset rst {body} }}"
            with self.subTest(message=message), self.assertRaisesRegex(
                SemanticError, message
            ):
                analyze(parse(source))


if __name__ == "__main__":
    unittest.main()
