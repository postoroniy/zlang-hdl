from pathlib import Path
import unittest

from zlang.ast.nodes import InterfaceKind, InterfaceTypeName
from zlang.parser import parse


ROOT = Path(__file__).resolve().parents[2]


class CreditParserTests(unittest.TestCase):
    def test_credit_payload_and_capacity_are_preserved(self) -> None:
        module = parse((ROOT / "examples/credit_source.zl").read_text())
        credit_type = module.ports[2].type_name
        self.assertIsInstance(credit_type, InterfaceTypeName)
        self.assertEqual(credit_type.kind, InterfaceKind.CREDIT)
        self.assertEqual(credit_type.payload_type.text, "u8")
        self.assertEqual(credit_type.capacity, 2)
        self.assertEqual(
            [assignment.target for assignment in module.assignments],
            ["tx.payload", "tx.send"],
        )

    def test_credit_accepts_aggregate_payload(self) -> None:
        module = parse(
            "struct Packet { data:u8 } module Source { clock c reset r "
            "out tx:credit<Packet,8> tx.payload=0 tx.send=0 }"
        )
        credit_type = module.ports[0].type_name
        self.assertEqual(credit_type.capacity, 8)
        self.assertEqual(credit_type.payload_type.text, "Packet")


if __name__ == "__main__":
    unittest.main()
