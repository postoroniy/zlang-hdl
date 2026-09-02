from pathlib import Path
import unittest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


class ClashConnectionTests(unittest.TestCase):
    def test_buffered_ready_valid_golden_matches_emitter(self) -> None:
        result = compile_source((ROOT / "examples/rv_buffer.zhl").read_text())
        expected = (ROOT / "examples/generated/RvBuffer.hs").read_text()
        self.assertEqual(result.clash, expected)
        self.assertIn("Vec 2 (Unsigned 8)", result.clash)
        self.assertIn("rx_tx_buffer_count", result.clash)

    def test_explicit_adapters_emit_credit_and_queue_state(self) -> None:
        rv_to_credit = compile_source(
            (ROOT / "examples/rv_to_credit.zhl").read_text()
        ).clash
        self.assertIn("tx_credits = register (2 :: Unsigned 2)", rv_to_credit)
        self.assertIn("rx_ready", rv_to_credit)

        credit_to_rv = compile_source(
            (ROOT / "examples/credit_to_rv.zhl").read_text()
        ).clash
        self.assertIn("rx_tx_buffer_slots", credit_to_rv)
        self.assertIn("rx_return = rx_tx_buffer_dequeue", credit_to_rv)


if __name__ == "__main__":
    unittest.main()
