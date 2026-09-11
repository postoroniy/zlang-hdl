from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from zlang.compiler import compile_source
from zlang.simulate import (
    ProtocolViolation,
    SimulationError,
    simulate_credit_cycles,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples/credit_source.zhl").read_text()


def source_cycle(
    payload: int,
    request: int,
    returned: int,
) -> dict[str, object]:
    return {
        "payload_data": payload,
        "request": request,
        "tx": {"return": returned},
    }


class CreditIntegrationTests(unittest.TestCase):
    def test_normal_zero_credit_and_return_behavior(self) -> None:
        module = compile_source(SOURCE).ir
        results = simulate_credit_cycles(
            module,
            [
                source_cycle(10, 1, 0),
                source_cycle(11, 1, 0),
                source_cycle(12, 1, 0),
                source_cycle(13, 1, 1),
                source_cycle(14, 1, 0),
            ],
        )
        self.assertEqual(
            [cycle["tx"]["credits"] for cycle in results],
            [2, 1, 0, 0, 1],
        )
        self.assertEqual(
            [cycle["tx"]["send"] for cycle in results],
            [1, 1, 0, 0, 1],
        )
        self.assertEqual(results[2]["tx"]["payload"], 12)

    def test_simultaneous_send_and_return_is_neutral_with_available_credit(self) -> None:
        module = compile_source(SOURCE).ir
        results = simulate_credit_cycles(
            module,
            [source_cycle(1, 1, 1), source_cycle(2, 1, 0)],
        )
        self.assertEqual(
            [(cycle["tx"]["credits"], cycle["tx"]["send"]) for cycle in results],
            [(2, 1), (2, 1)],
        )

    def test_reset_restores_maximum_credits_and_suppresses_transfer(self) -> None:
        module = compile_source(SOURCE).ir
        results = simulate_credit_cycles(
            module,
            [
                source_cycle(1, 1, 0),
                source_cycle(2, 1, 0),
                source_cycle(3, 1, 0),
                source_cycle(4, 1, 0),
            ],
            reset=[False, False, True, False],
        )
        self.assertEqual(
            [(cycle["tx"]["credits"], cycle["tx"]["send"]) for cycle in results],
            [(2, 1), (1, 1), (2, 0), (2, 1)],
        )

    def test_return_at_maximum_credits_is_an_overflow(self) -> None:
        module = compile_source(SOURCE).ir
        with self.assertRaisesRegex(ProtocolViolation, "return at maximum credits"):
            simulate_credit_cycles(module, [source_cycle(1, 0, 1)])

    def test_receiver_underflow_overflow_and_simultaneous_boundary(self) -> None:
        module = compile_source(
            "module CreditSink { clock clk reset rst in release:bit "
            "in rx:credit<u8,2> rx.return=release }"
        ).ir
        with self.assertRaisesRegex(ProtocolViolation, "underflow"):
            simulate_credit_cycles(
                module,
                [{"release": 1, "rx": {"payload": 0, "send": 0}}],
            )
        with self.assertRaisesRegex(ProtocolViolation, "maximum occupancy"):
            simulate_credit_cycles(
                module,
                [
                    {"release": 0, "rx": {"payload": 1, "send": 1}},
                    {"release": 0, "rx": {"payload": 2, "send": 1}},
                    {"release": 0, "rx": {"payload": 3, "send": 1}},
                ],
            )
        result = simulate_credit_cycles(
            module,
            [{"release": 1, "rx": {"payload": 7, "send": 1}}],
        )
        self.assertEqual(result[0]["rx"], {"return": 1, "transfer": 1})

    def test_credit_input_shape_is_validated(self) -> None:
        module = compile_source(SOURCE).ir
        with self.assertRaisesRegex(
            SimulationError, "credit input 'tx' requires fields: return"
        ):
            simulate_credit_cycles(
                module,
                [{"payload_data": 1, "request": 1, "tx": {"send": 0}}],
            )




if __name__ == "__main__":
    unittest.main()
