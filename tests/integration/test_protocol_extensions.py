from pathlib import Path
import unittest

from zlang.compiler import compile_source
from zlang.simulate import ProtocolViolation, simulate_cycles


ROOT = Path(__file__).resolve().parents[2]


def packet(payload: int, valid: int = 1, last: int = 1):
    return {"payload": payload, "valid": valid, "last": last}


def arbiter_cycle(a, b, ready: int = 1):
    return {"source_a": a, "source_b": b, "tx": {"ready": ready}}


def vc_cycle(payload: int, channel: int, request: int, returned=0, return_vc=0):
    return {
        "payload": payload,
        "channel": channel,
        "request": request,
        "tx": {"return": returned, "return_vc": return_vc},
    }


class ProtocolExtensionBehaviorTests(unittest.TestCase):
    def test_packet_grant_is_held_through_backpressure_and_until_last(self) -> None:
        module = compile_source(
            (ROOT / "examples/packet_round_robin.zl").read_text()
        ).ir
        results = simulate_cycles(
            module,
            [
                arbiter_cycle(packet(10, last=0), packet(20), ready=0),
                arbiter_cycle(packet(10, last=0), packet(20), ready=1),
                arbiter_cycle(packet(11, last=1), packet(20), ready=1),
                arbiter_cycle(packet(12), packet(20), ready=1),
            ],
        )

        self.assertEqual(
            [result["tx"]["grant"] for result in results],
            [0, 0, 0, 1],
        )
        self.assertEqual(
            [result["tx"]["payload"] for result in results],
            [10, 10, 11, 20],
        )
        self.assertEqual(results[0]["source_a"]["ready"], 0)
        self.assertEqual(results[3]["source_b"]["transfer"], 1)

    def test_round_robin_is_fair_at_packet_boundaries(self) -> None:
        module = compile_source(
            (ROOT / "examples/packet_round_robin.zl").read_text()
        ).ir
        results = simulate_cycles(
            module,
            [
                arbiter_cycle(packet(1), packet(2)),
                arbiter_cycle(packet(3), packet(2)),
                arbiter_cycle(packet(3), packet(4)),
                arbiter_cycle(packet(5), packet(4)),
            ],
        )
        self.assertEqual(
            [result["tx"]["grant"] for result in results],
            [0, 1, 0, 1],
        )

    def test_fixed_priority_documents_expected_starvation_boundary(self) -> None:
        module = compile_source(
            (ROOT / "examples/packet_fixed_arbiter.zl").read_text()
        ).ir
        cycles = [
            {
                "high_priority": packet(index),
                "low_priority": packet(100),
                "tx": {"ready": 1},
            }
            for index in range(4)
        ]
        results = simulate_cycles(module, cycles)
        self.assertEqual(
            [result["tx"]["payload"] for result in results],
            [0, 1, 2, 3],
        )
        self.assertTrue(
            all(result["low_priority"]["ready"] == 0 for result in results)
        )

    def test_packet_source_must_hold_payload_and_last_while_stalled(self) -> None:
        module = compile_source(
            (ROOT / "examples/packet_round_robin.zl").read_text()
        ).ir
        with self.assertRaisesRegex(ProtocolViolation, "changed while stalled"):
            simulate_cycles(
                module,
                [
                    arbiter_cycle(packet(1, last=0), packet(2), ready=0),
                    arbiter_cycle(packet(9, last=1), packet(2), ready=0),
                ],
            )

    def test_virtual_channel_credits_are_independent_and_bounded(self) -> None:
        module = compile_source(
            (ROOT / "examples/vc_credit_source.zl").read_text()
        ).ir
        results = simulate_cycles(
            module,
            [
                vc_cycle(1, 0, 1),
                vc_cycle(2, 0, 1),
                vc_cycle(3, 0, 1),
                vc_cycle(4, 1, 1),
                vc_cycle(5, 0, 1, returned=1, return_vc=0),
                vc_cycle(6, 0, 1),
            ],
        )

        self.assertEqual(
            [result["tx"]["send"] for result in results],
            [1, 1, 0, 1, 0, 1],
        )
        self.assertEqual(results[2]["tx"]["credits"], (0, 2))
        self.assertEqual(results[4]["tx"]["credits"], (0, 1))
        self.assertEqual(results[5]["tx"]["credits"], (1, 1))

    def test_vc_reset_restores_each_counter_and_over_return_is_rejected(self) -> None:
        module = compile_source(
            (ROOT / "examples/vc_credit_source.zl").read_text()
        ).ir
        reset_results = simulate_cycles(
            module,
            [vc_cycle(1, 0, 1), vc_cycle(2, 1, 1)],
            reset=[False, True],
        )
        self.assertEqual(reset_results[1]["tx"]["credits"], (2, 2))
        self.assertEqual(reset_results[1]["tx"]["send"], 0)

        with self.assertRaisesRegex(ProtocolViolation, "overflow on VC 1"):
            simulate_cycles(
                module,
                [vc_cycle(0, 0, 0, returned=1, return_vc=1)],
            )

    def test_vc_receiver_occupancy_is_checked_per_channel(self) -> None:
        module = compile_source(
            "module Sink { clock c reset r in rx:vc_credit<u8,2,2> "
            "in return_request:bit in return_channel:u1 "
            "rx.return=return_request rx.return_vc=return_channel }"
        ).ir

        def cycle(vc: int, sent: int, returned: int = 0, return_vc: int = 0):
            return {
                "rx": {"payload": 0, "vc": vc, "send": sent},
                "return_request": returned,
                "return_channel": return_vc,
            }

        results = simulate_cycles(
            module,
            [cycle(0, 1), cycle(0, 1), cycle(1, 1), cycle(0, 0, 1, 0)],
        )
        self.assertEqual(results[2]["rx"]["occupancy"], (2, 0))
        self.assertEqual(results[3]["rx"]["occupancy"], (2, 1))

        with self.assertRaisesRegex(ProtocolViolation, "underflow on VC 1"):
            simulate_cycles(module, [cycle(0, 0, 1, 1)])
        with self.assertRaisesRegex(ProtocolViolation, "overflow on VC 0"):
            simulate_cycles(
                module,
                [cycle(0, 1), cycle(0, 1), cycle(0, 1)],
            )


if __name__ == "__main__":
    unittest.main()
