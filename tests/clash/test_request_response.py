from pathlib import Path
import unittest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


class ClashRequestResponseTests(unittest.TestCase):
    def test_request_client_golden_matches_emitter(self) -> None:
        result = compile_source((ROOT / "examples/request_client.zl").read_text())
        expected = (ROOT / "examples/generated/RequestClient.hs").read_text()
        self.assertEqual(result.clash, expected)

    def test_in_order_outstanding_counter_and_assertions_are_emitted(self) -> None:
        clash = compile_source(
            "module Client { clock clk reset rst interface mem:"
            "request_response<u8,u16>{max_outstanding 2 ordering in_order} "
            "mem.request.payload=0 mem.request.valid=0 mem.response.ready=0 }"
        ).clash
        self.assertIn("mem_outstanding = register (0 :: Unsigned 2)", clash)
        self.assertIn('Verification.checkI "mem_within_limit"', clash)
        self.assertIn('Verification.checkI "mem_has_request"', clash)

    def test_out_of_order_id_table_and_assertion_are_emitted(self) -> None:
        clash = compile_source(
            (ROOT / "examples/request_client.zl").read_text()
        ).clash
        self.assertIn("Vec 2 Bit", clash)
        self.assertIn("Vec 2 (Unsigned 2)", clash)
        self.assertNotIn("Maybe (Unsigned 2)", clash)
        self.assertIn("zlangInsertId", clash)
        self.assertIn("zlangRemoveId", clash)
        self.assertIn('Verification.checkI "mem_ids_valid"', clash)


if __name__ == "__main__":
    unittest.main()
