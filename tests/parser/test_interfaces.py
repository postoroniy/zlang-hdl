from pathlib import Path
import unittest

from zlang.ast.nodes import FieldExpr, InterfaceKind, InterfaceTypeName, NameExpr
from zlang.parser import parse


ROOT = Path(__file__).resolve().parents[2]


class InterfaceParserTests(unittest.TestCase):
    def test_wire_and_ready_valid_port_types_are_preserved(self) -> None:
        module = parse(
            "module Ports { in a:wire<u8> in rx:rv<u16> out y:wire<u8> "
            "out tx:rv<u16> y=a tx.payload=rx.payload "
            "tx.valid=rx.valid rx.ready=tx.ready }"
        )

        self.assertEqual(
            [port.type_name.kind for port in module.ports],
            [
                InterfaceKind.WIRE,
                InterfaceKind.READY_VALID,
                InterfaceKind.WIRE,
                InterfaceKind.READY_VALID,
            ],
        )
        self.assertTrue(
            all(isinstance(port.type_name, InterfaceTypeName) for port in module.ports)
        )
        self.assertEqual(
            [assignment.target for assignment in module.assignments],
            ["y", "tx.payload", "tx.valid", "rx.ready"],
        )
        self.assertEqual(
            module.assignments[1].expression,
            FieldExpr(NameExpr("rx"), "payload"),
        )

    def test_ready_valid_example_parses(self) -> None:
        module = parse((ROOT / "examples/rv_passthrough.zhl").read_text())
        self.assertEqual(module.name, "RvPassthrough")
        self.assertEqual(len(module.assignments), 3)


if __name__ == "__main__":
    unittest.main()
