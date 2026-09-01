import unittest

from zlang.architecture import (
    ArchitectureImplementation,
    ResourceIntent,
    expand_architectures,
    extract_best_architecture,
)
from zlang.compiler import compile_source
from zlang.costs import UnifiedConstraint
from zlang.ir.expressions import CostMetric
from zlang.ir.types import SIntType


class ArchitectureAlternativesM29Tests(unittest.TestCase):
    def _root(self, type_name="u4"):
        ctype = "u8" if type_name.startswith("u") else "s8"
        out = "u9" if type_name.startswith("u") else "s9"
        compilation = compile_source(
            f"module M {{ in a:{type_name} in b:{type_name} in c:{ctype} "
            f"out y:{out} y=a*b+c }}"
        )
        return compilation.ir.assignments[0].expression

    def test_multiply_add_expands_to_three_same_timing_candidates(self):
        candidates = expand_architectures(self._root())
        self.assertEqual(
            [item.implementation for item in candidates],
            [ArchitectureImplementation.GENERIC_MUL_ADD,
             ArchitectureImplementation.MAC,
             ArchitectureImplementation.DSP_MAC],
        )
        self.assertEqual({(item.timing_contract.latency, item.timing_contract.initiation_interval) for item in candidates}, {(0, 1)})
        self.assertEqual(candidates[2].resource_intent, ResourceIntent.DEDICATED_DSP)
        self.assertTrue(all(item.value_root.type == candidates[0].value_root.type for item in candidates))

    def test_unsupported_shape_gets_only_generic(self):
        compilation = compile_source("module M { in a:u4 in b:u4 out y:u5 y=a+b }")
        candidates = expand_architectures(compilation.ir.assignments[0].expression)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].implementation, ArchitectureImplementation.GENERIC_MUL_ADD)

    def test_signed_is_supported_and_mixed_is_not(self):
        signed = expand_architectures(self._root("s4"))
        self.assertEqual(len(signed), 3)
        with self.assertRaises(Exception):
            compile_source("module M { in a:u4 in b:s4 in c:u8 out y:u9 y=a*b+c }")

    def test_m28_selection_and_dsp_constraint(self):
        candidates = expand_architectures(self._root())
        self.assertEqual(extract_best_architecture(candidates, CostMetric.LUT).selected.implementation,
                         ArchitectureImplementation.DSP_MAC)
        result = extract_best_architecture(
            candidates,
            CostMetric.LUT,
            [UnifiedConstraint(CostMetric.DSP, maximum=0)],
        )
        self.assertNotEqual(result.selected.implementation, ArchitectureImplementation.DSP_MAC)

    def test_exhaustive_small_unsigned_values_preserve_value(self):
        for a in range(16):
            for b in range(16):
                for c in range(16):
                    expected = (a * b + c) & 0xFF
                    self.assertEqual((a * b + c) & 0xFF, expected)


if __name__ == "__main__":
    unittest.main()
