from __future__ import annotations

import unittest

from zlang.ast.nodes import (
    BinaryExpr,
    VerificationGoalKind,
)
from zlang.parser import ParseError, parse


class VerificationUxParserTests(unittest.TestCase):
    def test_standalone_assert_and_cover_preserve_optional_clock_and_origins(self) -> None:
        module = parse(
            """module V {
                clock clk
                reset rst
                in enable : bit
                reg count : u8 = 0
                assert count_within @ clk { count <= 8 }
                cover reaches_full { count == 8 }
            }"""
        )

        self.assertEqual(
            [goal.kind for goal in module.verification_goals],
            [VerificationGoalKind.ASSERT, VerificationGoalKind.COVER],
        )
        self.assertEqual(
            [(goal.name, goal.clock) for goal in module.verification_goals],
            [("count_within", "clk"), ("reaches_full", None)],
        )
        self.assertTrue(
            all(isinstance(goal.expression, BinaryExpr) for goal in module.verification_goals)
        )
        self.assertTrue(all(goal.origin is not None for goal in module.verification_goals))
        self.assertTrue(
            all(goal.expression.origin is not None for goal in module.verification_goals)
        )

    def test_contract_scope_preserves_requirements_goals_clock_and_order(self) -> None:
        module = parse(
            """module V {
                clock clk
                reset rst
                in legal, ready : bit
                out valid : bit
                valid = legal
                contract transfer @ clk {
                    require legal_input { legal }
                    require sink_ready { ready }
                    assert invariant { valid == legal }
                    ensure output_shape { !valid | legal }
                    cover accepted { valid & ready }
                }
            }"""
        )

        self.assertEqual(len(module.verification_scopes), 1)
        scope = module.verification_scopes[0]
        self.assertEqual((scope.name, scope.clock), ("transfer", "clk"))
        self.assertEqual(
            [requirement.name for requirement in scope.requirements],
            ["legal_input", "sink_ready"],
        )
        self.assertEqual(
            [(goal.kind, goal.name) for goal in scope.goals],
            [
                (VerificationGoalKind.ASSERT, "invariant"),
                (VerificationGoalKind.ENSURE, "output_shape"),
                (VerificationGoalKind.COVER, "accepted"),
            ],
        )
        self.assertTrue(scope.origin is not None)
        self.assertTrue(all(item.origin is not None for item in scope.requirements))
        self.assertTrue(all(item.origin is not None for item in scope.goals))

    def test_verification_words_remain_ordinary_identifiers(self) -> None:
        module = parse(
            """module Contextual {
                in assert, cover, contract, require, ensure : bit
                out y : bit
                combined = assert & cover
                contract = require
                ensure = contract
                y = combined | ensure
            }"""
        )

        self.assertEqual(
            module.ports[0].names,
            ("assert", "cover", "contract", "require", "ensure"),
        )
        self.assertEqual(module.verification_goals, ())
        self.assertEqual(module.verification_scopes, ())
        self.assertEqual(
            [assignment.target for assignment in module.assignments],
            ["combined", "contract", "ensure", "y"],
        )

    def test_names_are_required_and_ensure_is_scope_only(self) -> None:
        invalid_sources = (
            "module V { clock clk assert @ clk { 1 } }",
            "module V { clock clk cover @ clk { 1 } }",
            "module V { clock clk contract @ clk { assert ok { 1 } } }",
            "module V { clock clk ensure ok @ clk { 1 } }",
        )
        for source in invalid_sources:
            with self.subTest(source=source), self.assertRaises(ParseError):
                parse(source)


if __name__ == "__main__":
    unittest.main()
