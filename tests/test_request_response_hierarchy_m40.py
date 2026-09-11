import unittest
from pathlib import Path
import tempfile
import os
import shutil
import subprocess

from zlang.backend.systemverilog.emitter import emit, emit_artifact
from zlang.ir.formal import generate_properties
from zlang.parser import ParseError, parse
from zlang.semantic import SemanticError, analyze
from zlang.toolchain import lint_with_verilator


ROOT = Path(__file__).resolve().parents[1]


class RequestResponseHierarchyM40Tests(unittest.TestCase):
    def source(self) -> str:
        return (ROOT / "examples/hierarchical_request_response_m40.zhl").read_text()

    def test_requester_responder_channels_are_elaborated(self) -> None:
        module = analyze(parse(self.source()))
        self.assertEqual(len(module.hierarchical_connections), 2)
        self.assertEqual(
            {(edge.source.channel.value, edge.source.owner, edge.destination.owner)
             for edge in module.hierarchical_connections},
            {("request", "requester", "responder"),
             ("response", "responder", "requester")},
        )
        self.assertEqual(module.children[0].request_responses[0].role.value, "requester")
        self.assertEqual(module.children[1].request_responses[0].role.value, "responder")

    def test_payload_and_contract_mismatches_are_rejected(self) -> None:
        bad = self.source().replace("interface bus: request_response<Request, Response>",
                                    "interface bus: request_response<u16, Response>", 1)
        with self.assertRaisesRegex(SemanticError, "cannot assign Request expression"):
            analyze(parse(bad))
        bad = self.source().replace("max_outstanding 1", "max_outstanding 2", 1)
        with self.assertRaisesRegex(SemanticError, "max_outstanding"):
            analyze(parse(bad))

    def test_manifest_publishes_both_channel_bindings(self) -> None:
        module = analyze(parse(self.source()))
        artifact = emit_artifact(module, selected_ir_identity="rr-m40")
        ids = {binding.semantic_signal_id for binding in artifact.bindings}
        self.assertIn("endpoint:requester.bus.request.payload", ids)
        self.assertIn("endpoint:requester.bus.request.ready", ids)
        self.assertIn("endpoint:responder.bus.response.payload", ids)
        self.assertIn("endpoint:responder.bus.response.ready", ids)

    def test_directional_buffering_is_independent(self) -> None:
        source = self.source().replace(
            "connect requester.bus -> responder.bus",
            "connect requester.bus -> responder.bus { request_buffer 2 response_buffer 1 }",
        )
        module = analyze(parse(source))
        request, response = module.hierarchical_connections
        self.assertEqual((request.request_buffer_depth, request.response_buffer_depth), (2, 0))
        self.assertEqual((response.request_buffer_depth, response.response_buffer_depth), (0, 1))

    def test_generic_buffer_on_request_response_is_rejected(self) -> None:
        source = self.source().replace(
            "connect requester.bus -> responder.bus",
            "connect requester.bus -> responder.bus { buffer 2 }",
        )
        with self.assertRaisesRegex(SemanticError, "ambiguous"):
            analyze(parse(source))

    def test_multi_outstanding_in_order_descriptor_and_manifest(self) -> None:
        source = self.source().replace("max_outstanding 1", "max_outstanding 2")
        module = analyze(parse(source))
        self.assertEqual(len(module.request_response_connections), 1)
        descriptor = module.request_response_connections[0]
        self.assertEqual(descriptor.max_outstanding, 2)
        self.assertEqual(descriptor.ordering.value, "in_order")
        self.assertEqual(descriptor.requester, "requester")
        self.assertEqual(descriptor.responder, "responder")
        self.assertEqual(descriptor.reset_epoch_policy, "synchronous_shared")
        artifact = emit_artifact(module, selected_ir_identity="rr-m40-n2")
        binding_ids = {binding.semantic_signal_id for binding in artifact.bindings}
        self.assertIn(f"{descriptor.semantic_id}:outstanding", binding_ids)
        self.assertIn(f"{descriptor.semantic_id}:waiting_response", binding_ids)

    def test_multi_outstanding_four_emits_parent_tracker(self) -> None:
        source = self.source().replace("max_outstanding 1", "max_outstanding 4")
        module = analyze(parse(source))
        generated = emit(module)
        self.assertIn(
            "logic [2:0] rr_requester_bus_requester_outstanding;",
            generated,
        )
        self.assertIn(
            "rr_requester_bus_requester_outstanding < 3'd4",
            generated,
        )


    def test_formal_descriptor_properties_and_bindings(self) -> None:
        source = self.source().replace("max_outstanding 1", "max_outstanding 2")
        design = generate_properties(analyze(parse(source)))
        # Five executable count/accounting obligations remain after removing
        # the old duplicate pseudo-"in_order" string assertion. Exact payload
        # order needs a typed accepted-request shadow queue and is not guessed.
        self.assertEqual(sum(p.generated_from.startswith("request_response:") for p in design.properties), 5)
        self.assertTrue(any(b.semantic_signal_id.endswith(":outstanding") for b in design.bindings))

    def test_multi_outstanding_zero_is_rejected(self) -> None:
        source = self.source().replace("max_outstanding 1", "max_outstanding 0")
        # WIDTH intentionally excludes zero; this is still a hard rejection of
        # the invalid non-positive capacity before semantic elaboration.
        with self.assertRaises(ParseError):
            analyze(parse(source))



if __name__ == "__main__":
    unittest.main()
