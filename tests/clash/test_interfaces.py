from pathlib import Path
import unittest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


class ClashInterfaceTests(unittest.TestCase):
    def test_ready_valid_golden_matches_emitter(self) -> None:
        result = compile_source((ROOT / "examples/rv_passthrough.zl").read_text())
        expected = (ROOT / "examples/generated/RvPassthrough.hs").read_text()
        self.assertEqual(result.clash, expected)

    def test_protocol_records_and_transfer_expression_are_emitted(self) -> None:
        clash = compile_source(
            "module Transfer { in rx:rv<u8> in enable:bit out fired:bit "
            "rx.ready=enable fired=rx.transfer }"
        ).clash
        self.assertIn("data ZLangReadyValidForward a", clash)
        self.assertIn("data ZLangReadyValidBackward", clash)
        self.assertIn("fired = ((rx_valid) .&. (rx_ready))", clash)
        self.assertIn('PortProduct "rx"', clash)


if __name__ == "__main__":
    unittest.main()
