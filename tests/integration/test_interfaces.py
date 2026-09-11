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
    simulate,
    simulate_protocol_cycles,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples/rv_passthrough.zhl").read_text()


class InterfaceIntegrationTests(unittest.TestCase):
    def test_all_valid_ready_transfer_combinations(self) -> None:
        module = compile_source(SOURCE).ir
        for valid in (0, 1):
            for ready in (0, 1):
                with self.subTest(valid=valid, ready=ready):
                    result = simulate(
                        module,
                        rx={"payload": 42, "valid": valid},
                        tx={"ready": ready},
                    )
                    transfer = int(bool(valid) and bool(ready))
                    self.assertEqual(
                        result,
                        {
                            "rx": {"ready": ready, "transfer": transfer},
                            "tx": {
                                "payload": 42,
                                "valid": valid,
                                "transfer": transfer,
                            },
                        },
                    )

    def test_backpressure_propagates_and_stable_stall_passes(self) -> None:
        module = compile_source(SOURCE).ir
        results = simulate_protocol_cycles(
            module,
            [
                {"rx": {"payload": 19, "valid": 1}, "tx": {"ready": 0}},
                {"rx": {"payload": 19, "valid": 1}, "tx": {"ready": 0}},
                {"rx": {"payload": 19, "valid": 1}, "tx": {"ready": 1}},
            ],
        )
        self.assertEqual([cycle["rx"]["ready"] for cycle in results], [0, 0, 1])
        self.assertEqual([cycle["tx"]["transfer"] for cycle in results], [0, 0, 1])

    def test_stability_checker_rejects_payload_or_valid_change_under_stall(self) -> None:
        module = compile_source(SOURCE).ir
        bad_second_cycles = (
            {"rx": {"payload": 20, "valid": 1}, "tx": {"ready": 0}},
            {"rx": {"payload": 19, "valid": 0}, "tx": {"ready": 0}},
        )
        for second_cycle in bad_second_cycles:
            with self.subTest(second_cycle=second_cycle), self.assertRaisesRegex(
                ProtocolViolation, "changed payload or valid while stalled"
            ):
                simulate_protocol_cycles(
                    module,
                    [
                        {
                            "rx": {"payload": 19, "valid": 1},
                            "tx": {"ready": 0},
                        },
                        second_cycle,
                    ],
                )

    def test_protocol_input_shapes_are_validated(self) -> None:
        module = compile_source(SOURCE).ir
        with self.assertRaisesRegex(
            SimulationError, "ready/valid input 'rx' requires fields: payload, valid"
        ):
            simulate(module, rx={"payload": 1}, tx={"ready": 1})
        with self.assertRaisesRegex(SimulationError, "rx.payload.*does not fit u8"):
            simulate(module, rx={"payload": 256, "valid": 1}, tx={"ready": 1})



if __name__ == "__main__":
    unittest.main()
