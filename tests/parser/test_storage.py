import unittest

from zlang.ast.nodes import MemoryCollision
from zlang.parser import parse


class StorageParserTests(unittest.TestCase):
    def test_fifo_and_memory_options_parse(self) -> None:
        module = parse(
            """
            module Storage {
              clock clk
              reset rst
              fifo queue: fifo<u8, 4>
              memory table: mem<u16, 16> {
                read_latency 1
                collision write_first
              }
            }
            """
        )

        self.assertEqual(module.fifos[0].name, "queue")
        self.assertEqual(module.fifos[0].depth, 4)
        self.assertEqual(module.fifos[0].element_type.text, "u8")
        self.assertEqual(module.memories[0].name, "table")
        self.assertEqual(module.memories[0].depth, 16)
        self.assertEqual(module.memories[0].read_latency, 1)
        self.assertIs(module.memories[0].collision, MemoryCollision.WRITE_FIRST)


if __name__ == "__main__":
    unittest.main()
