import unittest

from zlang.costs import UnifiedConstraint, extract_best
from zlang.ir.expressions import CostMetric
from zlang.ir.interfaces import ConnectionAdapter, InterfaceProtocol
from zlang.protocols import (
    ProtocolEndpoint,
    ProtocolLegalityError,
    ProtocolTraceChecker,
    drain_checker,
    candidate_cost,
    relate_protocol,
)


class ProtocolM33Tests(unittest.TestCase):
    def rv(self, domain="clk"):
        return ProtocolEndpoint(InterfaceProtocol.READY_VALID, "u2", "forward", domain, "rst")

    def credit(self, domain="clk"):
        return ProtocolEndpoint(InterfaceProtocol.CREDIT, "u2", "forward", domain, "rst", 2)

    def test_identical_rv_and_buffer_relation(self):
        direct = relate_protocol(self.rv(), self.rv())
        buffered = relate_protocol(self.rv(), self.rv(), buffer_depth=2)
        self.assertTrue(direct.equivalent and buffered.equivalent)
        self.assertIn("payload sequence", buffered.preserved_observations)
        self.assertIn("ready timing", buffered.changed_observations)
        self.assertNotEqual(direct.identity, buffered.identity)

    def test_credit_adapters_and_capacity(self):
        relation = relate_protocol(self.credit(), self.rv(), adapter=ConnectionAdapter.CREDIT_TO_READY_VALID, buffer_depth=2)
        self.assertTrue(relation.equivalent)
        with self.assertRaisesRegex(ProtocolLegalityError, "insufficient adapter buffer"):
            relate_protocol(self.credit(), self.rv(), adapter=ConnectionAdapter.CREDIT_TO_READY_VALID, buffer_depth=1)

    def test_domains_and_payload_are_rejected(self):
        with self.assertRaisesRegex(ProtocolLegalityError, "clock-domain"):
            relate_protocol(self.rv("a"), self.rv("b"))
        with self.assertRaisesRegex(ProtocolLegalityError, "payload"):
            relate_protocol(self.rv(), ProtocolEndpoint(InterfaceProtocol.READY_VALID, "u3", "forward", "clk", "rst"))
        with self.assertRaisesRegex(ProtocolLegalityError, "direction"):
            relate_protocol(
                self.rv(),
                ProtocolEndpoint(
                    InterfaceProtocol.READY_VALID,
                    "u2",
                    "reverse",
                    "clk",
                    "rst",
                ),
            )

    def test_variable_latency_is_unknown_to_fixed_latency_constraints(self):
        buffered = relate_protocol(self.rv(), self.rv(), buffer_depth=2)
        cost = candidate_cost(buffered)
        self.assertIsNone(cost.latency.value)
        with self.assertRaisesRegex(ValueError, "cannot be proven"):
            extract_best(
                [("buffered", cost)],
                constraints=(
                    UnifiedConstraint(CostMetric.LATENCY, maximum=2),
                ),
            )

    def test_trace_conservation_order_and_drain(self):
        checker = ProtocolTraceChecker(2)
        checker.observe_input_transfer(0)
        checker.observe_input_transfer(1)
        self.assertFalse(checker.observe_input_transfer(2))
        checker.observe_output_transfer(0)
        checker.observe_input_transfer(2)
        result = drain_checker(checker)
        self.assertTrue(result.safe)
        self.assertEqual(result.input_payloads, (0, 1, 2))
        self.assertEqual(result.output_payloads, (0, 1, 2))

    def test_trace_rejects_unknown_output_and_wrong_order(self):
        checker = ProtocolTraceChecker(2)
        self.assertFalse(checker.observe_output_transfer(7))
        checker.observe_input_transfer(1)
        self.assertFalse(checker.observe_output_transfer(2))
        self.assertFalse(checker.finish().safe)

    def test_reset_defines_new_epoch(self):
        checker = ProtocolTraceChecker(2)
        checker.observe_input_transfer(1)
        checker.reset()
        checker.observe_input_transfer(2)
        result = drain_checker(checker)
        self.assertEqual(result.output_payloads, (2,))


if __name__ == "__main__":
    unittest.main()
