from dataclasses import replace
from pathlib import Path
import unittest

from zlang.compiler import compile_source
from zlang.ir.expressions import ImplementationKind
from zlang.opt import (
    EffectKind,
    NodeCategory,
    OptimizationStage,
    Purity,
    Signedness,
    lower,
    restore,
)
from zlang.opt.ir import ExpressionOp
from zlang.parser import parse
from zlang.semantic import analyze


ROOT = Path(__file__).resolve().parents[2]


class CanonicalMetadataSemanticTests(unittest.TestCase):
    def test_type_timing_domain_effect_and_origin_metadata_are_derived(self) -> None:
        compilation = compile_source(
            (ROOT / "examples/metadata_datapath.zl").read_text()
        )
        canonical = compilation.optimization_ir
        dot = next(node for node in canonical.expressions if node.op is ExpressionOp.DOT)
        delay = next(
            node for node in canonical.expressions if node.op is ExpressionOp.DELAY
        )

        self.assertEqual(dot.metadata.width, 64)
        self.assertEqual(dot.metadata.signedness, Signedness.AGGREGATE)
        self.assertEqual(dot.metadata.latency, 0)
        self.assertEqual(dot.metadata.initiation_interval, 1)
        self.assertEqual(dot.metadata.domains, ("clk",))
        self.assertEqual(dot.metadata.purity, Purity.PURE)
        self.assertEqual(dot.metadata.effects, ())
        self.assertEqual(dot.origins[0].construct, "dot")

        self.assertEqual(delay.category, NodeCategory.STATE)
        self.assertEqual(delay.metadata.width, 18)
        self.assertEqual(delay.metadata.signedness, Signedness.UNSIGNED)
        self.assertEqual(delay.metadata.latency, 1)
        self.assertEqual(delay.metadata.domains, ("clk",))
        self.assertEqual(delay.metadata.purity, Purity.OBSERVATIONAL)
        self.assertEqual(delay.metadata.effects, (EffectKind.TIME_SHIFT,))
        self.assertEqual(delay.origins[0].construct, "delay<1>")

    def test_five_categories_cover_state_protocol_transaction_and_architecture(self) -> None:
        examples = (
            "add.zl",
            "counter.zl",
            "rv_passthrough.zl",
            "request_client.zl",
            "mac_choice.zl",
        )
        categories = {
            item.category
            for filename in examples
            for item in (
                *lower(
                    analyze(parse((ROOT / "examples" / filename).read_text()))
                ).expressions,
                *lower(
                    analyze(parse((ROOT / "examples" / filename).read_text()))
                ).entities,
            )
        }
        self.assertEqual(categories, set(NodeCategory))

    def test_high_level_and_selected_architecture_stages_are_distinct(self) -> None:
        source = (ROOT / "examples/cost_mac.zl").read_text()
        semantic = analyze(parse(source))
        compilation = compile_source(source)
        high = compilation.high_level_ir
        selected = compilation.optimization_ir
        high_choice = next(
            node for node in high.expressions
            if node.op is ExpressionOp.IMPLEMENTATION_CHOICE
        )
        selected_choice = next(
            node for node in selected.expressions
            if node.op is ExpressionOp.IMPLEMENTATION_CHOICE
        )

        self.assertEqual(high.stage, OptimizationStage.HIGH_LEVEL)
        self.assertEqual(selected.stage, OptimizationStage.SELECTED_ARCHITECTURE)
        self.assertIsNone(high_choice.attribute("selected"))
        self.assertEqual(
            selected_choice.attribute("selected"), ImplementationKind.DSP_MAC
        )
        self.assertEqual(restore(high), semantic)

        high_evidence = high_choice.attribute("evidence")
        selected_evidence = selected_choice.attribute("evidence")
        self.assertTrue(all(item.estimate is None for item in high_evidence))
        self.assertTrue(all(item.measurement is None for item in high_evidence))
        self.assertTrue(all(item.estimate is not None for item in selected_evidence))
        self.assertTrue(all(item.measurement is None for item in selected_evidence))

    def test_legacy_explorers_retain_the_high_level_source_root(self) -> None:
        for filename, construct, collection_name in (
            ("auto_pipeline_products.zl", "pipeline(auto)", "pipeline_explorations"),
            ("fir_architecture.zl", "architecture(auto)", "architecture_explorations"),
        ):
            with self.subTest(filename=filename):
                canonical = lower(
                    analyze(parse((ROOT / "examples" / filename).read_text()))
                )
                exploration = getattr(canonical, collection_name)[0]
                source = canonical.expressions[exploration.source_expression]
                self.assertIn(construct, {origin.construct for origin in source.origins})
                self.assertNotEqual(
                    exploration.source_expression,
                    next(
                        candidate.expression
                        for candidate in exploration.candidates
                        if candidate.name == exploration.selected
                    ),
                )

    def test_interning_retains_every_source_origin(self) -> None:
        canonical = lower(
            analyze(
                parse(
                    "module Shared {\n"
                    "  in a:u8\n"
                    "  out y:u9\n"
                    "  out z:u9\n"
                    "  y = a + a\n"
                    "  z = a + a\n"
                    "}\n"
                )
            )
        )
        add = next(node for node in canonical.expressions if node.op is ExpressionOp.ADD)
        self.assertEqual(len(add.origins), 2)
        self.assertEqual(
            {origin.span.start_line for origin in add.origins},
            {5, 6},
        )

    def test_inconsistent_derived_metadata_is_rejected(self) -> None:
        canonical = lower(analyze(parse("module A { in x:u8 out y:u8 y=x }")))
        node = canonical.expressions[0]
        with self.assertRaisesRegex(ValueError, "does not match type width"):
            replace(node, metadata=replace(node.metadata, width=7))


if __name__ == "__main__":
    unittest.main()
