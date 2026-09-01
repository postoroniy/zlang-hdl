"""Structured Clash recursive-formal FIFO-state coverage."""

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

from zlang.backend.clash import (
    emit_artifact,
    emit_formal_artifact,
    run_recursive_register_formal,
    validate_register_formal_artifact,
)
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_formal_artifact as emit_sv_formal_artifact
from zlang.formal import build_recursive_formal_design
from zlang.ir.formal import FormalStatus, TemporalForm
from zlang.parser import parse
from zlang.semantic import analyze
from zlang.toolchain import find_clash_executable, generate_verilog, lint_with_verilator


FIFO_CHILD = (
    "module FifoChild { clock c reset r in data:u8 in push:bit in pop:bit "
    "out count:u3 fifo q:fifo<u8,4> q.data=data q.push=push q.pop=pop "
    "count=q.count } "
)

DEPTH_TWO = FIFO_CHILD + (
    "module Child { clock c reset r in data:u8 in push:bit in pop:bit out count:u3 "
    "inst grand:FifoChild { data=data push=push pop=pop } count=grand.count } "
    "module Top { clock c reset r in data:u8 in push:bit in pop:bit out count:u3 "
    "inst child:Child { data=data push=push pop=pop } count=child.count }"
)

SIBLINGS = FIFO_CHILD + (
    "module Top { clock c reset r in data:u8 in push:bit in pop:bit out count:u3 "
    "inst fifo0:FifoChild { data=data push=push pop=pop } "
    "inst fifo1:FifoChild { data=data push=push pop=pop } count=fifo0.count }"
)

TOOLS = bool(find_clash_executable() and shutil.which("verilator"))
FORMAL_TOOLS = bool(TOOLS and all(shutil.which(tool) for tool in ("yosys", "sby", "z3")))


def compile_design(source: str):
    module = analyze(parse(source))
    design = build_recursive_formal_design(module)
    return module, design, emit_formal_artifact(module, design)


class ClashRecursiveFifoFormalTests(unittest.TestCase):
    def test_depth_two_uses_current_fifo_state_and_accepted_transfers(self):
        module, design, formal = compile_design(DEPTH_TWO)
        production = emit_artifact(module)
        self.assertEqual(production, emit_artifact(module))
        self.assertNotEqual(production.artifact_hash, formal.formal_artifact_hash)
        fifo = [item for item in design.bindings
                if item.ref.local_semantic_id.startswith("fifo:")]
        self.assertEqual(len(fifo), 6)
        self.assertTrue(all(item.physical_instance_path == ("Top", "child", "grand")
                            for item in fifo))
        self.assertIn("<*> (q_count)", formal.text)
        self.assertIn("<*> (q_enqueue)", formal.text)
        self.assertIn("<*> (q_dequeue)", formal.text)
        self.assertNotIn("<*> (q_count_next)", formal.text)
        self.assertFalse(any("slots" in item.ref.local_semantic_id
                             for item in design.bindings))

    @unittest.skipUnless(TOOLS, "Clash and Verilator are required")
    def test_siblings_have_distinct_validated_v4_locators(self):
        module, design, artifact = compile_design(SIBLINGS)
        with tempfile.TemporaryDirectory() as directory:
            files = generate_verilog(
                artifact.text, "Top_formal", Path(directory), find_clash_executable()
            )
            lint_with_verilator(files, "Top_formal")
            validated = validate_register_formal_artifact(artifact, files)
            counts = [item for item in validated.recursive_bindings
                      if item.local_semantic_id == "fifo:q.count"]
            self.assertEqual(len(counts), 2)
            self.assertEqual({item.physical_instance_path[-1] for item in counts},
                             {"fifo0", "fifo1"})
            self.assertEqual(len({item.formal_observation_token for item in counts}), 2)
            self.assertTrue(all(item.physical_available for item in counts))
            self.assertTrue(all(item.rtl_module == "Top_formal" for item in counts))
            restored = BackendArtifact.from_json(validated.to_json())
            restored_counts = [item for item in restored.recursive_bindings
                               if item.local_semantic_id == "fifo:q.count"]
            self.assertEqual(counts, restored_counts)

            top = next(path for path in files if path.name == "Top_formal.v")
            token = counts[0].formal_observation_token
            wrong = Path(directory) / "Top_formal_wrong_fifo_width.v"
            wrong.write_text(top.read_text().replace(
                f"output wire [2:0] {token}", f"output wire [1:0] {token}"
            ))
            altered = tuple(wrong if path == top else path for path in files)
            rejected = validate_register_formal_artifact(artifact, altered)
            rejected_count = next(
                item for item in rejected.recursive_bindings
                if item.semantic_binding_id == counts[0].semantic_binding_id
            )
            self.assertFalse(rejected_count.physical_available)

    @unittest.skipUnless(TOOLS, "Clash and Verilator are required")
    def test_nested_fifo_push_pop_full_empty_simultaneous_and_reset(self):
        module, design, artifact = compile_design(DEPTH_TWO)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = generate_verilog(
                artifact.text, "Top_formal", root, find_clash_executable()
            )
            validated = validate_register_formal_artifact(artifact, files)
            by_local = {
                item.local_semantic_id: item.formal_observation_token
                for item in validated.recursive_bindings
                if item.physical_instance_path == ("Top", "child", "grand")
            }
            declarations = []
            connections = [".c(c)", ".r(r)", ".data(data)", ".push(push)",
                           ".pop(pop)", ".count(count)"]
            for observation in validated.formal_observations:
                if observation.observation_token is None:
                    continue
                width = "" if observation.width == 1 else f" [{observation.width - 1}:0]"
                declarations.append(f"wire{width} {observation.observation_token};")
                connections.append(
                    f".{observation.observation_token}({observation.observation_token})"
                )
            count = by_local["fifo:q.count"]
            empty = by_local["fifo:q.empty"]
            full = by_local["fifo:q.full"]
            testbench = root / "tb.sv"
            testbench.write_text(f"""
module tb;
  reg c=0, r=1, push=0, pop=0; reg [7:0] data=0; wire [2:0] count;
  {' '.join(declarations)}
  Top_formal dut({', '.join(connections)});
  task tick; begin #1 c=1; #1 c=0; #1; end endtask
  initial begin
    tick(); if ({count} !== 0 || !{empty}) $fatal(1, "reset/empty");
    r=0; data=8'h11; push=1; tick(); if ({count} !== 1) $fatal(1, "push");
    data=8'h22; tick(); if ({count} !== 2) $fatal(1, "second push");
    push=0; pop=1; tick(); if ({count} !== 1) $fatal(1, "pop");
    push=1; pop=1; tick(); if ({count} !== 1) $fatal(1, "simultaneous");
    pop=0; tick(); tick(); tick(); if ({count} !== 4 || !{full}) $fatal(1, "full");
    tick(); if ({count} !== 4) $fatal(1, "blocked full push");
    push=0; pop=1; tick(); if ({count} !== 3) $fatal(1, "drain");
    r=1; pop=0; tick(); if ({count} !== 0 || !{empty}) $fatal(1, "nonempty reset");
    $finish;
  end
endmodule
""")
            obj = root / "obj"
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            built = subprocess.run(
                ("verilator", "--binary", "--timing", "-Wno-fatal", "--Mdir", str(obj),
                 "--top-module", "tb", *(str(path) for path in files), str(testbench)),
                cwd=root, capture_output=True, text=True,
                env=environment,
            )
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            ran = subprocess.run((str(obj / "Vtb"),), cwd=root,
                                 capture_output=True, text=True)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)

    def test_direct_sv_reuses_existing_recursive_fifo_observations(self):
        module = analyze(parse(DEPTH_TWO))
        design = build_recursive_formal_design(module)
        artifact = emit_sv_formal_artifact(module, design)
        fifo_bindings = tuple(
            item
            for item in artifact.recursive_bindings
            if item.physical_instance_path == ("Top", "child", "grand")
            and item.local_semantic_id.startswith("fifo:q.")
        )
        self.assertEqual(len(fifo_bindings), 6)
        self.assertTrue(
            all(item.formal_observation_token for item in fifo_bindings)
        )
        self.assertTrue(artifact.formal_observations)

    @unittest.skipUnless(FORMAL_TOOLS, "Clash, Verilator, Yosys, SBY, and Z3 are required")
    def test_real_nested_fifo_m35_properties_pass(self):
        module, design, artifact = compile_design(DEPTH_TWO)
        with tempfile.TemporaryDirectory() as directory:
            files = generate_verilog(
                artifact.text, "Top_formal", Path(directory), find_clash_executable()
            )
            results = run_recursive_register_formal(
                module, design, artifact, files, depth=8
            )
        fifo_results = [item for item in results
                        if item.physical_instance_path == ("Top", "child", "grand")]
        self.assertEqual(len(fifo_results), 5)
        self.assertTrue(all(item.status is FormalStatus.BOUNDED_PASS
                            for item in fifo_results))

    @unittest.skipUnless(FORMAL_TOOLS, "Clash, Verilator, Yosys, SBY, and Z3 are required")
    def test_fifo1_accounting_mutation_fails_with_sibling_attribution(self):
        module, design, artifact = compile_design(SIBLINGS)
        with tempfile.TemporaryDirectory() as directory:
            files = generate_verilog(
                artifact.text, "Top_formal", Path(directory), find_clash_executable()
            )
            fifo1 = next(item for item in design.instances
                         if item.physical_instance_path[-1] == "fifo1")
            target = next(path for path in files
                          if f"zformalInstance{fifo1.instance_identity[:12]}" in path.name)
            original = target.read_text()
            mutated = original.replace("q_count - 3'd1", "q_count")
            self.assertNotEqual(original, mutated)
            target.write_text(mutated)
            results = run_recursive_register_formal(
                module, design, artifact, files, depth=8
            )
        conservation_ids = {
            item.concrete_property_id for item in design.properties
            if item.property.generated_from == "fifo:q"
            and item.property.temporal_form is TemporalForm.NEXT_CYCLE
            and item.property.expression.startswith("q.count ==")
        }
        conservation = [item for item in results
                        if item.concrete_property_id in conservation_ids]
        fifo0_result = next(item for item in conservation
                            if item.physical_instance_path[-1] == "fifo0")
        fifo1_result = next(item for item in conservation
                            if item.physical_instance_path[-1] == "fifo1")
        self.assertIs(fifo0_result.status, FormalStatus.BOUNDED_PASS)
        self.assertIs(fifo1_result.status, FormalStatus.FAILED)
        self.assertEqual(fifo1_result.instance_identity, fifo1.instance_identity)
        self.assertEqual(fifo1_result.specialization_identity,
                         fifo0_result.specialization_identity)
        self.assertTrue(any("fifo:q.count" in key
                            for key, _ in fifo1_result.object_values))
        self.assertIsNotNone(fifo1_result.source_origin)
        self.assertIsNotNone(fifo1_result.counterexample)
        self.assertIsNotNone(fifo1_result.counterexample.cycle)
        self.assertEqual(fifo1_result.depth, 8)
        self.assertTrue(fifo1_result.counterexample.raw_trace)


if __name__ == "__main__":
    unittest.main()
