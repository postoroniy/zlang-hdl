from pathlib import Path
import unittest

from zlang.ast.nodes import ArbitrationPolicy, GrantScope, InterfaceKind
from zlang.parser import parse


ROOT = Path(__file__).resolve().parents[2]


class ProtocolExtensionParserTests(unittest.TestCase):
    def test_packet_arbiter_policy_sources_and_scope_parse(self) -> None:
        module = parse((ROOT / "examples/packet_round_robin.zhl").read_text())
        arbiter = module.arbiters[0]

        self.assertEqual(
            [port.type_name.kind for port in module.ports],
            [InterfaceKind.PACKET, InterfaceKind.PACKET, InterfaceKind.PACKET],
        )
        self.assertEqual(arbiter.sources, ("source_a", "source_b"))
        self.assertEqual(arbiter.destination, "tx")
        self.assertEqual(arbiter.policy, ArbitrationPolicy.ROUND_ROBIN)
        self.assertEqual(arbiter.grant_scope, GrantScope.PACKET)

    def test_virtual_channel_count_and_per_channel_credits_parse(self) -> None:
        module = parse((ROOT / "examples/vc_credit_source.zhl").read_text())
        interface = module.ports[-1].type_name

        self.assertEqual(interface.kind, InterfaceKind.VC_CREDIT)
        self.assertEqual(interface.virtual_channels, 2)
        self.assertEqual(interface.capacity, 2)

    def test_beat_scope_is_an_explicit_alternative(self) -> None:
        module = parse(
            "module Beat { clock c reset r in a:packet<u8> in b:packet<u8> "
            "out y:packet<u8> arbiter [a,b] -> y "
            "{ policy fixed_priority grant beat } }"
        )
        self.assertEqual(module.arbiters[0].grant_scope, GrantScope.BEAT)


if __name__ == "__main__":
    unittest.main()
