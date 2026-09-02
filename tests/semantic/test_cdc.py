from pathlib import Path
import unittest

from zlang.ir.cdc import CrossingKind
from zlang.compiler import compile_source
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[2]


def analyze_body(body: str):
    return analyze(
        parse(
            "module Domains { "
            "clock source_clock reset source_reset @ source_clock "
            "clock destination_clock reset destination_reset @ destination_clock "
            f"{body} }}"
        )
    )


class CdcSemanticTests(unittest.TestCase):
    def test_domains_and_crossings_are_explicit_in_typed_ir(self) -> None:
        module = analyze(parse((ROOT / "examples/cdc_async_fifo.zhl").read_text()))

        self.assertTrue(module.is_multi_clock)
        self.assertIsNone(module.clock)
        self.assertIsNone(module.reset)
        self.assertEqual(
            [(domain.clock, domain.reset) for domain in module.clock_domains],
            [
                ("source_clock", "source_reset"),
                ("destination_clock", "destination_reset"),
            ],
        )
        self.assertEqual(module.ports[0].domain, "source_clock")
        self.assertEqual(module.ports[1].domain, "destination_clock")
        self.assertEqual(
            module.connections[0].crossing.kind,
            CrossingKind.ASYNC_FIFO,
        )
        self.assertEqual(module.connections[0].crossing.depth, 4)

    def test_register_domain_is_typed_and_local_next_state_is_allowed(self) -> None:
        module = analyze_body(
            "in x:bit @ source_clock out y:bit @ source_clock "
            "reg held:bit=0 @ source_clock y=held held<-x"
        )
        self.assertEqual(module.registers[0].domain, "source_clock")

    def test_each_multi_clock_reset_and_port_requires_a_domain(self) -> None:
        with self.assertRaisesRegex(
            SemanticError, "reset 'source_reset' requires an explicit clock domain"
        ):
            analyze(
                parse(
                    "module Bad { clock source_clock reset source_reset "
                    "clock destination_clock reset destination_reset @ destination_clock }"
                )
            )
        with self.assertRaisesRegex(
            SemanticError, "port 'x' requires an explicit clock domain"
        ):
            analyze_body("in x:bit out y:bit @ destination_clock y=0")

    def test_implicit_connection_and_assignment_crossings_are_rejected(self) -> None:
        with self.assertRaisesRegex(SemanticError, "implicit clock-domain crossing"):
            analyze_body(
                "in x:bit @ source_clock out y:bit @ destination_clock "
                "connect x -> y"
            )
        with self.assertRaisesRegex(
            SemanticError, "implicit clock-domain crossing in assignment"
        ):
            analyze_body(
                "in x:bit @ source_clock out y:bit @ destination_clock y=x"
            )

    def test_two_flop_crossings_accept_only_single_bit_wires(self) -> None:
        with self.assertRaisesRegex(
            SemanticError, "sync_level crossing requires bit wire endpoints"
        ):
            analyze_body(
                "in x:u8 @ source_clock out y:u8 @ destination_clock "
                "connect x -> y { crossing sync_level }"
            )
        with self.assertRaisesRegex(
            SemanticError, "pulse_toggle crossing requires bit wire endpoints"
        ):
            analyze_body(
                "in x:rv<bit> @ source_clock out y:rv<bit> @ destination_clock "
                "connect x -> y { crossing pulse_toggle }"
            )

    def test_handshake_and_async_fifo_require_ready_valid_endpoints(self) -> None:
        with self.assertRaisesRegex(
            SemanticError, "handshake crossing requires ready/valid endpoints"
        ):
            analyze_body(
                "in x:u8 @ source_clock out y:u8 @ destination_clock "
                "connect x -> y { crossing handshake }"
            )
        with self.assertRaisesRegex(
            SemanticError, "async_fifo crossing requires ready/valid endpoints"
        ):
            analyze_body(
                "in x:u8 @ source_clock out y:u8 @ destination_clock "
                "connect x -> y { crossing async_fifo(4) }"
            )

    def test_async_fifo_depth_is_a_power_of_two_and_at_least_four(self) -> None:
        for depth in (2, 6):
            with self.subTest(depth=depth), self.assertRaisesRegex(
                SemanticError, "power of two and at least 4"
            ):
                analyze_body(
                    "in x:rv<u8> @ source_clock "
                    "out y:rv<u8> @ destination_clock "
                    f"connect x -> y {{ crossing async_fifo({depth}) }}"
                )

    def test_explicit_crossing_requires_different_domains(self) -> None:
        with self.assertRaisesRegex(SemanticError, "requires different domains"):
            analyze_body(
                "in x:bit @ source_clock out y:bit @ source_clock "
                "connect x -> y { crossing sync_level }"
            )

    def test_single_ready_valid_aggregate_accepts_only_async_fifo(self) -> None:
        accepted = compile_source(
            "import std.bus.axi_stream "
            "module AggregateCdc { "
            "clock a reset ar @a clock b reset br @b "
            "i:AXIStream<32>.sink @a o:AXIStream<32>.source @b "
            "i -> o { crossing async_fifo(4) } }",
            include_clash=False,
        ).ir
        self.assertEqual(len(accepted.connections), 1)
        self.assertEqual(
            accepted.connections[0].crossing.kind,
            CrossingKind.ASYNC_FIFO,
        )
        self.assertEqual(accepted.connections[0].source.name, "i__t")
        self.assertEqual(accepted.connections[0].destination.name, "o__t")

        with self.assertRaisesRegex(
            SemanticError,
            "aggregate protocol crossings currently support only async_fifo",
        ):
            compile_source(
                "import std.bus.axi_stream "
                "module BadAggregateCdc { "
                "clock a reset ar @a clock b reset br @b "
                "i:AXIStream<32>.sink @a o:AXIStream<32>.source @b "
                "i -> o { crossing handshake } }",
                include_clash=False,
            )


if __name__ == "__main__":
    unittest.main()
