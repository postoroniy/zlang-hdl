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


if __name__ == "__main__":
    unittest.main()
