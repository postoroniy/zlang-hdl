from pathlib import Path
import unittest

from zlang.ir.csr import CsrBindingKind, CsrPriority
from zlang.ir.expressions import FieldAccess, InputRef
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[2]


class HardwareCsrSemanticTests(unittest.TestCase):
    def test_bindings_and_safe_default_priority_reach_typed_ir(self) -> None:
        module = analyze(parse((ROOT / "examples/engine_csr.zhl").read_text()))
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

    def test_status_and_sticky_bindings_retain_typed_aggregate_leaves(self) -> None:
        module = analyze(parse("""
struct Perf { cycles:u32 }
struct Status { busy:bit perf:Perf }
module AggregateStatusBank {
    clock clk reset rst
    in status:Status
    csr registers @0 {
        STATUS @0 {
            busy bit @0 ro <- status.busy
            error bit @1 w1c <- sticky(status.busy)
        }
        CYCLES @4 { value u32 @31:0 ro <- status.perf.cycles }
    }
}
"""))
        busy = module.csr_blocks[0].registers[0].fields[0].binding
        cycles = module.csr_blocks[0].registers[1].fields[0].binding
        self.assertEqual(busy.signal, "status.busy")
        self.assertIsInstance(busy.source, FieldAccess)
        self.assertIsInstance(busy.source.expression, InputRef)
        self.assertEqual(busy.source.expression.name, "status")
        self.assertIsInstance(cycles.source, FieldAccess)
        self.assertIsInstance(cycles.source.expression, FieldAccess)
        self.assertEqual(cycles.source.field, "cycles")

    def test_aggregate_bindings_fail_closed_for_invalid_leaf_or_command(self) -> None:
        cases = (
            (
                "in status:Status csr x @0 { R @0 { "
                "f bit @0 ro <- status.missing } }",
                "references unknown member 'missing'",
            ),
            (
                "in status:Status csr x @0 { R @0 { "
                "f u2 @1:0 ro <- status.busy } }",
                "hardware signal 'status.busy' has type bit",
            ),
            (
                "out command:Status csr x @0 { R @0 { "
                "f bit @0 pulse -> command.busy } }",
                "command bindings currently require a scalar output port",
            ),
        )
        for body, message in cases:
            source = (
                "struct Status { busy:bit } "
                f"module Bad {{ clock clk reset rst {body} }}"
            )
            with self.subTest(message=message), self.assertRaisesRegex(
                SemanticError, message
            ):
                analyze(parse(source))

    def test_aggregate_binding_rejects_implicit_clock_domain_crossing(self) -> None:
        source = """
struct Status { busy:bit }
module Bad {
    clock control reset control_rst @control
    clock datapath reset datapath_rst @datapath
    in status:Status @datapath
    csr registers @0 @control {
        STATUS @0 { busy bit @0 ro <- status.busy }
    }
}
"""
        with self.assertRaisesRegex(SemanticError, "clock domain"):
            analyze(parse(source))


if __name__ == "__main__":
    unittest.main()
