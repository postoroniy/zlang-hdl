"""Structured Clash recursive-formal user-register coverage."""

from pathlib import Path
import shutil
import tempfile
import unittest

from zlang.backend.clash import (
    emit_artifact,
    emit_formal_artifact,
    run_recursive_register_formal,
    validate_register_formal_artifact,
)
from zlang.backend.systemverilog import emit_formal_artifact as emit_sv_formal_artifact
from zlang.backend.manifest import BackendArtifact
from zlang.formal import build_recursive_formal_design
from zlang.ir.formal import FormalStatus
from zlang.parser import parse
from zlang.semantic import analyze
from zlang.toolchain import find_clash_executable, generate_verilog, lint_with_verilator


DEPTH_TWO = (
    "module Grand { clock c reset r in en:bit out y:u8 reg q:u8=0 "
    "q <- mux(en,truncate<8>(q+1),q) y=q } "
    "module Child { clock c reset r in en:bit out y:u8 "
    "inst g:Grand { en=en } y=g.y } "
    "module Top { clock c reset r in en:bit out y:u8 "
    "inst child:Child { en=en } y=child.y }"
)

SIBLINGS = (
    "module Counter { clock c reset r in en:bit out y:u8 reg count:u8=0 "
    "count <- mux(en,truncate<8>(count+1),count) y=count } "
    "module Top { clock c reset r in en:bit out y:u8 "
    "inst child0:Counter { en=en } inst child1:Counter { en=en } y=child0.y }"
)

ASYNC_SIBLINGS = SIBLINGS.replace("reset r", "async reset r @c")

INSTANCE_ARRAY = (
    "module Lane { clock clk reset rst in enable:bit in step:u8 out value:u8 "
    "reg count:u8=0 when enable { count <- truncate<8>(count+step) } value=count } "
    "module Top { clock clk reset rst in enables:vec<2,bit> "
    "out values:vec<2,u8> inst lane[2]:Lane generate(i in 0..2) { "
    "lane[i].enable=enables[i] lane[i].step=1 } "
    "values=generate(i in 0..2) lane[i].value }"
)

TOOLS = bool(find_clash_executable() and shutil.which("verilator"))
FORMAL_TOOLS = bool(TOOLS and all(shutil.which(tool) for tool in ("yosys", "sby", "z3")))


def compile_design(source: str):
    module = analyze(parse(source))
    design = build_recursive_formal_design(module)
    return module, design, emit_formal_artifact(module, design)


class ClashRecursiveRegisterFormalTests(unittest.TestCase):
    def test_structured_current_state_depth_two_and_hash_noninterference(self):
        module, design, formal = compile_design(DEPTH_TWO)
        production_before = emit_artifact(module)
        production_after = emit_artifact(module)
        self.assertEqual(production_before.text, production_after.text)
        self.assertEqual(production_before.artifact_hash, production_after.artifact_hash)
        self.assertNotEqual(production_before.artifact_hash, formal.formal_artifact_hash)
        self.assertIn("data ZFormalOutput", formal.text)
        self.assertIn("NOINLINE zformalInstance", formal.text)
        self.assertIn("<*> (q)", formal.text)
        self.assertNotIn("<*> (q_next)", formal.text)
        register = next(item for item in design.bindings
                        if item.ref.local_semantic_id == "register:q")
        self.assertEqual(register.physical_instance_path, ("Top", "child", "g"))
        draft = next(item for item in formal.formal_observations
                     if item.semantic_binding_id == register.semantic_binding_id)
        self.assertFalse(draft.physical_available)
        self.assertIsNone(draft.observation_token)

    @unittest.skipUnless(TOOLS, "Clash and Verilator are required")
    def test_depth_two_generated_port_and_width_are_validated(self):
        module, design, artifact = compile_design(DEPTH_TWO)
        with tempfile.TemporaryDirectory() as directory:
            files = generate_verilog(
                artifact.text, "Top_formal", Path(directory), find_clash_executable()
            )
            lint_with_verilator(files, "Top_formal")
            validated = validate_register_formal_artifact(artifact, files)
            binding = next(item for item in validated.recursive_bindings
                           if item.local_semantic_id == "register:q")
            self.assertTrue(binding.physical_available)
            self.assertEqual(binding.width, 8)
            self.assertEqual(binding.rtl_module, "Top_formal")
            self.assertIn(binding.formal_observation_token, next(
                path.read_text() for path in files if path.name == "Top_formal.v"
            ))
            restored = BackendArtifact.from_json(validated.to_json())
            restored_binding = next(item for item in restored.recursive_bindings
                                    if item.semantic_binding_id == binding.semantic_binding_id)
            self.assertTrue(restored_binding.physical_available)
            self.assertEqual(restored_binding.formal_observation_token,
                             binding.formal_observation_token)

            top = next(path for path in files if path.name == "Top_formal.v")
            wrong = Path(directory) / "Top_formal_wrong_width.v"
            wrong.write_text(top.read_text().replace(
                f"output wire [7:0] {binding.formal_observation_token}",
                f"output wire [6:0] {binding.formal_observation_token}",
            ))
            altered = tuple(wrong if path == top else path for path in files)
            rejected = validate_register_formal_artifact(artifact, altered)
            rejected_binding = next(item for item in rejected.recursive_bindings
                                    if item.semantic_binding_id == binding.semantic_binding_id)
            self.assertFalse(rejected_binding.physical_available)

    @unittest.skipUnless(TOOLS, "Clash and Verilator are required")
    def test_identical_input_siblings_remain_distinct(self):
        module, design, artifact = compile_design(SIBLINGS)
        with tempfile.TemporaryDirectory() as directory:
            files = generate_verilog(
                artifact.text, "Top_formal", Path(directory), find_clash_executable()
            )
            validated = validate_register_formal_artifact(artifact, files)
            registers = [item for item in validated.recursive_bindings
                         if item.local_semantic_id == "register:count"]
            self.assertEqual(len(registers), 2)
            self.assertEqual(
                {item.physical_instance_path[-1] for item in registers},
                {"child0", "child1"},
            )
            self.assertEqual(len({item.formal_observation_token for item in registers}), 2)
            for item in registers:
                physical_module = f"zformalInstance{item.instance_identity[:12]}"
                self.assertTrue(any(physical_module in path.name for path in files))
            top = next(path.read_text() for path in files if path.name == "Top_formal.v")
            self.assertTrue(all(item.formal_observation_token in top for item in registers))

    @unittest.skipUnless(TOOLS, "Clash and Verilator are required")
    def test_sequential_instance_array_uses_typed_physical_names(self):
        module, design, artifact = compile_design(INSTANCE_ARRAY)
        self.assertNotIn("lane[", artifact.text)
        self.assertRegex(
            artifact.text,
            r"zformalResult_zlang_instance_lane_0_[0-9a-f]{8}",
        )
        self.assertRegex(
            artifact.text,
            r"zformalResult_zlang_instance_lane_1_[0-9a-f]{8}",
        )
        # Constant and indexed bindings remain complete Signal arguments.
        self.assertIn("(pure ((1 :: Unsigned 8)))", artifact.text)
        self.assertIn("(0 :: Index 2)) <$> enables)", artifact.text)

        with tempfile.TemporaryDirectory() as directory:
            files = generate_verilog(
                artifact.text, "Top_formal", Path(directory), find_clash_executable()
            )
            lint_with_verilator(files, "Top_formal")
            validated = validate_register_formal_artifact(artifact, files)

        registers = [
            item for item in validated.recursive_bindings
            if item.local_semantic_id == "register:count"
        ]
        self.assertEqual(
            {item.physical_instance_path for item in registers},
            {("Top", "lane[0]"), ("Top", "lane[1]")},
        )
        self.assertTrue(all(item.physical_available for item in registers))
        self.assertEqual(len({item.instance_identity for item in registers}), 2)
        self.assertEqual(len({item.specialization_identity for item in registers}), 1)

    @unittest.skipUnless(
        FORMAL_TOOLS, "Clash, Verilator, Yosys, SBY, and Z3 are required"
    )
    def test_sequential_instance_array_one_bounded_property_passes(self):
        module, design, artifact = compile_design(INSTANCE_ARRAY)
        selected = next(
            item for item in design.properties
            if item.physical_instance_path == ("Top", "lane[0]")
            and (item.property.generated_from or "").endswith(":reset")
        )
        with tempfile.TemporaryDirectory() as directory:
            files = generate_verilog(
                artifact.text, "Top_formal", Path(directory), find_clash_executable()
            )
            results = run_recursive_register_formal(
                module,
                design,
                artifact,
                files,
                depth=4,
                property_ids=frozenset({selected.concrete_property_id}),
            )
        self.assertEqual(len(results), 1)
        self.assertIs(results[0].status, FormalStatus.BOUNDED_PASS)
        self.assertEqual(results[0].physical_instance_path, ("Top", "lane[0]"))

    @unittest.skipUnless(TOOLS, "Clash and Verilator are required")
    def test_direct_sv_semantic_register_observation_parity(self):
        module = analyze(parse(DEPTH_TWO))
        design = build_recursive_formal_design(module)
        clash = emit_formal_artifact(module, design)
        direct = emit_sv_formal_artifact(module, design)
        with tempfile.TemporaryDirectory() as directory:
            files = generate_verilog(
                clash.text, "Top_formal", Path(directory), find_clash_executable()
            )
            clash = validate_register_formal_artifact(clash, files)
        clash_ids = {
            item.semantic_binding_id for item in clash.formal_observations
            if item.physical_available and ":register:" in item.semantic_binding_id
        }
        direct_ids = {
            item.semantic_binding_id for item in direct.formal_observations
            if item.physical_available and ":register:" in item.semantic_binding_id
        }
        self.assertEqual(clash_ids, direct_ids)

    @unittest.skipUnless(FORMAL_TOOLS, "Clash, Verilator, Yosys, SBY, and Z3 are required")
    def test_real_depth_two_register_properties_pass(self):
        module, design, artifact = compile_design(DEPTH_TWO)
        with tempfile.TemporaryDirectory() as directory:
            files = generate_verilog(
                artifact.text, "Top_formal", Path(directory), find_clash_executable()
            )
            results = run_recursive_register_formal(
                module, design, artifact, files, depth=6
            )
        register_results = [item for item in results
                            if item.physical_instance_path == ("Top", "child", "g")]
        self.assertEqual(len(register_results), 2)
        self.assertTrue(all(item.status is FormalStatus.BOUNDED_PASS
                            for item in register_results))

    @unittest.skipUnless(FORMAL_TOOLS, "Clash, Verilator, Yosys, SBY, and Z3 are required")
    def test_child1_reset_mutation_fails_with_sibling_attribution(self):
        module, design, artifact = compile_design(SIBLINGS)
        with tempfile.TemporaryDirectory() as directory:
            files = generate_verilog(
                artifact.text, "Top_formal", Path(directory), find_clash_executable()
            )
            child1 = next(item for item in design.instances
                          if item.physical_instance_path[-1] == "child1")
            target = next(path for path in files
                          if f"zformalInstance{child1.instance_identity[:12]}" in path.name)
            original = target.read_text()
            mutated = original.replace("count <= 8'd0;", "count <= 8'd1;")
            self.assertNotEqual(original, mutated)
            target.write_text(mutated)
            results = run_recursive_register_formal(
                module, design, artifact, files, depth=6
            )
        reset_ids = {
            item.concrete_property_id for item in design.properties
            if (item.property.generated_from or "").endswith(":reset")
        }
        resets = [item for item in results if item.concrete_property_id in reset_ids]
        child0_result = next(item for item in resets
                             if item.physical_instance_path[-1] == "child0")
        child1_result = next(item for item in resets
                             if item.physical_instance_path[-1] == "child1")
        self.assertIs(child0_result.status, FormalStatus.BOUNDED_PASS)
        self.assertIs(child1_result.status, FormalStatus.FAILED)
        self.assertEqual(child1_result.instance_identity, child1.instance_identity)
        self.assertEqual(child1_result.specialization_identity,
                         child0_result.specialization_identity)
        self.assertTrue(child1_result.object_values)
        self.assertIn("register:count", child1_result.object_values[0][0])
        self.assertIsNotNone(child1_result.source_origin)
        self.assertIsNotNone(child1_result.counterexample)
        self.assertIsNotNone(child1_result.counterexample.cycle)
        self.assertEqual(child1_result.depth, 6)
        self.assertTrue(child1_result.counterexample.raw_trace)

    @unittest.skipUnless(FORMAL_TOOLS, "Clash, Verilator, Yosys, SBY, and Z3 are required")
    def test_async_release_reset_mutation_is_not_vacuously_masked(self):
        module, design, artifact = compile_design(ASYNC_SIBLINGS)
        with tempfile.TemporaryDirectory() as directory:
            files = generate_verilog(
                artifact.text, "Top_formal", Path(directory), find_clash_executable()
            )
            child1 = next(item for item in design.instances
                          if item.physical_instance_path[-1] == "child1")
            target = next(path for path in files
                          if f"zformalInstance{child1.instance_identity[:12]}" in path.name)
            original = target.read_text()
            mutated = original.replace("count <= 8'd0;", "count <= 8'd1;")
            self.assertNotEqual(original, mutated)
            target.write_text(mutated)
            results = run_recursive_register_formal(
                module, design, artifact, files, depth=8
            )
        reset_ids = {
            concrete.concrete_property_id for concrete in design.properties
            if (concrete.property.generated_from or "").endswith(":reset")
        }
        reset_results = [
            item for item in results
            if item.concrete_property_id in reset_ids
        ]
        child1_result = next(
            item for item in reset_results
            if item.physical_instance_path[-1] == "child1"
        )
        self.assertIs(child1_result.status, FormalStatus.FAILED)
        self.assertIsNotNone(child1_result.counterexample)
        self.assertIsNotNone(child1_result.counterexample.cycle)
        self.assertEqual(child1_result.depth, 8)
        self.assertTrue(child1_result.counterexample.raw_trace)


if __name__ == "__main__":
    unittest.main()
