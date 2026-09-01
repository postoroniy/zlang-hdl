from pathlib import Path
import unittest

from zlang.ir.expressions import ReadyValidRef
from zlang.ir.interfaces import InterfaceProtocol, ReadyValidSignal
from zlang.ir.module import PortDirection
from zlang.ir.types import BitType, UIntType
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[2]


class InterfaceSemanticTests(unittest.TestCase):
    def test_ready_valid_direction_and_signals_survive_in_typed_ir(self) -> None:
        module = analyze(parse((ROOT / "examples/rv_passthrough.zl").read_text()))

        rx, tx = module.ports
        self.assertEqual(rx.direction, PortDirection.INPUT)
        self.assertEqual(tx.direction, PortDirection.OUTPUT)
        self.assertEqual(rx.protocol, InterfaceProtocol.READY_VALID)
        self.assertEqual(tx.protocol, InterfaceProtocol.READY_VALID)
        self.assertEqual(rx.type, UIntType(8))
        self.assertEqual(
            [assignment.signal for assignment in module.assignments],
            [
                ReadyValidSignal.PAYLOAD,
                ReadyValidSignal.VALID,
                ReadyValidSignal.READY,
            ],
        )
        self.assertTrue(
            all(
                isinstance(assignment.expression, ReadyValidRef)
                for assignment in module.assignments
            )
        )

    def test_wire_interface_is_type_transparent(self) -> None:
        module = analyze(parse("module Wire { in x:wire<u8> out y:wire<u8> y=x }"))
        self.assertEqual(
            [port.protocol for port in module.ports],
            [InterfaceProtocol.WIRE, InterfaceProtocol.WIRE],
        )

    def test_transfer_is_a_read_only_bit(self) -> None:
        module = analyze(
            parse(
                "module Transfer { in rx:rv<u8> in enable:bit out fired:bit "
                "rx.ready=enable fired=rx.transfer }"
            )
        )
        transfer = module.assignments[1].expression
        self.assertIsInstance(transfer, ReadyValidRef)
        self.assertEqual(transfer.signal, ReadyValidSignal.TRANSFER)
        self.assertEqual(transfer.type, BitType())

        with self.assertRaisesRegex(SemanticError, "transfer.*read-only"):
            analyze(
                parse(
                    "module Bad { in rx:rv<u8> rx.ready=1 rx.transfer=1 }"
                )
            )

    def test_protocol_direction_is_enforced(self) -> None:
        invalid_sources = (
            (
                "module Bad { in rx:rv<u8> rx.payload=0 rx.ready=1 }",
                "cannot drive incoming ready/valid field 'rx.payload'",
            ),
            (
                "module Bad { out tx:rv<u8> tx.ready=1 tx.payload=0 tx.valid=0 }",
                "cannot drive incoming ready/valid field 'tx.ready'",
            ),
            (
                "module Bad { out tx:rv<u8> tx=0 }",
                "must be assigned by field",
            ),
        )
        for source, diagnostic in invalid_sources:
            with self.subTest(diagnostic=diagnostic), self.assertRaisesRegex(
                SemanticError, diagnostic
            ):
                analyze(parse(source))

    def test_required_fields_and_payload_types_are_checked(self) -> None:
        with self.assertRaisesRegex(SemanticError, "tx.valid.*has no assignment"):
            analyze(parse("module Bad { out tx:rv<u8> tx.payload=0 }"))
        with self.assertRaisesRegex(
            SemanticError, "cannot assign bit expression to u8 output 'tx.payload'"
        ):
            analyze(
                parse(
                    "module Bad { in rx:rv<u8> out tx:rv<u8> "
                    "rx.ready=tx.ready tx.payload=rx.valid tx.valid=rx.valid }"
                )
            )

    def test_ready_valid_sequential_top_uses_ordinary_typed_semantics(self) -> None:
        module = analyze(
            parse(
                "module StatefulTop { clock clk reset rst "
                "in rx:rv<u8> out count:u8 reg r:u8=0 "
                "rx.ready=1 count=r when rx.transfer { r <- rx.payload } }"
            )
        )
        self.assertEqual(module.name, "StatefulTop")
        self.assertEqual(tuple(register.name for register in module.registers), ("r",))
        self.assertIsNotNone(module.resolved_transition)

    def test_combinational_protocol_dependency_cycle_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            SemanticError, "combinational ready/valid dependency cycle"
        ):
            analyze(
                parse(
                    "module Loop { in rx:rv<u8> out tx:rv<u8> "
                    "tx.payload=rx.payload tx.valid=rx.ready "
                    "rx.ready=tx.valid }"
                )
            )


if __name__ == "__main__":
    unittest.main()
