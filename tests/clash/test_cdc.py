from pathlib import Path
import unittest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


class ClashCdcTests(unittest.TestCase):
    def generated(self, example: str) -> str:
        return compile_source((ROOT / "examples" / example).read_text()).clash

    def assert_golden(self, example: str, generated: str) -> None:
        module_name = compile_source(
            (ROOT / "examples" / example).read_text()
        ).ir.name
        self.assertEqual(
            generated,
            (ROOT / "examples" / "generated" / f"{module_name}.hs").read_text(),
        )

    def test_level_uses_a_two_flip_flop_synchronizer(self) -> None:
        generated = self.generated("cdc_level.zl")
        self.assert_golden("cdc_level.zl", generated)
        self.assertIn("createDomain vSystem{vName=\"SourceClockDomain\"", generated)
        self.assertIn("createDomain vSystem{vName=\"DestinationClockDomain\"", generated)
        self.assertIn("Synchronizer.dualFlipFlopSynchronizer", generated)
        self.assertNotIn("unsafeSynchronizer", generated)

    def test_pulse_uses_source_toggle_and_destination_edge_detection(self) -> None:
        generated = self.generated("cdc_pulse.zl")
        self.assert_golden("cdc_pulse.zl", generated)
        self.assertIn("source_toggle = register", generated)
        self.assertIn("synchronized_toggle", generated)
        self.assertIn("previous_toggle = register", generated)
        self.assertIn("crossed_pulse = xor", generated)

    def test_handshake_holds_data_and_synchronizes_request_and_acknowledge(self) -> None:
        generated = self.generated("cdc_handshake.zl")
        self.assert_golden("cdc_handshake.zl", generated)
        self.assertIn("source_data = register", generated)
        self.assertIn("source_request = register", generated)
        self.assertIn("destination_acknowledge = register", generated)
        self.assertEqual(
            generated.count("Synchronizer.dualFlipFlopSynchronizer"), 2
        )
        self.assertIn("Explicit.unsafeSynchronizer", generated)

    def test_async_fifo_uses_clash_gray_pointer_primitive(self) -> None:
        generated = self.generated("cdc_async_fifo.zl")
        self.assert_golden("cdc_async_fifo.zl", generated)
        self.assertIn("Synchronizer.asyncFIFOSynchronizer (SNat @2)", generated)
        self.assertIn("source_full", generated)
        self.assertIn("destination_empty", generated)


if __name__ == "__main__":
    unittest.main()
