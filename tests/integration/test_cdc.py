from pathlib import Path
import unittest

from zlang.compiler import compile_source
from zlang.simulate import ProtocolViolation, simulate_cdc_steps


ROOT = Path(__file__).resolve().parents[2]
SOURCE = "source_clock"
DESTINATION = "destination_clock"


def compile_example(name: str):
    return compile_source((ROOT / "examples" / name).read_text()).ir


def rv(payload: int = 0, valid: int = 0, ready: int = 0):
    return {
        "source": {"payload": payload, "valid": valid},
        "destination": {"ready": ready},
    }


class CdcBehaviorTests(unittest.TestCase):
    def test_level_changes_after_two_destination_edges(self) -> None:
        results = simulate_cdc_steps(
            compile_example("cdc_level.zl"),
            [{"level": value} for value in (0, 1, 1, 1, 1)],
            [
                {SOURCE, DESTINATION},
                {SOURCE},
                {DESTINATION},
                {DESTINATION},
                set(),
            ],
            [{SOURCE, DESTINATION}, set(), set(), set(), set()],
        )
        self.assertEqual([item["synced"] for item in results], [0, 0, 0, 0, 1])

    def test_toggle_crossing_emits_one_destination_pulse(self) -> None:
        results = simulate_cdc_steps(
            compile_example("cdc_pulse.zl"),
            [{"pulse": value} for value in (0, 1, 0, 0, 0, 0)],
            [
                {SOURCE, DESTINATION},
                {SOURCE},
                {DESTINATION},
                {DESTINATION},
                {DESTINATION},
                set(),
            ],
            [{SOURCE, DESTINATION}, set(), set(), set(), set(), set()],
        )
        self.assertEqual(
            [item["crossed_pulse"] for item in results],
            [0, 0, 0, 0, 1, 0],
        )

    def test_toggle_rejects_a_second_event_before_the_first_crosses(self) -> None:
        with self.assertRaisesRegex(ProtocolViolation, "before the previous pulse"):
            simulate_cdc_steps(
                compile_example("cdc_pulse.zl"),
                [{"pulse": 1}, {"pulse": 1}],
                [{SOURCE}, {SOURCE}],
            )

    def test_handshake_holds_payload_under_destination_backpressure(self) -> None:
        results = simulate_cdc_steps(
            compile_example("cdc_handshake.zl"),
            [
                rv(),
                rv(42, 1),
                rv(99, 1),
                rv(99, 1),
                rv(99, 1),
                rv(99, 1, 1),
                rv(0, 0, 1),
                rv(0, 0, 1),
                rv(),
            ],
            [
                {SOURCE, DESTINATION},
                {SOURCE},
                {DESTINATION},
                {DESTINATION},
                set(),
                {DESTINATION},
                {SOURCE},
                {SOURCE},
                set(),
            ],
            [{SOURCE, DESTINATION}, *([set()] * 8)],
        )

        self.assertEqual(results[1]["source"], {"ready": 1, "transfer": 1})
        self.assertEqual(results[4]["destination"]["payload"], 42)
        self.assertEqual(results[4]["destination"]["valid"], 1)
        self.assertEqual(results[5]["destination"]["transfer"], 1)
        self.assertEqual(results[8]["source"]["ready"], 1)

    def test_async_fifo_preserves_order_and_delays_returned_capacity(self) -> None:
        results = simulate_cdc_steps(
            compile_example("cdc_async_fifo.zl"),
            [
                rv(),
                rv(1, 1),
                rv(2, 1),
                rv(3, 1),
                rv(4, 1),
                rv(),
                rv(),
                rv(0, 0, 1),
                rv(0, 0, 1),
                rv(),
                rv(),
                rv(),
            ],
            [
                {SOURCE, DESTINATION},
                {SOURCE},
                {SOURCE},
                {SOURCE},
                {SOURCE},
                {DESTINATION},
                {DESTINATION},
                {DESTINATION},
                {DESTINATION},
                {SOURCE},
                {SOURCE},
                set(),
            ],
            [{SOURCE, DESTINATION}, *([set()] * 11)],
        )

        self.assertEqual(results[5]["source"]["ready"], 0)
        self.assertEqual(
            [results[index]["destination"]["payload"] for index in (7, 8)],
            [1, 2],
        )
        self.assertEqual(
            [results[index]["destination"]["transfer"] for index in (7, 8)],
            [1, 1],
        )
        self.assertEqual(results[9]["source"]["ready"], 0)
        self.assertEqual(results[10]["source"]["ready"], 0)
        self.assertEqual(results[11]["source"]["ready"], 1)

    def test_endpoint_resets_must_be_coordinated(self) -> None:
        with self.assertRaisesRegex(ProtocolViolation, "asserted together"):
            simulate_cdc_steps(
                compile_example("cdc_level.zl"),
                [{"level": 0}],
                [{SOURCE}],
                [{SOURCE}],
            )


if __name__ == "__main__":
    unittest.main()
