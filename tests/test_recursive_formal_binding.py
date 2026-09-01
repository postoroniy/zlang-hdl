import json
import shutil
import tempfile
import unittest
from pathlib import Path

from zlang.backend.clash import emit_formal_artifact
from zlang.backend.manifest import BackendArtifact, RECURSIVE_MANIFEST_VERSION
from zlang.toolchain import find_clash_executable, generate_verilog, lint_with_verilator
from zlang.formal import build_recursive_formal_design, emit_recursive_harness, run_recursive_formal
from zlang.ir.formal import FormalStatus
from zlang.parser import parse
from zlang.semantic import analyze


ROOT = Path(__file__).resolve().parents[1]


class RecursiveFormalBindingTests(unittest.TestCase):
    def test_depth_two_instance_tree_and_state_properties(self):
        source = (
            "module Grand { clock c reset r out y:u8 reg q:u8=0 "
            "rule step when 1 { q <- q } y=q } "
            "module Child { clock c reset r out y:u8 inst g:Grand y=g.y } "
            "module Top { clock c reset r out y:u8 inst child:Child y=child.y }"
        )
        module = analyze(parse(source))
        design = build_recursive_formal_design(module, selected_ir_identity="top-selected")
        self.assertEqual(len(design.instances), 3)
        self.assertGreaterEqual(len(design.properties), 2)
        paths = {item.physical_instance_path for item in design.instances}
        self.assertIn(("Top", "child", "g"), paths)
        grand = next(item for item in design.instances if item.module_name == "Grand")
        self.assertTrue(any(item.ref.instance_identity == grand.instance_identity
                            and "register:q" in item.ref.local_semantic_id
                            for item in design.bindings))

    def test_same_specialization_instances_are_distinct(self):
        source = (
            "module Child { clock c reset r out y:u8 reg q:u8=0 "
            "rule step when 1 { q <- q } y=q } "
            "module Top { clock c reset r out y:u8 "
            "inst fifo0:Child y=fifo0.y inst fifo1:Child }"
        )
        module = analyze(parse(source))
        design = build_recursive_formal_design(module)
        children = [item for item in design.instances if item.module_name == "Child"]
        self.assertEqual(len(children), 2)
        self.assertNotEqual(children[0].instance_identity, children[1].instance_identity)
        properties = [item for item in design.properties if item.defining_module == "Child"]
        self.assertEqual(len({item.concrete_property_id for item in properties}), len(properties))
        self.assertEqual({item.physical_instance_path[-1] for item in children}, {"fifo0", "fifo1"})

    def test_manifest_v4_round_trip_and_observation_harness(self):
        source = (ROOT / "examples/axi_csr_top.zl").read_text()
        module = analyze(parse(source))
        design = build_recursive_formal_design(module)
        artifact = emit_formal_artifact(module, design)
        self.assertEqual(artifact.manifest_version, RECURSIVE_MANIFEST_VERSION)
        self.assertTrue(artifact.instances)
        self.assertTrue(artifact.recursive_bindings)
        self.assertTrue(artifact.formal_observations)
        self.assertTrue(artifact.formal_artifact_hash)
        restored = BackendArtifact.from_json(artifact.to_json())
        self.assertEqual(restored.instances, artifact.instances)
        self.assertEqual(restored.recursive_bindings, artifact.recursive_bindings)
        harness = emit_recursive_harness(design)
        self.assertIn("recursive_m35_formal", harness)
        self.assertIn("observation", harness)

    def test_deterministic_serialization_and_cache_identity(self):
        source = (ROOT / "examples/hierarchical_request_response_m40.zl").read_text()
        module = analyze(parse(source))
        first = build_recursive_formal_design(module)
        second = build_recursive_formal_design(module)
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.required_observations, tuple(sorted(first.required_observations)))

    def test_unconnected_nested_observations_are_explicit_skips(self):
        module = analyze(parse((ROOT / "examples/axi_csr_top.zl").read_text()))
        design = build_recursive_formal_design(module)
        results = run_recursive_formal(design)
        self.assertTrue(results)
        self.assertTrue(all(item.status is FormalStatus.SKIPPED for item in results))
        self.assertTrue(all(item.physical_instance_path for item in results))
        self.assertIn("not connected", results[0].reason)

    def test_clash_formal_artifact_materializes_typed_rr_observations(self):
        source = (ROOT / "examples/simple_dma_m40.zl").read_text()
        module = analyze(parse(source))
        design = build_recursive_formal_design(module)
        artifact = emit_formal_artifact(module, design)
        self.assertIn("NOINLINE zformalInstance", artifact.text)
        self.assertIn("topEntity", artifact.text)
        self.assertIn('t_name = "SimpleDMA_formal"', artifact.text)
        # Structured artifacts remain unavailable until generated RTL ports
        # and widths have been validated.
        rr_observations = [item for item in artifact.formal_observations
                           if ":rr:" in item.semantic_binding_id]
        self.assertEqual(len(rr_observations), 5)
        self.assertTrue(all(item.observation_token is None
                            for item in rr_observations))
        # The child register is not yet exported by the closed Clash ABI and
        # must remain an explicit unavailable observation.
        nested_registers = [item for item in artifact.formal_observations
                            if "register:count" in item.semantic_binding_id]
        self.assertTrue(nested_registers)
        self.assertTrue(all(item.observation_token is None for item in nested_registers))
        # Request/response formal components publish the existing typed ledger,
        # not requester/responder child schedules.  The rule binding remains in
        # the recursive semantic manifest so its unsupported status is visible,
        # but it must not force an unbound observation into this closed ABI.
        nested_rule_fires = [
            item for item in artifact.formal_observations
            if ":rule:" in item.semantic_binding_id
        ]
        self.assertTrue(nested_rule_fires)
        self.assertTrue(all(item.observation_token is None
                            for item in nested_rule_fires))

    @unittest.skipUnless(find_clash_executable() and shutil.which("verilator"),
                         "Clash and Verilator are unavailable")
    def test_clash_formal_artifact_generates_and_lints(self):
        source = (ROOT / "examples/simple_dma_m40.zl").read_text()
        module = analyze(parse(source))
        artifact = emit_formal_artifact(module, build_recursive_formal_design(module))
        with tempfile.TemporaryDirectory() as directory:
            files = generate_verilog(artifact.text, "SimpleDMA_formal",
                                     Path(directory), find_clash_executable())
            lint_with_verilator(files, "SimpleDMA_formal")


if __name__ == "__main__":
    unittest.main()
