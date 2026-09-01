import unittest

from zlang.compiler import compile_source
from zlang.simulate import ProtocolViolation, simulate_cycles


SCALAR_FIFO = """
module ScalarFifo {
  clock clk
  reset rst
  in data: u8
  in push: bit
  in pop: bit
  out front: u8
  out count: u2
  out ready: bit
  out valid: bit
  out full: bit
  out empty: bit
  fifo queue: fifo<u8, 2>
  queue.data = data
  queue.push = push
  queue.pop = pop
  front = queue.front
  count = queue.count
  ready = queue.ready
  valid = queue.valid
  full = queue.full
  empty = queue.empty
}
"""


def memory_source(collision: str) -> str:
    return f"""
    module MemoryModel {{
      clock clk
      reset rst
      in read_address: u2
      in write_enable: bit
      in write_address: u2
      in write_data: u8
      out read_data: u8
      memory table: mem<u8, 4> {{
        read_latency 1
        collision {collision}
      }}
      table.read_address = read_address
      table.write_enable = write_enable
      table.write_address = write_address
      table.write_data = write_data
      read_data = table.read_data
    }}
    """


class StorageIntegrationTests(unittest.TestCase):
    def test_fifo_boundaries_order_and_full_simultaneous_transfer(self) -> None:
        module = compile_source(SCALAR_FIFO).ir
        results = simulate_cycles(
            module,
            [
                {"data": 0, "push": 0, "pop": 0},
                {"data": 11, "push": 1, "pop": 0},
                {"data": 22, "push": 1, "pop": 0},
                {"data": 33, "push": 1, "pop": 1},
                {"data": 0, "push": 0, "pop": 1},
                {"data": 0, "push": 0, "pop": 1},
            ],
            reset=[True, False, False, False, False, False],
        )

        self.assertEqual(results[0]["empty"], 1)
        self.assertEqual(results[1]["count"], 0)
        self.assertEqual(results[2]["front"], 11)
        self.assertEqual(results[3]["full"], 1)
        self.assertEqual(results[3]["ready"], 1)
        self.assertEqual(results[3]["front"], 11)
        self.assertEqual(results[4]["front"], 22)
        self.assertEqual(results[5]["front"], 33)

    def test_fifo_reset_overflow_and_underflow_are_enforced(self) -> None:
        module = compile_source(SCALAR_FIFO).ir
        with self.assertRaisesRegex(ProtocolViolation, "underflow"):
            simulate_cycles(
                module,
                [{"data": 0, "push": 0, "pop": 1}],
            )
        with self.assertRaisesRegex(ProtocolViolation, "overflow"):
            simulate_cycles(
                module,
                [
                    {"data": 1, "push": 1, "pop": 0},
                    {"data": 2, "push": 1, "pop": 0},
                    {"data": 3, "push": 1, "pop": 0},
                ],
            )
        reset_results = simulate_cycles(
            module,
            [
                {"data": 1, "push": 1, "pop": 0},
                {"data": 0, "push": 0, "pop": 0},
            ],
            reset=[False, True],
        )
        self.assertEqual(reset_results[1]["count"], 0)
        self.assertEqual(reset_results[1]["valid"], 0)

    def test_fifo_ready_valid_bridge_preserves_order_under_backpressure(self) -> None:
        module = compile_source(
            """
            module Bridge { clock c reset r in rx:rv<u8> out tx:rv<u8>
              fifo q:fifo<u8,2>
              q.data=rx.payload q.push=rx.transfer q.pop=tx.transfer
              rx.ready=q.ready tx.payload=q.front tx.valid=q.valid }
            """
        ).ir
        results = simulate_cycles(
            module,
            [
                {"rx": {"payload": 0, "valid": 0}, "tx": {"ready": 0}},
                {"rx": {"payload": 7, "valid": 1}, "tx": {"ready": 0}},
                {"rx": {"payload": 9, "valid": 1}, "tx": {"ready": 0}},
                {"rx": {"payload": 12, "valid": 1}, "tx": {"ready": 1}},
                {"rx": {"payload": 0, "valid": 0}, "tx": {"ready": 1}},
            ],
            reset=[True, False, False, False, False],
        )
        self.assertEqual(results[2]["tx"]["payload"], 7)
        self.assertEqual(results[3]["tx"]["payload"], 7)
        self.assertEqual(results[3]["rx"]["ready"], 1)
        self.assertEqual(results[4]["tx"]["payload"], 9)

    def test_memory_read_latency_and_collision_modes_are_explicit(self) -> None:
        cycles = [
            {"read_address": 0, "write_enable": 0, "write_address": 0, "write_data": 0},
            {"read_address": 1, "write_enable": 1, "write_address": 1, "write_data": 9},
            {"read_address": 1, "write_enable": 0, "write_address": 0, "write_data": 0},
            {"read_address": 0, "write_enable": 0, "write_address": 0, "write_data": 0},
        ]
        read_first = simulate_cycles(
            compile_source(memory_source("read_first")).ir,
            cycles,
            reset=[True, False, False, False],
        )
        write_first = simulate_cycles(
            compile_source(memory_source("write_first")).ir,
            cycles,
            reset=[True, False, False, False],
        )

        self.assertEqual([item["read_data"] for item in read_first], [0, 0, 0, 9])
        self.assertEqual([item["read_data"] for item in write_first], [0, 0, 9, 9])


if __name__ == "__main__":
    unittest.main()
