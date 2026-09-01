import unittest
from pathlib import Path

from zlang import compile_source
from zlang.backend.manifest import BackendArtifact, publish_artifact
from zlang.ir import (
    PortDirection,
    build_top_aggregate_abi,
    build_top_physical_abi,
)
from zlang.ir.equivalence import BindingSide
from zlang.semantic import SemanticError


ROOT = Path(__file__).resolve().parents[1]


SOURCE = """
struct Payload { addr:uint<8> data:uint<16> }
protocol Bus {
  role source
  role sink
  channel req:rv<Payload> source -> sink
  member irq:bit sink -> source
}
module Child {
  clock clk reset rst
  interface bus:Bus.sink
  bus.req.ready=1
  bus.irq=0
}
module Top {
  clock clk reset rst
  interface bus:Bus.sink
  inst child:Child
  connect bus -> child.bus
}
"""


SHAPES_SOURCE = """
struct Lane { value:u8 valid:bit }
struct Payload { tag:u4 lanes:vec<2,Lane> }
module Shapes {
  in request:Payload
  out response:Payload
  in rx:rv<Payload>
  out tx:rv<Payload>
  response=request
  tx.payload=rx.payload
  tx.valid=rx.valid
  rx.ready=tx.ready
}
"""


class TopAggregateABITests(unittest.TestCase):
    def test_recursive_flattening_and_roles(self):
        module = compile_source(SOURCE).ir
        abi = build_top_aggregate_abi(module)
        self.assertEqual(
            [leaf.external_name for leaf in abi.leaves],
            ["bus_req_payload_addr", "bus_req_payload_data", "bus_req_valid", "bus_req_ready", "bus_irq"],
        )
        self.assertEqual(
            [leaf.direction for leaf in abi.leaves],
            [PortDirection.INPUT, PortDirection.INPUT, PortDirection.INPUT,
             PortDirection.OUTPUT, PortDirection.OUTPUT],
        )
        self.assertEqual(abi.leaves[1].width, 16)

    def test_same_role_delegation_is_explicit(self):
        module = compile_source(SOURCE).ir
        self.assertEqual(len(module.aggregate_protocol_connections), 1)
        self.assertTrue(module.aggregate_protocol_connections[0].delegation)
        self.assertEqual(len(module.hierarchical_connections), 0)

    def test_manifest_v3_round_trip(self):
        module = compile_source(SOURCE).ir
        artifact = publish_artifact(module, "top", backend="clash", selected_ir_identity="top-v3", side=BindingSide.IMPLEMENTATION)
        self.assertEqual(artifact.manifest_version, 3)
        restored = BackendArtifact.from_json(artifact.to_json())
        self.assertEqual(restored.manifest_version, 3)
        self.assertEqual(
            {binding.semantic_signal_id for binding in restored.bindings if binding.signal_kind == "payload"},
            {"aggregate:Top.bus.req.payload.addr", "aggregate:Top.bus.req.payload.data"},
        )

    def test_collision_is_diagnostic(self):
        source = SOURCE.replace("member irq:bit sink -> source", "member irq:bit sink -> source member irq_:bit sink -> source")
        with self.assertRaisesRegex(ValueError, "collision"):
            build_top_aggregate_abi(compile_source(source).ir)

    def test_vector_protocol_leaf_uses_canonical_physical_width(self):
        source = SOURCE.replace(
            "member irq:bit sink -> source",
            "member irq:bit sink -> source member lanes:vec<2,u8> sink -> source",
        ).replace("bus.irq=0", "bus.irq=0 bus.lanes=generate(i in 0..2) 0")
        abi = build_top_aggregate_abi(compile_source(source).ir)
        lanes = next(leaf for leaf in abi.leaves if leaf.member_path == ("bus", "lanes"))
        self.assertEqual(lanes.width, 16)
        self.assertEqual(lanes.signedness, "unsigned")

    def test_complete_top_abi_splits_structs_and_preserves_vector_arrays(self):
        module = compile_source(SHAPES_SOURCE, include_clash=False).ir
        abi = build_top_physical_abi(module)
        leaves = {leaf.leaf_semantic_id: leaf for leaf in abi.leaves}

        self.assertEqual(
            tuple(leaves),
            (
                "port:request.tag",
                "port:request.lanes.value",
                "port:request.lanes.valid",
                "port:response.tag",
                "port:response.lanes.value",
                "port:response.lanes.valid",
                "port:rx.payload.tag",
                "port:rx.payload.lanes.value",
                "port:rx.payload.lanes.valid",
                "port:rx.valid",
                "port:rx.ready",
                "port:tx.payload.tag",
                "port:tx.payload.lanes.value",
                "port:tx.payload.lanes.valid",
                "port:tx.valid",
                "port:tx.ready",
            ),
        )
        values = leaves["port:request.lanes.value"]
        self.assertEqual(values.external_name, "request_lanes_value")
        self.assertEqual(str(values.canonical_type), "vec<2,u8>")
        self.assertEqual(values.array_dimensions, (2,))
        self.assertEqual(
            tuple((item.indices, item.msb, item.lsb) for item in values.packed_element_slices),
            (((0,), 17, 10), ((1,), 8, 1)),
        )
        # A vector of structs becomes one typed array per field. Its packed
        # slices are intentionally non-contiguous in the private AoS core.
        self.assertIsNone(values.packed_msb)
        self.assertIsNone(values.packed_lsb)
        self.assertEqual(leaves["port:rx.ready"].direction, PortDirection.OUTPUT)
        self.assertEqual(leaves["port:tx.ready"].direction, PortDirection.INPUT)

    def test_aggregate_wire_struct_is_a_generic_leaf_projection(self):
        source = """
        struct Status { code:u4 lanes:vec<2,u8> }
        protocol Bus {
          role source role sink
          member status:Status source -> sink
        }
        module Top {
          interface bus:Bus.source
          bus.status=Status { code=3 lanes=generate(i in 0..2) i }
        }
        """
        abi = build_top_physical_abi(
            compile_source(source, include_clash=False).ir
        )
        self.assertEqual(
            tuple((leaf.leaf_semantic_id, leaf.external_name) for leaf in abi.leaves),
            (
                ("aggregate:Top.bus.status.code", "bus_status_code"),
                ("aggregate:Top.bus.status.lanes", "bus_status_lanes"),
            ),
        )
        lanes = abi.leaves[1]
        self.assertEqual(lanes.array_dimensions, (2,))
        self.assertEqual(lanes.packed_root_external_name, "bus__status")

    def test_request_response_payload_structs_are_split_for_both_roles(self):
        source = (ROOT / "examples/hierarchical_request_response_m40.zl").read_text()
        requester = build_top_physical_abi(
            compile_source(source, top="Requester", include_clash=False).ir
        )
        responder = build_top_physical_abi(
            compile_source(source, top="Responder", include_clash=False).ir
        )
        request = {
            leaf.leaf_semantic_id: leaf for leaf in requester.leaves
        }
        response = {
            leaf.leaf_semantic_id: leaf for leaf in responder.leaves
        }
        self.assertEqual(
            request["port:bus.request.payload.addr"].direction,
            PortDirection.OUTPUT,
        )
        self.assertEqual(
            request["port:bus.response.payload.data"].direction,
            PortDirection.INPUT,
        )
        self.assertEqual(
            response["port:bus.request.payload.addr"].direction,
            PortDirection.INPUT,
        )
        self.assertEqual(
            response["port:bus.response.payload.data"].direction,
            PortDirection.OUTPUT,
        )
        self.assertEqual(
            request["port:bus.request.payload.addr"].packed_root_external_name,
            "bus_request_payload",
        )


if __name__ == "__main__":
    unittest.main()
