import unittest

from zlang.compiler import compile_source
from zlang.formal import build_formal_design, build_recursive_formal_design
from zlang.ir import Ownership, PropertyKind, TemporalForm


class FormalCreditDirectionTests(unittest.TestCase):
    def _credit_properties(self, source: str):
        design = build_formal_design(
            compile_source(source).ir
        )
        properties = tuple(
            item
            for item in design.properties
            if item.generated_from and item.generated_from.startswith("credit:")
        )
        return design, properties

    def test_sender_uses_available_credits_and_environment_return_assumption(self):
        design, properties = self._credit_properties(
            """
            module Sender {
                clock clk
                reset rst
                in payload: u8
                in request: bit
                out tx: credit<u8,2>
                tx.payload = payload
                tx.send = request
            }
            """
        )
        self.assertEqual(len(properties), 5)
        self.assertTrue(all(item.non_executable_reason is None for item in properties))
        by_expression = {item.expression: item for item in properties}

        transfer = by_expression["tx.send == 0 || tx.credits > 0"]
        self.assertIs(transfer.kind, PropertyKind.ASSERTION)
        self.assertIs(transfer.ownership, Ownership.SOURCE_ENDPOINT)

        return_capacity = by_expression[
            "tx.return == 0 || tx.send == 1 || tx.credits < 2"
        ]
        self.assertIs(return_capacity.kind, PropertyKind.ASSUMPTION)
        self.assertIs(return_capacity.ownership, Ownership.ENVIRONMENT)

        conservation = by_expression[
            "tx.credits == previous(tx.credits - tx.send + tx.return)"
        ]
        self.assertIs(conservation.kind, PropertyKind.ASSERTION)
        self.assertIs(conservation.temporal_form, TemporalForm.NEXT_CYCLE)
        self.assertIn("previous(reset)", conservation.predicate.render())
        self.assertEqual(
            set(conservation.relevant_signals),
            {"reset", "port:tx.credits", "port:tx.send", "port:tx.return"},
        )

        reset = by_expression["previous(rst) -> tx.credits == 2"]
        self.assertIs(reset.kind, PropertyKind.ASSERTION)
        self.assertIsNone(reset.reset_condition)
        self.assertEqual(
            set(reset.relevant_signals), {"reset", "port:tx.credits"}
        )

    def test_receiver_uses_occupancy_and_environment_send_assumption(self):
        design, properties = self._credit_properties(
            """
            module Receiver {
                clock clk
                reset rst
                in release: bit
                in rx: credit<u8,2>
                rx.return = release
            }
            """
        )
        self.assertEqual(len(properties), 5)
        self.assertTrue(all(item.non_executable_reason is None for item in properties))
        by_expression = {item.expression: item for item in properties}

        returned = by_expression[
            "rx.return == 0 || rx.send == 1 || rx.occupancy > 0"
        ]
        self.assertIs(returned.kind, PropertyKind.ASSERTION)
        self.assertIs(returned.ownership, Ownership.SINK_ENDPOINT)

        send_capacity = by_expression[
            "rx.send == 0 || rx.return == 1 || rx.occupancy < 2"
        ]
        self.assertIs(send_capacity.kind, PropertyKind.ASSUMPTION)
        self.assertIs(send_capacity.ownership, Ownership.ENVIRONMENT)

        conservation = by_expression[
            "rx.occupancy == previous(rx.occupancy + rx.send - rx.return)"
        ]
        self.assertIs(conservation.kind, PropertyKind.ASSERTION)
        self.assertIs(conservation.temporal_form, TemporalForm.NEXT_CYCLE)
        self.assertIn("previous(reset)", conservation.predicate.render())
        self.assertEqual(
            set(conservation.relevant_signals),
            {"reset", "port:rx.occupancy", "port:rx.send", "port:rx.return"},
        )

        reset = by_expression["previous(rst) -> rx.occupancy == 0"]
        self.assertIs(reset.kind, PropertyKind.ASSERTION)
        self.assertIsNone(reset.reset_condition)
        self.assertEqual(
            set(reset.relevant_signals), {"reset", "port:rx.occupancy"}
        )

        occupancy = next(
            item for item in design.bindings
            if item.semantic_signal_id == "port:rx.occupancy"
        )
        self.assertEqual(occupancy.width, 2)
        self.assertEqual(occupancy.direction, "internal")

    def test_receiver_occupancy_survives_recursive_semantic_binding(self):
        module = compile_source(
            "module Receiver { clock clk reset rst in release:bit "
            "in rx:credit<u8,2> out observed:u8 "
            "rx.return=release observed=rx.payload }",
        ).ir
        design = build_recursive_formal_design(module)
        occupancy = next(
            item for item in design.bindings
            if item.ref.local_semantic_id == "port:rx.occupancy"
        )
        self.assertEqual(occupancy.width, 2)
        self.assertEqual(occupancy.signedness, "unsigned")
        self.assertEqual(occupancy.direction, "internal")
        receiver_properties = tuple(
            item for item in design.properties
            if (item.property.generated_from or "").startswith("credit:rx")
        )
        self.assertEqual(len(receiver_properties), 5)
        self.assertTrue(
            all(item.property.non_executable_reason is None
                for item in receiver_properties)
        )
        self.assertTrue(all(
            not item.property.predicate.observation_ids()
            or any(
                ref.local_semantic_id == "port:rx.occupancy"
                for ref in item.object_refs
            )
            for item in receiver_properties
            if "occupancy" in item.property.expression
        ))

if __name__ == "__main__":
    unittest.main()
