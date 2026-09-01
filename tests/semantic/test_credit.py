from pathlib import Path
import unittest

from zlang.ir.expressions import CreditRef
from zlang.ir.interfaces import CreditSignal, InterfaceProtocol
from zlang.ir.types import BitType, UIntType
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[2]


class CreditSemanticTests(unittest.TestCase):
    def test_sender_capacity_and_fields_survive_in_typed_ir(self) -> None:
        module = analyze(parse((ROOT / "examples/credit_source.zl").read_text()))
        tx = module.ports[2]
        self.assertEqual(tx.protocol, InterfaceProtocol.CREDIT)
        self.assertEqual(tx.capacity, 2)
        self.assertEqual(
            [assignment.signal for assignment in module.assignments],
            [CreditSignal.PAYLOAD, CreditSignal.SEND],
        )

    def test_transfer_and_sender_credit_count_are_typed_read_only_values(self) -> None:
        module = analyze(
            parse(
                "module Observe { clock c reset r in request:bit "
                "out tx:credit<u8,8> out fired:bit out remaining:u4 "
                "tx.payload=0 tx.send=request fired=tx.transfer "
                "remaining=tx.credits }"
            )
        )
        transfer = module.assignments[2].expression
        credits = module.assignments[3].expression
        self.assertIsInstance(transfer, CreditRef)
        self.assertEqual(transfer.signal, CreditSignal.TRANSFER)
        self.assertEqual(transfer.type, BitType())
        self.assertIsInstance(credits, CreditRef)
        self.assertEqual(credits.signal, CreditSignal.CREDITS)
        self.assertEqual(credits.type, UIntType(4))

        for field in ("transfer", "credits"):
            with self.subTest(field=field), self.assertRaisesRegex(
                SemanticError, "read-only"
            ):
                analyze(
                    parse(
                        "module Bad { clock c reset r out tx:credit<u8,2> "
                        f"tx.payload=0 tx.send=0 tx.{field}=0 }}"
                    )
                )

    def test_credit_direction_and_required_fields_are_checked(self) -> None:
        invalid_sources = (
            (
                "module Bad { clock c reset r out tx:credit<u8,2> "
                "tx.payload=0 tx.send=0 tx.return=0 }",
                "cannot drive incoming credit field 'tx.return'",
            ),
            (
                "module Bad { clock c reset r in rx:credit<u8,2> "
                "rx.return=0 rx.send=0 }",
                "cannot drive incoming credit field 'rx.send'",
            ),
            (
                "module Bad { clock c reset r out tx:credit<u8,2> tx.payload=0 }",
                "tx.send.*has no assignment",
            ),
            (
                "module Bad { clock c reset r in rx:credit<u8,2> }",
                "rx.return.*has no assignment",
            ),
        )
        for source, diagnostic in invalid_sources:
            with self.subTest(diagnostic=diagnostic), self.assertRaisesRegex(
                SemanticError, diagnostic
            ):
                analyze(parse(source))

    def test_credit_requires_clock_and_reset(self) -> None:
        with self.assertRaisesRegex(
            SemanticError, "credit interfaces require a module clock and reset"
        ):
            analyze(
                parse(
                    "module Bad { out tx:credit<u8,2> tx.payload=0 tx.send=0 }"
                )
            )

    def test_receiver_does_not_expose_sender_credit_count(self) -> None:
        with self.assertRaisesRegex(
            SemanticError, "does not own a sender credit count"
        ):
            analyze(
                parse(
                    "module Bad { clock c reset r in rx:credit<u8,2> "
                    "out count:u2 rx.return=0 count=rx.credits }"
                )
            )

    def test_credit_dependency_cycle_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            SemanticError, "combinational credit dependency cycle"
        ):
            analyze(
                parse(
                    "module Bad { clock c reset r out tx:credit<u8,2> "
                    "tx.payload=0 tx.send=tx.transfer }"
                )
            )


if __name__ == "__main__":
    unittest.main()
