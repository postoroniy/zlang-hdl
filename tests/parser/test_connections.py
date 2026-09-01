import unittest

from zlang.ast.nodes import ConnectionAdapter
from zlang.parser import parse


class ConnectionParserTests(unittest.TestCase):
    def test_direct_connection_parses(self) -> None:
        connection = parse(
            "module Link { in rx:rv<u8> out tx:rv<u8> connect rx -> tx }"
        ).connections[0]
        self.assertEqual((connection.source, connection.destination), ("rx", "tx"))
        self.assertEqual(connection.buffer_depth, 0)
        self.assertIsNone(connection.adapter)

    def test_buffer_and_explicit_adapter_are_preserved(self) -> None:
        connection = parse(
            "module Link { in rx:rv<u8> out tx:credit<u8,2> "
            "connect rx -> tx { buffer 2 adapter rv_to_credit } }"
        ).connections[0]
        self.assertEqual(connection.buffer_depth, 2)
        self.assertEqual(
            connection.adapter, ConnectionAdapter.READY_VALID_TO_CREDIT
        )


if __name__ == "__main__":
    unittest.main()
