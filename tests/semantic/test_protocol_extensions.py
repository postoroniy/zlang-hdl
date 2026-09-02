from pathlib import Path
import unittest

from zlang.ir.arbitration import ArbitrationPolicy, GrantScope
from zlang.ir.expressions import VirtualChannelCreditRef, VectorIndex
from zlang.ir.interfaces import InterfaceProtocol, VirtualChannelCreditSignal
from zlang.ir.types import UIntType, VecType
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[2]


class ProtocolExtensionSemanticTests(unittest.TestCase):
    def test_packet_policy_and_grant_scope_are_typed_ir(self) -> None:
        module = analyze(
            parse((ROOT / "examples/packet_round_robin.zhl").read_text())
        )
        arbiter = module.arbiters[0]

        self.assertEqual(arbiter.policy, ArbitrationPolicy.ROUND_ROBIN)
        self.assertEqual(arbiter.grant_scope, GrantScope.PACKET)
        self.assertEqual([source.name for source in arbiter.sources], ["source_a", "source_b"])
        self.assertEqual(arbiter.destination.name, "tx")
        self.assertTrue(
            all(port.protocol is InterfaceProtocol.PACKET for port in module.ports)
        )

    def test_vc_bounds_and_per_channel_credit_vector_are_explicit(self) -> None:
        module = analyze(
            parse(
                "module V { clock c reset r out tx:vc_credit<u8,4,3> "
                "out empty:bit tx.payload=0 tx.vc=0 tx.send=0 "
                "empty=tx.credits[2] == 0 }"
            )
        )
        tx = module.ports[0]
        expression = module.assignments[-1].expression

        self.assertEqual(tx.virtual_channels, 4)
        self.assertEqual(tx.capacity, 3)
        self.assertIsInstance(expression.left, VectorIndex)
        self.assertIsInstance(expression.left.expression, VirtualChannelCreditRef)
        self.assertEqual(
            expression.left.expression.signal,
            VirtualChannelCreditSignal.CREDITS,
        )
        self.assertEqual(
            expression.left.expression.type,
            VecType(4, UIntType(2)),
        )

    def test_vc_count_must_be_a_power_of_two(self) -> None:
        with self.assertRaisesRegex(SemanticError, "power of two and at least 2"):
            analyze(
                parse(
                    "module Bad { clock c reset r out tx:vc_credit<u8,3,2> "
                    "tx.payload=0 tx.vc=0 tx.send=0 }"
                )
            )

    def test_arbiter_requires_clock_packet_directions_and_matching_payloads(self) -> None:
        cases = (
            (
                "module Bad { in a:packet<u8> in b:packet<u8> out y:packet<u8> "
                "arbiter [a,b] -> y { policy round_robin grant packet } }",
                "require one module clock",
            ),
            (
                "module Bad { clock c reset r out a:packet<u8> in b:packet<u8> "
                "out y:packet<u8> arbiter [a,b] -> y "
                "{ policy round_robin grant packet } }",
                "source 'a' must be an input",
            ),
            (
                "module Bad { clock c reset r in a:rv<u8> in b:packet<u8> "
                "out y:packet<u8> arbiter [a,b] -> y "
                "{ policy round_robin grant packet } }",
                "source 'a' must use packet",
            ),
            (
                "module Bad { clock c reset r in a:packet<u8> in b:packet<u16> "
                "out y:packet<u8> arbiter [a,b] -> y "
                "{ policy round_robin grant packet } }",
                "payload mismatch",
            ),
        )
        for source, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic), self.assertRaisesRegex(
                SemanticError, diagnostic
            ):
                analyze(parse(source))

    def test_arbiter_sources_are_distinct_and_exclusively_owned(self) -> None:
        with self.assertRaisesRegex(SemanticError, "sources must be distinct"):
            analyze(
                parse(
                    "module Bad { clock c reset r in a:packet<u8> "
                    "out y:packet<u8> arbiter [a,a] -> y "
                    "{ policy fixed_priority grant packet } }"
                )
            )
        with self.assertRaisesRegex(SemanticError, "assigned more than once"):
            analyze(
                parse(
                    "module Bad { clock c reset r in a:packet<u8> "
                    "in b:packet<u8> out y:packet<u8> y.last=1 "
                    "arbiter [a,b] -> y { policy fixed_priority grant packet } }"
                )
            )


if __name__ == "__main__":
    unittest.main()
