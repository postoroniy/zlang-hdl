from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from zlang.cli import main
from zlang.compiler import compile_source
from zlang.simulate import simulate_csr_cycles


ROOT = Path(__file__).resolve().parents[2]


class CsrIntegrationTests(unittest.TestCase):
    def test_access_policies_and_address_decode(self) -> None:
        module = compile_source((ROOT / "examples/control_csr.zl").read_text()).ir
        results = simulate_csr_cycles(
            module,
            [
                {"addr": 0x40000004, "write": 0, "wdata": 0, "read": 1},
                {"addr": 0x40000000, "write": 1, "wdata": 0xBB, "read": 0},
                {"addr": 0x40000000, "write": 0, "wdata": 0, "read": 1},
                {"addr": 0x40000004, "write": 1, "wdata": 2, "read": 0},
                {"addr": 0x40000004, "write": 0, "wdata": 0, "read": 1},
                {"addr": 0x40000008, "write": 0, "wdata": 0, "read": 1},
            ],
        )
        self.assertEqual(results[0]["rdata"], 3)
        self.assertEqual(results[2]["rdata"], 0xB)
        self.assertEqual(results[2]["state"]["control.CONTROL.start"], 1)
        self.assertEqual(results[2]["state"]["control.CONTROL.command"], 5)
        self.assertEqual(results[4]["rdata"], 1)
        self.assertEqual((results[5]["ready"], results[5]["rdata"]), (0, 0))

    def test_reset_and_pulse_duration_are_cycle_accurate(self) -> None:
        module = compile_source((ROOT / "examples/control_csr.zl").read_text()).ir
        results = simulate_csr_cycles(
            module,
            [
                {"addr": 0x40000000, "write": 1, "wdata": 0x10, "read": 0},
                {"addr": 0x40000000, "write": 0, "wdata": 0, "read": 0},
                {"addr": 0x40000000, "write": 0, "wdata": 0, "read": 0},
                {"addr": 0x40000004, "write": 0, "wdata": 0, "read": 1},
            ],
            reset=[False, False, False, True],
        )
        self.assertEqual(results[1]["state"]["control.CONTROL.start"], 1)
        self.assertEqual(results[2]["state"]["control.CONTROL.start"], 0)
        self.assertEqual(results[3]["rdata"], 3)

    def test_cli_json_and_markdown_agree_with_typed_map(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            json_path = root / "control.json"
            markdown_path = root / "control.md"
            with redirect_stdout(io.StringIO()):
                status = main(
                    [
                        str(ROOT / "examples/control_csr.zl"),
                        "--csr-json",
                        str(json_path),
                        "--csr-markdown",
                        str(markdown_path),
                    ]
                )
            self.assertEqual(status, 0)
            document = json.loads(json_path.read_text())
            status_register = document["blocks"][0]["registers"][1]
            self.assertEqual(status_register["address"], 0x40000004)
            self.assertEqual(status_register["fields"][1]["access"], "w1c")
            markdown = markdown_path.read_text()
            self.assertIn("`0x40000004`", markdown)
            self.assertIn("| error | 1 | `bit` | `w1c` | `0x1` |", markdown)


if __name__ == "__main__":
    unittest.main()
