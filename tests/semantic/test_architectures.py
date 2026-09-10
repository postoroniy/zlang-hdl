from pathlib import Path
import unittest

from dataclasses import replace

from zlang.architecture import ArchitectureCandidate
from zlang.compiler import compile_source
from zlang.parser import ParseError, parse
from zlang.simulate import simulate


ROOT = Path(__file__).resolve().parents[2]


class ArchitectureSemanticTests(unittest.TestCase):
    def _module_and_results(self):
        result = compile_source(
            (ROOT / "examples/implementation_intent.zhl").read_text(),
            top="FirArchitecture",
            include_clash=False,
        )
        return result.ir, list(result.exploration_results)

    def test_implement_exposes_bounded_architecture_candidates(self) -> None:
        module, results = self._module_and_results()
        self.assertEqual(len(results), 1)
        candidates = tuple(
            item.architecture
            for item in results[0].candidates
            if isinstance(item.architecture, ArchitectureCandidate)
        )
        self.assertTrue(candidates)
        self.assertTrue(
            all(item.value_root.type == module.outputs[0].type for item in candidates)
        )

    def test_exact_architecture_candidates_preserve_behavior(self) -> None:
        module, results = self._module_and_results()
        candidates = tuple(
            item.architecture
            for item in results[0].candidates
            if isinstance(item.architecture, ArchitectureCandidate)
        )
        vectors = (
            ([0, 0, 0, 0], [255, 3, 9, 1]),
            ([1, 2, 3, 4], [5, 6, 7, 8]),
            ([255, 255, 255, 255], [255, 255, 255, 255]),
        )
        for candidate in candidates:
            candidate_module = replace(
                module,
                assignments=(replace(module.assignments[0], expression=candidate.value_root),),
            )
            for samples, coefficients in vectors:
                expected = sum(
                    sample * coefficient
                    for sample, coefficient in zip(samples, coefficients, strict=True)
                )
                self.assertEqual(
                    simulate(
                        candidate_module,
                        samples=samples,
                        coefficients=coefficients,
                    )["y"],
                    expected,
                    candidate.name,
                )

    def test_scalar_architecture_spelling_has_migration_diagnostic(self) -> None:
        with self.assertRaisesRegex(
            ParseError, "scalar architecture\\(auto\\) was removed"
        ):
            parse("module Legacy { in a:u8 out y:u8 y=architecture(auto){a} }")

    def test_scalar_pipeline_and_explore_are_not_architecture_sources(self) -> None:
        for spelling in ("pipeline(auto){a}", "explore { a }"):
            with self.subTest(spelling=spelling):
                with self.assertRaises(ParseError):
                    parse(f"module Legacy {{ in a:u8 out y:u8 y={spelling} }}")


if __name__ == "__main__":
    unittest.main()
