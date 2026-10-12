from pathlib import Path
import unittest

from zlang.ast.nodes import BinaryExpr, ContractKind
from zlang.parser import parse


ROOT = Path(__file__).resolve().parents[2]


class VerificationParserTests(unittest.TestCase):
    def test_assume_and_guarantee_preserve_clock_reset_and_expression(self) -> None:
        module = parse((ROOT / "examples/contracted_add.zhl").read_text())

        self.assertEqual([item.kind for item in module.contracts], [
            ContractKind.ASSUME,
            ContractKind.GUARANTEE,
        ])
        self.assertEqual(
            [(item.name, item.clock, item.reset) for item in module.contracts],
            [
                ("operands_bounded", "clk", "rst"),
                ("sum_matches", "clk", "rst"),
            ],
        )
        self.assertTrue(
            all(isinstance(item.expression, BinaryExpr) for item in module.contracts)
        )

    def test_contract_body_uses_normal_expression_grammar(self) -> None:
        module = parse(
            "module C { clock c reset r in valid:bit out ready:bit ready=valid "
            "guarantee held @ c disable iff r { mux(valid, ready, 1) } }"
        )
        self.assertEqual(module.contracts[0].name, "held")


if __name__ == "__main__":
    unittest.main()
