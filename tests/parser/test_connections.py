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

    def test_connection_retains_exact_endpoint_name_spans(self) -> None:
        connection = parse(
            "module Link { in rx:rv<u8> out tx:rv<u8> "
            "child:Stage rx -> child.tx }"
        ).connections[0]
        self.assertEqual(
            [
                (item.start_line, item.start_column, item.end_column)
                for item in connection.source_name_origins
                if item is not None
            ],
            [(1, 54, 56)],
        )
        self.assertEqual(
            [
                (item.start_line, item.start_column, item.end_column)
                for item in connection.destination_name_origins
                if item is not None
            ],
            [(1, 60, 65), (1, 66, 68)],
        )

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
