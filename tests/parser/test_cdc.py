import unittest

from zlang.ast.nodes import CrossingKind
from zlang.parser import parse


class CdcParserTests(unittest.TestCase):
    def test_named_domains_are_preserved_on_resets_ports_and_state(self) -> None:
        module = parse(
            "module Domains { clock a reset ar @ a clock b reset br @ b "
            "in x:bit @ a out y:bit @ b reg held:bit=0 @ a "
            "connect x -> y { crossing sync_level } }"
        )

        self.assertEqual(module.clocks, ("a", "b"))
        self.assertEqual(module.reset_domains, (("ar", "a"), ("br", "b")))
        self.assertEqual([port.domain for port in module.ports], ["a", "b"])
        self.assertEqual(module.registers[0].domain, "a")
        self.assertEqual(
            module.connections[0].crossing.kind,
            CrossingKind.SYNC_LEVEL,
        )

    def test_all_crossing_forms_parse(self) -> None:
        basic = {
            name: parse(
                "module C { in x:bit out y:bit "
                f"connect x -> y {{ crossing {name} }} }}"
            ).connections[0].crossing
            for name in ("sync_level", "pulse_toggle", "handshake")
        }
        self.assertEqual(
            [crossing.kind for crossing in basic.values()],
            [
                CrossingKind.SYNC_LEVEL,
                CrossingKind.PULSE_TOGGLE,
                CrossingKind.HANDSHAKE,
            ],
        )
        crossing = parse(
            "module C { in x:bit out y:bit "
            "connect x -> y { crossing async_fifo(8) } }"
        ).connections[0].crossing
        self.assertEqual(crossing.kind, CrossingKind.ASYNC_FIFO)
        self.assertEqual(crossing.depth, 8)


if __name__ == "__main__":
    unittest.main()
