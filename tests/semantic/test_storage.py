import unittest

from zlang.ir import expressions as expr
from zlang.ir.storage import FifoSignal, MemoryCollision, MemorySignal
from zlang.ir.types import BitType, UIntType
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


FIFO_SOURCE = """
module FifoStatus {
  clock clk
  reset rst
  in data: u8
  in push: bit
  in pop: bit
  out front: u8
  out count: u3
  out ready: bit
  fifo queue: fifo<u8, 4>
  queue.data = data
  queue.push = push
  queue.pop = pop
  front = queue.front
  count = queue.count
  ready = queue.ready
}
"""


MEMORY_SOURCE = """
module MemoryStatus {
  clock clk
  reset rst
  in read_address: u4
  in write_enable: bit
  in write_address: u4
  in write_data: u8
  out read_data: u8
  memory table: mem<u8, 16> {
    read_latency 1
    collision write_first
  }
  table.read_address = read_address
  table.write_enable = write_enable
  table.write_address = write_address
  table.write_data = write_data
  read_data = table.read_data
}
"""


class StorageSemanticTests(unittest.TestCase):
    def test_fifo_controls_and_status_references_are_typed(self) -> None:
        module = analyze(parse(FIFO_SOURCE))
        fifo = module.fifos[0]

        self.assertEqual(fifo.element_type, UIntType(8))
        self.assertEqual(fifo.depth, 4)
        self.assertEqual(fifo.count_width, 3)
        self.assertEqual(fifo.push.type, BitType())
        references = {
            assignment.target.name: assignment.expression
            for assignment in module.assignments
        }
        self.assertEqual(
            references["front"],
            expr.FifoRef("queue", FifoSignal.FRONT, UIntType(8)),
        )
        self.assertEqual(
            references["count"],
            expr.FifoRef("queue", FifoSignal.COUNT, UIntType(3)),
        )

    def test_memory_latency_collision_and_addresses_are_typed(self) -> None:
        module = analyze(parse(MEMORY_SOURCE))
        memory = module.memories[0]

        self.assertEqual(memory.element_type, UIntType(8))
        self.assertEqual(memory.address_width, 4)
        self.assertEqual(memory.read_latency, 1)
        self.assertIs(memory.collision, MemoryCollision.WRITE_FIRST)
        self.assertEqual(memory.read_address.type, UIntType(4))
        self.assertEqual(
            module.assignments[0].expression,
            expr.MemoryRef("table", MemorySignal.READ_DATA, UIntType(8)),
        )

    def test_storage_configuration_and_controls_are_validated(self) -> None:
        cases = {
            "requires a clock and reset": """
                module Bad { in d:u8 in p:bit fifo q:fifo<u8,2>
                  q.data=d q.push=p q.pop=0 }
            """,
            "has no 'pop' control assignment": """
                module Bad { clock c reset r in d:u8 in p:bit
                  fifo q:fifo<u8,2> q.data=d q.push=p }
            """,
            "assigned more than once": """
                module Bad { clock c reset r in d:u8 in p:bit
                  fifo q:fifo<u8,2> q.data=d q.data=d q.push=p q.pop=0 }
            """,
            "is read-only": """
                module Bad { clock c reset r in d:u8 in p:bit
                  fifo q:fifo<u8,2> q.data=d q.push=p q.pop=0 q.full=p }
            """,
            "depth must be a power of two": """
                module Bad { clock c reset r in a:u2 in we:bit in d:u8
                  memory m:mem<u8,3>{read_latency 1 collision read_first}
                  m.read_address=a m.write_enable=we m.write_address=a m.write_data=d }
            """,
            "currently requires read_latency 1": """
                module Bad { clock c reset r in a:u2 in we:bit in d:u8
                  memory m:mem<u8,4>{read_latency 2 collision read_first}
                  m.read_address=a m.write_enable=we m.write_address=a m.write_data=d }
            """,
            "push and pop controls must be bit": """
                module Bad { clock c reset r in d:u8
                  fifo q:fifo<u8,2> q.data=d q.push=d q.pop=0 }
            """,
        }
        for message, source in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(SemanticError, message):
                    analyze(parse(source))


if __name__ == "__main__":
    unittest.main()
