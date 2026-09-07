import unittest

from zlang.ast.nodes import MemoryCollision, MemoryResetPolicy
from zlang.parser import ParseError, parse


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
        self.assertIs(
            module.memories[0].contents_reset, MemoryResetPolicy.CLEAR
        )
        self.assertIs(
            module.memories[0].read_data_reset, MemoryResetPolicy.CLEAR
        )

    def test_memory_reset_policies_and_zero_latency_parse(self) -> None:
        module = parse(
            """
            module Storage {
              clock clk reset rst
              memory table: mem<u16, 16> {
                read_latency 0
                collision read_first
                reset {
                  contents preserve
                  read_data clear
                }
              }
            }
            """
        )
        memory = module.memories[0]
        self.assertEqual(memory.read_latency, 0)
        self.assertIs(memory.contents_reset, MemoryResetPolicy.PRESERVE)
        self.assertIs(memory.read_data_reset, MemoryResetPolicy.CLEAR)

        hexadecimal = parse(
            "module Hex{clock c reset r memory m:mem<u8,2>{"
            "read_latency 0x0 collision read_first}}"
        )
        self.assertEqual(hexadecimal.memories[0].read_latency, 0)

    def test_memory_reset_block_requires_both_directives_in_order(self) -> None:
        sources = (
            "memory m:mem<u8,2>{read_latency 1 collision read_first "
            "reset{contents preserve}}",
            "memory m:mem<u8,2>{read_latency 1 collision read_first "
            "reset{read_data preserve}}",
            "memory m:mem<u8,2>{read_latency 1 collision read_first "
            "reset{read_data preserve contents clear}}",
            "memory m:mem<u8,2>{read_latency 1 collision read_first "
            "reset{contents clear read_data preserve} "
            "reset{contents clear read_data clear}}",
        )
        for declaration in sources:
            with self.subTest(declaration=declaration):
                with self.assertRaises(ParseError):
                    parse(f"module Bad{{clock c reset r {declaration}}}")


if __name__ == "__main__":
    unittest.main()
