from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source
from zlang.simulate import simulate_cycles


ROOT = Path(__file__).resolve().parents[2]


class RuleIntegrationTests(unittest.TestCase):
    def test_priority_firing_and_reset_are_cycle_accurate(self) -> None:
        module = compile_source((ROOT / "examples/rule_counter.zhl").read_text()).ir
        results = simulate_cycles(
            module,
            [
                {"increment": 1, "clear": 0},
                {"increment": 1, "clear": 0},
                {"increment": 1, "clear": 1},
                {"increment": 1, "clear": 0},
                {"increment": 1, "clear": 0},
            ],
            reset=[False, False, False, False, True],
        )
        self.assertEqual([item["count_out"] for item in results], [0, 1, 2, 0, 0])

    def test_lower_rule_is_skipped_atomically_on_any_conflict(self) -> None:
        source = (
            "module Atomic { clock c reset r in go:bit out y:u8 "
            "reg a:u8=0 reg b:u8=0 "
            "rule high when go { a <- 1 } "
            "rule low when go { a <- 2 b <- 3 } priority high > low y=b }"
        )
        module = compile_source(source).ir
        results = simulate_cycles(module, [{"go": 1}, {"go": 0}])
        self.assertEqual(results[1]["y"], 0)

    def test_state_and_output_action_fire_atomically(self) -> None:
        module = compile_source((ROOT / "examples/rule_action.zhl").read_text()).ir
        results = simulate_cycles(
            module,
            [{"enable": 0}, {"enable": 1}, {"enable": 0}],
        )
        self.assertEqual([item["fired"] for item in results], [0, 1, 0])

    @unittest.skipUnless(shutil.which("verilator"), "Verilator unavailable")
    def test_concise_priority_counter_direct_sv_simulates(self) -> None:
        module = compile_source((ROOT / "examples/rule_counter.zhl").read_text()).ir
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rtl = root / "RuleCounter.sv"
            rtl.write_text(emit_experimental(module))
            harness = root / "rules_test.cpp"
            harness.write_text(
                '#include "VRuleCounter.h"\n'
                "static void tick(VRuleCounter& d) { d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval(); }\n"
                "int main() { VRuleCounter d; d.increment=0; d.clear=0; d.rst=1; tick(d); d.rst=0; "
                "d.increment=1; tick(d); if (d.count_out != 1) return 1; "
                "d.clear=1; tick(d); if (d.count_out != 0) return 2; return 0; }\n"
            )
            obj = root / "obj"
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            subprocess.run(
                (
                    "verilator", "--cc", "--exe", "--build", "--top-module",
                    "RuleCounter", "--Mdir", str(obj), "-o", "rules_sim",
                    str(rtl), str(harness),
                ),
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            subprocess.run((str(obj / "rules_sim"),), check=True)


if __name__ == "__main__":
    unittest.main()
