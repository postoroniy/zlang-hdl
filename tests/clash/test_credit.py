from pathlib import Path
import unittest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


class ClashCreditTests(unittest.TestCase):
    def test_credit_source_golden_matches_emitter(self) -> None:
        result = compile_source((ROOT / "examples/credit_source.zhl").read_text())
        expected = (ROOT / "examples/generated/CreditSource.hs").read_text()
        self.assertEqual(result.clash, expected)

    def test_sender_counter_gating_and_assertions_are_emitted(self) -> None:
        clash = compile_source(
            (ROOT / "examples/credit_source.zhl").read_text()
        ).clash
        self.assertIn("tx_credits = register (2 :: Unsigned 2)", clash)
        self.assertIn("credits == 0 then low", clash)
        self.assertIn('Verification.checkI "tx_no_underflow"', clash)
        self.assertIn('Verification.checkI "tx_no_overflow"', clash)
        self.assertIn("ZLangCreditForward <$> tx_payload", clash)

    def test_receiver_occupancy_and_assertions_are_emitted(self) -> None:
        clash = compile_source(
            "module CreditSink { clock clk reset rst in accept:bit "
            "in rx:credit<u8,2> out observed:u8 "
            "rx.return=accept observed=rx.payload }"
        ).clash
        self.assertIn("rx_occupancy = register (0 :: Unsigned 2)", clash)
        self.assertIn('Verification.checkI "rx_no_underflow"', clash)
        self.assertIn('Verification.checkI "rx_no_overflow"', clash)
        self.assertIn("ZLangCreditReturn <$> rx_return_checked", clash)


if __name__ == "__main__":
    unittest.main()
