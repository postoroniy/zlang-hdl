from pathlib import Path
import unittest

from zlang.compiler import compile_source
from zlang.backend.clash import emit_artifact


ROOT = Path(__file__).resolve().parents[2]


class ClashEmitterTests(unittest.TestCase):
    def test_checked_in_example_matches_current_emitter(self) -> None:
        generated = compile_source((ROOT / "examples/add.zhl").read_text()).clash
        self.assertEqual(generated, (ROOT / "examples/generated/Add.hs").read_text())

    def test_emitter_preserves_addition_carry(self) -> None:
        generated = compile_source((ROOT / "examples/add.zhl").read_text()).clash
        self.assertIn("topEntity :: Unsigned 8 -> Unsigned 8 -> Unsigned 9", generated)
        self.assertIn("resize (a) :: Unsigned 9", generated)
        self.assertIn('t_output = PortName "y"', generated)

    def test_runtime_logical_not_lowers_without_unary_minus(self) -> None:
        generated = compile_source(
            "module Invert { in x:bit out y:bit y = !x }"
        ).clash
        self.assertIn("topEntity x = boolToBit ((x) == (low))", generated)
        self.assertNotIn("unary", generated)

    def test_rtl_keyword_leaf_ports_are_mangled_in_annotation_and_manifest(self) -> None:
        module = compile_source(
            "module ReservedPorts { in input:u8 out output:u8 output=input }",
            include_clash=False,
        ).ir
        artifact = emit_artifact(module)
        self.assertIn('t_inputs = [PortName "zlang_input"]', artifact.text)
        self.assertIn('t_output = PortName "zlang_output"', artifact.text)
        bindings = {
            item.semantic_signal_id: item.rtl_path for item in artifact.bindings
        }
        self.assertEqual(bindings["port:input"], "zlang_input")
        self.assertEqual(bindings["port:output"], "zlang_output")

    def test_systemverilog_packed_keyword_is_mangled_for_clash_rtl(self) -> None:
        module = compile_source(
            "module ReservedPacked { in x:u8 out packed:u8 packed=x }",
            include_clash=False,
        ).ir
        artifact = emit_artifact(module)
        self.assertIn('t_output = PortName "zlang_packed"', artifact.text)
        binding = next(
            item for item in artifact.bindings
            if item.semantic_signal_id == "port:packed"
        )
        self.assertEqual(binding.rtl_path, "zlang_packed")

    def test_hierarchical_retained_calls_emit_transitive_helpers_once(self) -> None:
        source = """
fn identity<type T>(x:T)->T { x }
fn twice<type T>(x:T)->T { identity(identity(x)) }
module Child { in x:u8 out y:u8 y=twice(x) }
module Top { in x:u8 out y:u8 inst child:Child { x } y=child.y }
"""
        module = compile_source(source, top="Top", include_clash=False).ir
        generated = emit_artifact(module).text
        child = module.children[0]
        expected = sorted(
            function.name for function in child.callable_definitions
        )
        signatures = sorted(
            line.split(" ::", 1)[0]
            for line in generated.splitlines()
            if line.startswith("zlang_spec_") and " :: " in line
        )
        self.assertEqual(signatures, expected)
        self.assertEqual(len(signatures), 2)
        by_source = {
            function.metadata.source_name: function.name
            for function in child.callable_definitions
            if function.metadata is not None
        }
        self.assertIn(
            f"{by_source['identity']} (",
            next(
                line for line in generated.splitlines()
                if line.startswith(by_source["twice"] + " ") and " = " in line
            ),
        )
        self.assertIn("child x = zlang_spec_", generated)


if __name__ == "__main__":
    unittest.main()
