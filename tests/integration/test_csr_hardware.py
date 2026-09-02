from pathlib import Path
import unittest

from zlang.compiler import compile_source
from zlang.simulate import simulate_csr_cycles


ROOT = Path(__file__).resolve().parents[2]
STATUS = 0x50000004
CONTROL = 0x50000000


def cycle(*, addr=0, write=0, wdata=0, read=0, busy=0, error=0):
    return {
        "addr": addr,
        "write": write,
        "wdata": wdata,
        "read": read,
        "engine_busy": busy,
        "engine_error": error,
    }


class HardwareCsrIntegrationTests(unittest.TestCase):
    def test_hardware_priority_is_exhaustive_for_set_and_clear(self) -> None:
        module = compile_source((ROOT / "examples/engine_csr.zhl").read_text()).ir
        for old in (0, 1):
            for hardware_set in (0, 1):
                for software_clear in (0, 1):
                    results = simulate_csr_cycles(
                        module,
                        [
                            cycle(error=old),
                            cycle(
                                addr=STATUS,
                                write=software_clear,
                                wdata=2 if software_clear else 0,
                                error=hardware_set,
                            ),
                            cycle(),
                        ],
                    )
                    expected = (old & ~software_clear) | hardware_set
                    self.assertEqual(
                        results[2]["state"]["engine.STATUS.error"],
                        expected,
                        (old, hardware_set, software_clear),
                    )

    def test_status_pulse_readback_and_reset_are_cycle_accurate(self) -> None:
        module = compile_source((ROOT / "examples/engine_csr.zhl").read_text()).ir
        results = simulate_csr_cycles(
            module,
            [
                cycle(addr=STATUS, read=1, busy=1),
                cycle(addr=CONTROL, write=1, wdata=1),
                cycle(addr=CONTROL, read=1),
                cycle(),
                cycle(error=1),
                cycle(addr=STATUS, read=1),
                cycle(addr=STATUS, read=1),
            ],
            reset=[False, False, False, False, False, False, True],
        )
        self.assertEqual(results[0]["rdata"], 1)
        self.assertEqual(results[2]["engine_start"], 1)
        self.assertEqual(results[2]["rdata"], 0)
        self.assertEqual(results[3]["engine_start"], 0)
        self.assertEqual(results[5]["rdata"], 2)
        self.assertEqual(results[6]["rdata"], 0)

    def test_explicit_software_priority_allows_clear_to_win(self) -> None:
        source = (
            "module SoftwareWins { clock clk reset rst in event:bit "
            "csr x @0 { R @0 { error bit w1c <- sticky(event) "
            "priority software } } }"
        )
        module = compile_source(source).ir
        results = simulate_csr_cycles(
            module,
            [
                {"addr": 0, "write": 0, "wdata": 0, "read": 0, "event": 1},
                {"addr": 0, "write": 1, "wdata": 1, "read": 0, "event": 1},
                {"addr": 0, "write": 0, "wdata": 0, "read": 1, "event": 0},
            ],
        )
        self.assertEqual(results[2]["rdata"], 0)


if __name__ == "__main__":
    unittest.main()
