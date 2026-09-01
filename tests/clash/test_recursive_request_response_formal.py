"""Structured Clash recursive-formal request/response ledger coverage."""

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


ROOT = Path(__file__).resolve().parents[2]
SIMPLE_DMA = (ROOT / "examples/simple_dma_m40.zl").read_text()
SMALL_RR = (ROOT / "examples/hierarchical_request_response_m40.zl").read_text().replace(
    "max_outstanding 1", "max_outstanding 2"
).replace(
    "connect requester.bus -> responder.bus",
    "connect requester.bus -> responder.bus { request_buffer 1 response_buffer 1 }",
)
SMALL_RR_UNBUFFERED = (
    ROOT / "examples/hierarchical_request_response_m40.zl"
).read_text().replace("max_outstanding 1", "max_outstanding 2")
TOP = (
    "module Top { clock clk reset rst in base:u8 in data:u8 in start:bit "
    "in accept:bit out busy:bit inst channel:SimpleDMA<8> { base data start accept } "
    "busy=channel.busy }"
)
SIBLINGS = (
    "module Top { clock clk reset rst in fire:bit in accept_request:bit "
    "in accept_response:bit in data:u8 out response_seen:bit "
    "inst channel0:HierarchicalRequestResponse { fire accept_request accept_response data } "
    "inst channel1:HierarchicalRequestResponse { fire accept_request accept_response data } "
    "response_seen=channel0.response_seen }"
)

MIXED_RR_STATE_RULES = """
struct MixedRequest { data:u8 }
struct MixedResponse { data:u8 }

module MixedRequester {
    clock clk reset rst
    in fire, accept_response, clear : bit
    interface bus:request_response<MixedRequest,MixedResponse> {
        max_outstanding 1 ordering in_order
    }
    reg count:u8=0
    priority {
        clear_count: when clear { count <- 0 }
        advance: when bus.response.transfer {
            count <- truncate<8>(count + 1)
        }
    }
    bus.request.payload=MixedRequest{data=1}
    bus.request.valid=fire
    bus.response.ready=accept_response
}

module MixedResponder {
    clock clk reset rst
    in accept_request:bit in data:u8 out seen:bit
    interface bus:request_response<MixedRequest,MixedResponse> {
        max_outstanding 1 ordering in_order
    }
    bus.request.ready=accept_request
    bus.response.payload=MixedResponse{data=data}
    bus.response.valid=bus.request.transfer
    seen=bus.response.transfer
}

module MixedRRStateRules {
    clock clk reset rst
    in fire, accept_request, accept_response, clear:bit
    in data:u8 out seen:bit
    inst requester:MixedRequester{fire accept_response clear}
    inst responder:MixedResponder{accept_request data}
    connect requester.bus -> responder.bus
    seen=responder.seen
}
"""

TOOLS = bool(find_clash_executable() and shutil.which("verilator"))
FORMAL_TOOLS = bool(TOOLS and all(shutil.which(tool) for tool in ("yosys", "sby", "z3")))


def compile_design(source: str):
    module = analyze(parse(source))
    design = build_recursive_formal_design(module)
    return module, design, emit_formal_artifact(module, design)


def rr_bindings(artifact, path=None):
    return [item for item in artifact.recursive_bindings
            if item.local_semantic_id.startswith("rr:")
            and (path is None or item.physical_instance_path == path)]


class ClashRecursiveRequestResponseFormalTests(unittest.TestCase):
    def test_mixed_rr_child_state_and_rule_use_one_formal_product(self):
        module, design, formal = compile_design(MIXED_RR_STATE_RULES)
        production_before = emit_artifact(module)
        production_after = emit_artifact(module)
        self.assertEqual(production_before.text, production_after.text)
        self.assertEqual(
            production_before.artifact_hash, production_after.artifact_hash
        )

        child = [
            item for item in design.bindings
            if item.physical_instance_path == ("MixedRRStateRules", "requester")
            and item.ref.local_semantic_id.startswith(("register:", "rule:"))
        ]
        self.assertEqual(
            [item.ref.local_semantic_id for item in child],
            [
                "register:count",
                "rule:clear_count.fire",
                "rule:advance.fire",
            ],
        )
        self.assertIn("mixedRequesterComponentFormal", formal.text)
        self.assertIn("<$> requester_component_output", formal.text)
        self.assertNotIn("count_next)", formal.text)
        self.assertNotIn("lane[", formal.text)

    @unittest.skipUnless(
        FORMAL_TOOLS, "Clash, Verilator, Yosys, SBY, and Z3 are required"
    )
    def test_mixed_rr_ledger_state_and_rule_properties_execute(self):
        module, design, artifact = compile_design(MIXED_RR_STATE_RULES)
        with tempfile.TemporaryDirectory() as directory:
            files = generate_verilog(
                artifact.text,
                "MixedRRStateRules_formal",
                Path(directory),
                find_clash_executable(),
            )
            lint_with_verilator(files, "MixedRRStateRules_formal")
            validated = validate_register_formal_artifact(artifact, files)
            state_and_rules = [
                item for item in validated.recursive_bindings
                if item.physical_instance_path
                == ("MixedRRStateRules", "requester")
                and item.local_semantic_id.startswith(("register:", "rule:"))
            ]
            self.assertEqual(len(state_and_rules), 3)
            self.assertTrue(all(item.physical_available for item in state_and_rules))
            self.assertEqual(len({item.instance_identity for item in state_and_rules}), 1)

            selected = frozenset(
                item.concrete_property_id for item in design.properties
                if item.property.generated_from in {
                    "priority:clear_count>advance",
                    "register:count:reset",
                }
                or (
                    item.physical_instance_path == ("MixedRRStateRules",)
                    and item.property.generated_from.startswith("request_response:")
                    and "outstanding <= 1" in item.property.expression
                )
            )
            results = run_recursive_register_formal(
                module,
                design,
                artifact,
                files,
                depth=6,
                property_ids=selected,
            )
            self.assertEqual(len(results), 3)
            self.assertTrue(
                all(item.status is FormalStatus.BOUNDED_PASS for item in results)
            )

            advance = next(
                item for item in state_and_rules
                if item.local_semantic_id == "rule:advance.fire"
            )
            top = next(
                path for path in files if path.name == "MixedRRStateRules_formal.v"
            )
            original = top.read_text()
            assignment = next(
                line for line in original.splitlines()
                if line.strip().startswith(
                    f"assign {advance.formal_observation_token} ="
                )
            )
            top.write_text(original.replace(
                assignment,
                f"  assign {advance.formal_observation_token} = 1'b1;",
                1,
            ))
            priority_id = next(
                item.concrete_property_id for item in design.properties
                if item.property.generated_from == "priority:clear_count>advance"
            )
            (failed,) = run_recursive_register_formal(
                module,
                design,
                artifact,
                files,
                depth=6,
                property_ids=frozenset({priority_id}),
            )
        self.assertIs(failed.status, FormalStatus.FAILED)
        self.assertEqual(
            failed.physical_instance_path,
            ("MixedRRStateRules", "requester"),
        )
        self.assertIsNotNone(failed.counterexample)

    def test_simple_dma_materializes_only_current_frozen_ledger_state(self):
        module, design, formal = compile_design(SIMPLE_DMA)
        production = emit_artifact(module)
        self.assertEqual(production, emit_artifact(module))
        self.assertNotEqual(production.artifact_hash, formal.formal_artifact_hash)
        by_suffix = {item.ref.local_semantic_id.rsplit(":", 1)[1]: item
                     for item in design.bindings
                     if item.ref.local_semantic_id.startswith("rr:")}
        self.assertEqual(set(by_suffix), {
            "outstanding", "request_accept", "response_consume",
            "request_occupancy", "response_occupancy",
        })
        self.assertEqual(by_suffix["outstanding"].width, 2)
        self.assertEqual(by_suffix["request_occupancy"].width, 3)
        self.assertEqual(by_suffix["response_occupancy"].width, 2)
        self.assertIn("<*> (rr_engine_mem_engine_outstanding)", formal.text)
        self.assertNotIn("<*> (rr_engine_mem_engine_outstanding_next)", formal.text)
        self.assertNotIn("waiting_response)", formal.text)

    def test_max_outstanding_one_two_four_preserves_frozen_bounds(self):
        for maximum in (1, 2, 4):
            source = SIMPLE_DMA.replace("max_outstanding 2", f"max_outstanding {maximum}")
            module = analyze(parse(source))
            design = build_recursive_formal_design(module)
            bounds = next(item.property.expression for item in design.properties
                          if item.property.generated_from.startswith("request_response:")
                          and " <= " in item.property.expression
                          and "outstanding >=" in item.property.expression)
            self.assertIn(f"<= {maximum}", bounds)
            outstanding = next(item for item in design.bindings
                               if item.ref.local_semantic_id.endswith(":outstanding"))
            self.assertEqual(outstanding.width, max(1, maximum.bit_length()))

    @unittest.skipUnless(TOOLS, "Clash and Verilator are required")
    def test_depth_two_siblings_and_locator_width_validation(self):
        module, design, artifact = compile_design(SMALL_RR + "\n" + SIBLINGS)
        with tempfile.TemporaryDirectory() as directory:
            files = generate_verilog(
                artifact.text, "Top_formal", Path(directory), find_clash_executable()
            )
            lint_with_verilator(files, "Top_formal")
            validated = validate_register_formal_artifact(artifact, files)
            outstanding = [item for item in rr_bindings(validated)
                           if item.local_semantic_id.endswith(":outstanding")]
            self.assertEqual(len(outstanding), 2)
            self.assertEqual({item.physical_instance_path for item in outstanding},
                             {("Top", "channel0"), ("Top", "channel1")})
            self.assertEqual(len({item.formal_observation_token
                                  for item in outstanding}), 2)
            self.assertTrue(all(item.physical_available for item in outstanding))
            restored = BackendArtifact.from_json(validated.to_json())
            self.assertEqual(rr_bindings(validated), rr_bindings(restored))

            target = outstanding[0]
            top = next(path for path in files if path.name == "Top_formal.v")
            wrong = Path(directory) / "Top_formal_wrong_rr_width.v"
            wrong.write_text(top.read_text().replace(
                f"output wire [1:0] {target.formal_observation_token}",
                f"output wire {target.formal_observation_token}",
            ))
            rejected = validate_register_formal_artifact(
                artifact, tuple(wrong if path == top else path for path in files)
            )
            rejected_target = next(item for item in rejected.recursive_bindings
                                   if item.semantic_binding_id == target.semantic_binding_id)
            self.assertFalse(rejected_target.physical_available)

    @unittest.skipUnless(TOOLS, "Clash and Verilator are required")
    def test_simple_dma_accounting_buffers_simultaneous_and_reset_epoch(self):
        module, design, artifact = compile_design(SIMPLE_DMA)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = generate_verilog(
                artifact.text, "SimpleDMA_formal", root, find_clash_executable()
            )
            validated = validate_register_formal_artifact(artifact, files)
            values = {item.local_semantic_id.rsplit(":", 1)[1]: item.signal_token
                      for item in rr_bindings(validated)}
            declarations, connections = [], [
                ".clk(clk)", ".rst(rst)", ".base(base)", ".data(data)",
                ".start(start)", ".accept(accept)", ".busy(busy)",
            ]
            for observation in validated.formal_observations:
                if observation.observation_token is None:
                    continue
                width = "" if observation.width == 1 else f" [{observation.width - 1}:0]"
                declarations.append(f"wire{width} {observation.observation_token};")
                connections.append(
                    f".{observation.observation_token}({observation.observation_token})"
                )
            o, rq, rp = (values[name] for name in (
                "outstanding", "request_occupancy", "response_occupancy"
            ))
            accepted, consumed = (values[name] for name in (
                "request_accept", "response_consume"
            ))
            tb = root / "tb.sv"
            tb.write_text(f"""
module tb;
  reg clk=0,rst=1,start=0,accept=0; reg [7:0] base=0,data=1; wire busy;
  {' '.join(declarations)}
  SimpleDMA_formal dut({', '.join(connections)});
  task tick; begin #1 clk=1; #1; #1 clk=0; #1; end endtask
  initial begin
    tick(); if ({o} != 0 || {rq} != 0 || {rp} != 0) $fatal(1,"idle reset");
    rst=0; start=1; tick(); if ({rq} != 1 || {o} != 0) $fatal(1,"admission is not acceptance");
    tick(); tick(); if ({rq} != 3 || {o} != 0) $fatal(1,"request fill/stall");
    start=0; accept=1; tick(); if ({accepted} != 1 || {o} != 1) $fatal(1,"responder acceptance");
    start=1; tick(); if ({rp} != 1 || {consumed} != 1 || {o} != 2) $fatal(1,"response buffering");
    tick(); if (!{accepted} || !{consumed} || {o} != 1) $fatal(1,"same-cycle setup");
    tick(); if ({o} != 1) $fatal(1,"same-cycle accounting");
    rst=1; tick(); if ({o} != 0 || {rq} != 0 || {rp} != 0) $fatal(1,"reset epoch");
    $finish;
  end
endmodule
""")
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            obj = root / "obj"
            built = subprocess.run(
                ("verilator", "--binary", "--timing", "-Wno-fatal", "--Mdir", str(obj),
                 "--top-module", "tb", *(str(path) for path in files), str(tb)),
                cwd=root, env=environment, capture_output=True, text=True,
            )
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            ran = subprocess.run((str(obj / "Vtb"),), cwd=root,
                                 capture_output=True, text=True)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)

    @unittest.skipUnless(FORMAL_TOOLS, "Clash, Verilator, Yosys, SBY, and Z3 are required")
    def test_simple_dma_real_recursive_m35_passes(self):
        module, design, artifact = compile_design(SIMPLE_DMA)
        with tempfile.TemporaryDirectory() as directory:
            files = generate_verilog(
                artifact.text, "SimpleDMA_formal", Path(directory), find_clash_executable()
            )
            results = run_recursive_register_formal(
                module, design, artifact, files, depth=8
            )
        rr_results = [item for item in results
                      if item.object_values and ":rr:" in item.object_values[0][0]]
        self.assertEqual(len(rr_results), 5)
        self.assertTrue(all(item.status is FormalStatus.BOUNDED_PASS
                            for item in rr_results))

    @unittest.skipUnless(FORMAL_TOOLS, "Clash, Verilator, Yosys, SBY, and Z3 are required")
    def test_channel1_connection_ledger_mutation_fails_with_sibling_attribution(self):
        module, design, artifact = compile_design(SMALL_RR_UNBUFFERED + "\n" + SIBLINGS)
        with tempfile.TemporaryDirectory() as directory:
            files = generate_verilog(
                artifact.text, "Top_formal", Path(directory), find_clash_executable()
            )
            channel1 = next(item for item in design.instances
                            if item.physical_instance_path == ("Top", "channel1"))
            target = next(path for path in files
                          if f"zformalInstance{channel1.instance_identity[:12]}" in path.name)
            original = target.read_text()
            mutated = original.replace(
                "rr_requester_bus_requester_outstanding + 2'd1",
                "rr_requester_bus_requester_outstanding",
            )
            self.assertNotEqual(original, mutated)
            target.write_text(mutated)
            conservation_ids = {
                item.concrete_property_id for item in design.properties
                if item.property.generated_from.startswith("request_response:")
                and item.property.temporal_form is TemporalForm.NEXT_CYCLE
                and "outstanding == previous" in item.property.expression
            }
            results = run_recursive_register_formal(
                module, design, artifact, files, depth=9,
                property_ids=frozenset(conservation_ids),
            )
        conservation = [item for item in results
                        if item.concrete_property_id in conservation_ids]
        channel0_result = next(item for item in conservation
                               if item.physical_instance_path == ("Top", "channel0"))
        channel1_result = next(item for item in conservation
                               if item.physical_instance_path == ("Top", "channel1"))
        self.assertIs(channel0_result.status, FormalStatus.BOUNDED_PASS)
        self.assertIs(channel1_result.status, FormalStatus.FAILED)
        self.assertEqual(channel1_result.instance_identity, channel1.instance_identity)
        self.assertEqual(channel1_result.specialization_identity,
                         channel0_result.specialization_identity)
        self.assertTrue(any(key.endswith(":outstanding")
                            for key, _ in channel1_result.object_values))
        self.assertIsNotNone(channel1_result.source_origin)
        self.assertIsNotNone(channel1_result.counterexample)
        self.assertIsNotNone(channel1_result.counterexample.cycle)
        self.assertEqual(channel1_result.depth, 9)

    @unittest.skipUnless(TOOLS, "Clash and Verilator are required")
    def test_direct_sv_parity_for_all_request_response_observations(self):
        module = analyze(parse(SIMPLE_DMA))
        design = build_recursive_formal_design(module)
        clash = emit_formal_artifact(module, design)
        direct = emit_sv_formal_artifact(module, design)
        with tempfile.TemporaryDirectory() as directory:
            files = generate_verilog(
                clash.text, "SimpleDMA_formal", Path(directory), find_clash_executable()
            )
            clash = validate_register_formal_artifact(clash, files)
        clash_available = {item.local_semantic_id for item in rr_bindings(clash)
                           if item.physical_available}
        direct_available = {item.local_semantic_id for item in rr_bindings(direct)
                            if item.physical_available}
        self.assertEqual(direct_available, clash_available)
        for suffix in (
            "outstanding",
            "request_accept",
            "response_consume",
            "request_occupancy",
            "response_occupancy",
        ):
            self.assertTrue(any(
                item.endswith(f":{suffix}") for item in direct_available
            ))


if __name__ == "__main__":
    unittest.main()
