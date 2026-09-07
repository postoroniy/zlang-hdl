from pathlib import Path
import unittest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


class ClashStorageTests(unittest.TestCase):
    def test_fifo_bridge_emits_explicit_count_slots_and_handshakes(self) -> None:
        generated = compile_source(
            (ROOT / "examples/fifo_bridge.zhl").read_text()
        ).clash

        self.assertEqual(
            generated, (ROOT / "examples/generated/FifoBridge.hs").read_text()
        )
        self.assertIn("queue_count = register", generated)
        self.assertIn("queue_slots = register", generated)
        self.assertIn("queue_enqueue", generated)
        self.assertIn("queue_dequeue", generated)
        self.assertIn("rx_transfer", generated)
        self.assertIn("tx_transfer", generated)

    def test_memory_emits_explicit_one_cycle_read_register(self) -> None:
        generated = compile_source(
            (ROOT / "examples/sync_memory.zhl").read_text()
        ).clash

        self.assertEqual(
            generated, (ROOT / "examples/generated/SyncMemory.hs").read_text()
        )
        self.assertIn("table_cells = register", generated)
        self.assertIn("table_read_data = register", generated)
        self.assertIn("cells !!", generated)
        self.assertIn("replace", generated)

    def test_explicit_default_memory_reset_policy_preserves_legacy_clash(self) -> None:
        implicit = """
        module LegacyMemory {
          clock clk reset rst
          in address:u2 in we:bit in data:u8 out q:u8
          memory table:mem<u8,4>{read_latency 1 collision read_first}
          table.read_address=address table.write_enable=we
          table.write_address=address table.write_data=data
          q=table.read_data
        }
        """
        explicit = implicit.replace(
            "collision read_first}",
            "collision read_first reset { contents clear read_data clear }}",
        )

        implicit_clash = compile_source(implicit).clash
        self.assertEqual(compile_source(explicit).clash, implicit_clash)
        self.assertNotIn("Clash.Explicit.Reset", implicit_clash)
        self.assertNotIn("withReset noReset", implicit_clash)

    def test_profiled_memory_emits_independent_reset_and_latency_paths(self) -> None:
        latency_zero = compile_source(
            """
            module AsyncRead {
              clock clk reset rst
              in address:u2 in we:bit in data:u8 out q:u8
              memory table:mem<u8,4>{
                read_latency 0 collision write_first
                reset { contents preserve read_data clear }
              }
              table.read_address=address table.write_enable=we
              table.write_address=address table.write_data=data
              q=table.read_data
            }
            """
        ).clash
        self.assertEqual(
            latency_zero.count("import Clash.Explicit.Reset (noReset)"), 1
        )
        self.assertIn("table_cells = withReset noReset (register", latency_zero)
        self.assertNotIn("table_read_data = register", latency_zero)
        self.assertIn("if resetActive then (0 :: Unsigned 8) else value", latency_zero)
        self.assertIn("table_write_active", latency_zero)

        registered = compile_source(
            """
            module PreservedReadResult {
              clock clk reset rst
              in address:u2 in we:bit in data:u8 out q:u8
              memory table:mem<u8,4>{
                read_latency 1 collision read_first
                reset { contents clear read_data preserve }
              }
              table.read_address=address table.write_enable=we
              table.write_address=address table.write_data=data
              q=table.read_data
            }
            """
        ).clash
        self.assertIn("table_cells = register", registered)
        self.assertIn(
            "table_read_data = withReset noReset (register", registered
        )
        self.assertIn("if resetActive then old else value", registered)

    def test_scheduled_memory_preserve_policy_gates_actions(self) -> None:
        generated = compile_source(
            """
            module ScheduledPreserve {
              clock clk reset rst
              in address:u2 in read_enable:bit in write_enable:bit in data:u8
              out q:u8
              memory table:mem<u8,4>{
                read_latency 1 collision write_first
                reset { contents preserve read_data preserve }
              }
              fetch: when read_enable { table.read(address) }
              store: when write_enable { table.write(address,data) }
              q=table.read_data
            }
            """
        ).clash

        self.assertIn("table_read_active", generated)
        self.assertIn("table_write_active", generated)
        self.assertIn("if resetActive then low else fire", generated)
        self.assertEqual(generated.count("withReset noReset (register"), 2)


if __name__ == "__main__":
    unittest.main()
