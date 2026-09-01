from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.toolchain import CLASH_ENVIRONMENT, CLASH_EXECUTABLE
from zlang.compiler import compile_source
from zlang.simulate import SimulationError, simulate, simulate_cycles


ROOT = Path(__file__).resolve().parents[2]


class SequentialIntegrationTests(unittest.TestCase):
    def test_counter_cycle_and_reset_behavior(self) -> None:
        module = compile_source((ROOT / "examples/counter.zl").read_text()).ir
        outputs = simulate_cycles(
            module,
            [{}, {}, {}, {}, {}, {}],
            reset=[True, False, False, False, True, False],
        )
        self.assertEqual([item["y"] for item in outputs], [0, 0, 1, 2, 0, 0])

    def test_delay_has_exact_two_cycle_latency(self) -> None:
        module = compile_source((ROOT / "examples/delayed_mul.zl").read_text()).ir
        outputs = simulate_cycles(
            module,
            [
                {"a": 2, "b": 3},
                {"a": 4, "b": 5},
                {"a": 6, "b": 7},
                {"a": 8, "b": 9},
            ],
        )
        self.assertEqual([item["y"] for item in outputs], [0, 0, 6, 20])

    def test_state_updates_are_simultaneous(self) -> None:
        source = """
            module Swap {
                clock c reset r
                reg a:u8=1 reg b:u8=2
                out y:u8 y=a
                a <- b
                b <- a
            }
        """
        module = compile_source(source).ir
        outputs = simulate_cycles(module, [{}, {}, {}, {}])
        self.assertEqual([item["y"] for item in outputs], [1, 2, 1, 2])

    def test_combinational_simulator_rejects_sequential_module(self) -> None:
        module = compile_source((ROOT / "examples/counter.zl").read_text()).ir
        with self.assertRaisesRegex(SimulationError, "use simulate_cycles"):
            simulate(module)

    @unittest.skipUnless(CLASH_EXECUTABLE, "Clash executable is not available")
    def test_milestone_four_examples_compile_to_verilog(self) -> None:
        for source_name in ("Counter.hs", "DelayedMul.hs"):
            with self.subTest(source=source_name), tempfile.TemporaryDirectory() as temporary:
                subprocess.run(
                    [
                        CLASH_EXECUTABLE,
                        "--verilog",
                        str(ROOT / "examples/generated" / source_name),
                        "-outputdir",
                        temporary,
                    ],
                    check=True,
                    cwd=ROOT,
                    env=CLASH_ENVIRONMENT,
                )


if __name__ == "__main__":
    unittest.main()
