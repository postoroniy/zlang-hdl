from pathlib import Path
import unittest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


class VerificationArtifactTests(unittest.TestCase):
    def test_clash_and_sva_goldens_match(self) -> None:
        result = compile_source((ROOT / "examples/contracted_add.zl").read_text())

        self.assertEqual(
            result.clash,
            (ROOT / "examples/generated/ContractedAdd.hs").read_text(),
        )
        self.assertEqual(
            result.contracts_sva,
            (ROOT / "examples/generated/ContractedAdd.contracts.sv").read_text(),
        )
        self.assertIn("operands_bounded: assume property", result.contracts_sva)
        self.assertIn("sum_matches: assert property", result.contracts_sva)
        self.assertIn("disable iff (rst == 1'b1)", result.contracts_sva)
        self.assertIn("bind ContractedAdd", result.contracts_sva)

    def test_protocol_transfer_is_lowered_to_the_documented_event(self) -> None:
        result = compile_source(
            "module ContractedBuffer { clock clk reset rst "
            "in rx:rv<u8> out tx:rv<u8> connect rx -> tx { buffer 1 } "
            "guarantee transfer_event @ clk disable iff rst "
            "{ rx.transfer == (rx.valid & rx.ready) } }"
        )

        self.assertIn("(rx_valid) && (rx_ready)", result.contracts_sva)
        self.assertIn(".rx_valid(rx_valid)", result.contracts_sva)
        self.assertIn(".rx_ready(rx_ready)", result.contracts_sva)

    def test_module_without_contracts_has_no_sva_artifact(self) -> None:
        result = compile_source((ROOT / "examples/add.zl").read_text())
        self.assertEqual(result.contracts_sva, "")


if __name__ == "__main__":
    unittest.main()
