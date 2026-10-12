import unittest

from zlang.ir.interfaces import ConnectionAdapter, InterfaceProtocol
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


class ConnectionSemanticTests(unittest.TestCase):
    def test_identical_ready_valid_connection_is_typed_and_expanded(self) -> None:
        module = analyze(
            parse("module Link { in rx:rv<u8> out tx:rv<u8> connect rx -> tx }")
        )
        connection = module.connections[0]
        self.assertEqual(connection.source.protocol, InterfaceProtocol.READY_VALID)
        self.assertEqual(connection.destination.protocol, InterfaceProtocol.READY_VALID)
        self.assertEqual(connection.buffer_depth, 0)
        self.assertEqual(len(module.assignments), 3)

    def test_wire_connection_is_type_checked(self) -> None:
        module = analyze(
            parse("module Link { in x:wire<u8> out y:wire<u8> connect x -> y }")
        )
        self.assertEqual(len(module.assignments), 1)
        with self.assertRaisesRegex(SemanticError, "payload mismatch"):
            analyze(
                parse(
                    "module Bad { in x:wire<u8> out y:wire<u16> connect x -> y }"
                )
            )

    def test_direction_and_adapter_selection_are_explicit(self) -> None:
        with self.assertRaisesRegex(SemanticError, "source 'tx' must be an input"):
            analyze(
                parse(
                    "module Bad { in rx:rv<u8> out tx:rv<u8> connect tx -> rx }"
                )
            )

    def test_buffered_connection_owns_fields_and_requires_clock(self) -> None:
        module = analyze(
            parse(
                "module Buffered { clock c reset r in rx:rv<u8> out tx:rv<u8> "
                "connect rx -> tx { buffer 2 } }"
            )
        )
        self.assertEqual(module.connections[0].buffer_depth, 2)
        self.assertEqual(module.assignments, ())
        with self.assertRaisesRegex(SemanticError, "require a module clock"):
            analyze(
                parse(
                    "module Bad { in rx:rv<u8> out tx:rv<u8> "
                    "connect rx -> tx { buffer 2 } }"
                )
            )

    def test_adapter_capacity_and_buffer_rules_are_diagnosed(self) -> None:
        module = analyze(
            parse(
                "module Convert { clock c reset r in rx:rv<u8> "
                "out tx:credit<u8,2> "
                "connect rx -> tx { adapter rv_to_credit } }"
            )
        )
        self.assertEqual(
            module.connections[0].adapter,
            ConnectionAdapter.READY_VALID_TO_CREDIT,
        )
        with self.assertRaisesRegex(SemanticError, "smaller than source capacity"):
            analyze(
                parse(
                    "module Bad { clock c reset r in rx:credit<u8,4> "
                    "out tx:rv<u8> connect rx -> tx { buffer 2 "
                    "adapter credit_to_rv } }"
                )
            )
        with self.assertRaisesRegex(SemanticError, "equal capacities"):
            analyze(
                parse(
                    "module Bad { clock c reset r in rx:credit<u8,2> "
                    "out tx:credit<u8,3> connect rx -> tx }"
                )
            )
        with self.assertRaisesRegex(SemanticError, "requires adapter rv_to_credit"):
            analyze(
                parse(
                    "module Bad { clock c reset r in rx:rv<u8> "
                    "out tx:credit<u8,2> connect rx -> tx }"
                )
            )
        with self.assertRaisesRegex(
            SemanticError, "credit_to_rv requires an explicit buffer depth"
        ):
            analyze(
                parse(
                    "module Bad { clock c reset r in rx:credit<u8,2> "
                    "out tx:rv<u8> connect rx -> tx { adapter credit_to_rv } }"
                )
            )


if __name__ == "__main__":
    unittest.main()
