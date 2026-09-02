from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from zlang.cli import main


ROOT = Path(__file__).resolve().parents[2]


class VerificationIntegrationTests(unittest.TestCase):
    def test_cli_writes_bindable_contract_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clash_path = root / "ContractedAdd.hs"
            sva_path = root / "ContractedAdd.contracts.sv"
            with redirect_stdout(io.StringIO()):
                status = main(
                    [
                        str(ROOT / "examples/contracted_add.zhl"),
                        "-o",
                        str(clash_path),
                        "--contracts-sva",
                        str(sva_path),
                    ]
                )

            self.assertEqual(status, 0)
            self.assertEqual(
                clash_path.read_text(),
                (ROOT / "examples/generated/ContractedAdd.hs").read_text(),
            )
            self.assertEqual(
                sva_path.read_text(),
                (ROOT / "examples/generated/ContractedAdd.contracts.sv").read_text(),
            )


if __name__ == "__main__":
    unittest.main()
