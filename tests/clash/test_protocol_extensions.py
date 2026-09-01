from pathlib import Path
import unittest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


class ClashProtocolExtensionTests(unittest.TestCase):
    def generated(self, example: str) -> str:
        result = compile_source((ROOT / "examples" / example).read_text())
        self.assertEqual(
            result.clash,
            (ROOT / "examples" / "generated" / f"{result.ir.name}.hs").read_text(),
        )
        return result.clash

    def test_fixed_priority_emits_first_valid_selection_and_packet_lock(self) -> None:
        generated = self.generated("packet_fixed_arbiter.zl")
        self.assertIn("if valid_0 == high then 0", generated)
        self.assertIn("grant_active = register", generated)
        self.assertIn("grant_owner = register", generated)
        self.assertIn("transferred .&. lastBeat", generated)
        self.assertNotIn("next_priority = register", generated)

    def test_round_robin_emits_rotating_packet_boundary_priority(self) -> None:
        generated = self.generated("packet_round_robin.zl")
        self.assertIn("next_priority = register", generated)
        self.assertIn("if selected == 1 then 0 else selected + 1", generated)
        self.assertIn("grant_complete", generated)
        self.assertIn("zlangPacketLast", generated)

    def test_virtual_channel_credit_emits_one_counter_per_vc(self) -> None:
        generated = self.generated("vc_credit_source.zl")
        self.assertIn("tx_credits_0 = register", generated)
        self.assertIn("tx_credits_1 = register", generated)
        self.assertIn("case vc of { 0 -> credits_0 > 0", generated)
        self.assertIn("tx_sent_0", generated)
        self.assertIn("tx_returned_1", generated)


if __name__ == "__main__":
    unittest.main()
