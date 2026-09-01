from pathlib import Path
import os
import shutil
import subprocess
import unittest

import pytest

from zlang.backend.clash import emit as emit_clash
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.simulate import (
    ProtocolViolation,
    simulate,
    simulate_connection_cycles,
    simulate_protocol_cycles,
)
from zlang.toolchain import (
    find_clash_executable,
    generate_verilog,
    lint_with_verilator,
)


ROOT = Path(__file__).resolve().parents[2]


class ConnectionIntegrationTests(unittest.TestCase):
    def test_direct_ready_valid_connection_preserves_backpressure(self) -> None:
        module = compile_source((ROOT / "examples/rv_connect.zl").read_text()).ir
        results = simulate_protocol_cycles(
            module,
            [
                {"rx": {"payload": 7, "valid": 1}, "tx": {"ready": 0}},
                {"rx": {"payload": 7, "valid": 1}, "tx": {"ready": 1}},
            ],
        )
        self.assertEqual(results[0]["rx"]["ready"], 0)
        self.assertEqual(results[0]["tx"]["transfer"], 0)
        self.assertEqual(results[1]["rx"]["ready"], 1)
        self.assertEqual(results[1]["tx"]["transfer"], 1)

    def test_direct_wire_connection_behavior(self) -> None:
        module = compile_source(
            "module Link { in x:wire<u8> out y:wire<u8> connect x -> y }"
        ).ir
        self.assertEqual(simulate(module, x=42), {"y": 42})

    def test_ready_valid_buffer_preserves_order_and_stall_stability(self) -> None:
        module = compile_source((ROOT / "examples/rv_buffer.zl").read_text()).ir
        results = simulate_connection_cycles(
            module,
            [
                {"rx": {"payload": 0, "valid": 0}, "tx": {"ready": 0}},
                {"rx": {"payload": 11, "valid": 1}, "tx": {"ready": 0}},
                {"rx": {"payload": 22, "valid": 1}, "tx": {"ready": 0}},
                {"rx": {"payload": 22, "valid": 1}, "tx": {"ready": 1}},
                {"rx": {"payload": 22, "valid": 1}, "tx": {"ready": 1}},
            ],
            reset=[True, False, False, False, False],
        )
        self.assertEqual(results[0]["rx"]["ready"], 0)
        self.assertEqual(results[1]["tx"]["valid"], 0)
        self.assertEqual(results[2]["tx"]["payload"], 11)
        self.assertEqual(results[2]["tx"]["valid"], 1)
        self.assertEqual(results[3]["tx"]["payload"], 11)
        self.assertEqual(results[3]["tx"]["transfer"], 1)
        self.assertEqual(results[4]["tx"]["payload"], 22)

    def test_ready_valid_buffer_full_pop_push_preserves_exact_order(self) -> None:
        module = compile_source((ROOT / "examples/rv_buffer.zl").read_text()).ir
        results = simulate_connection_cycles(
            module,
            [
                {"rx": {"payload": 9, "valid": 1}, "tx": {"ready": 0}},
                {"rx": {"payload": 10, "valid": 1}, "tx": {"ready": 0}},
                {"rx": {"payload": 11, "valid": 1}, "tx": {"ready": 1}},
                {"rx": {"payload": 0, "valid": 0}, "tx": {"ready": 1}},
                {"rx": {"payload": 0, "valid": 0}, "tx": {"ready": 1}},
            ],
        )
        self.assertEqual(
            [
                (
                    cycle["rx"]["ready"], cycle["tx"]["valid"],
                    cycle["tx"]["payload"],
                )
                for cycle in results
            ],
            [(1, 0, 0), (1, 1, 9), (1, 1, 9), (1, 1, 10), (1, 1, 11)],
        )

    def test_ready_valid_to_credit_stops_at_zero_credits(self) -> None:
        module = compile_source((ROOT / "examples/rv_to_credit.zl").read_text()).ir
        results = simulate_connection_cycles(
            module,
            [
                {"rx": {"payload": 1, "valid": 1}, "tx": {"return": 0}},
                {"rx": {"payload": 2, "valid": 1}, "tx": {"return": 0}},
                {"rx": {"payload": 3, "valid": 1}, "tx": {"return": 0}},
                {"rx": {"payload": 3, "valid": 1}, "tx": {"return": 1}},
                {"rx": {"payload": 3, "valid": 1}, "tx": {"return": 0}},
            ],
        )
        self.assertEqual([cycle["tx"]["send"] for cycle in results], [1, 1, 0, 0, 1])
        self.assertEqual(results[2]["rx"]["ready"], 0)
        self.assertEqual(results[4]["tx"]["payload"], 3)

    def test_credit_to_ready_valid_returns_credit_only_when_dequeued(self) -> None:
        module = compile_source((ROOT / "examples/credit_to_rv.zl").read_text()).ir
        results = simulate_connection_cycles(
            module,
            [
                {"rx": {"payload": 9, "send": 1}, "tx": {"ready": 0}},
                {"rx": {"payload": 10, "send": 1}, "tx": {"ready": 0}},
                {"rx": {"payload": 0, "send": 0}, "tx": {"ready": 1}},
                {"rx": {"payload": 0, "send": 0}, "tx": {"ready": 1}},
            ],
        )
        self.assertEqual(results[1]["tx"]["payload"], 9)
        self.assertEqual(results[2]["rx"]["return"], 1)
        self.assertEqual(results[3]["tx"]["payload"], 10)
        self.assertEqual(results[3]["tx"]["transfer"], 1)

        with self.assertRaisesRegex(ProtocolViolation, "without credit"):
            simulate_connection_cycles(
                module,
                [
                    {"rx": {"payload": 1, "send": 1}, "tx": {"ready": 0}},
                    {"rx": {"payload": 2, "send": 1}, "tx": {"ready": 0}},
                    {"rx": {"payload": 3, "send": 1}, "tx": {"ready": 0}},
                ],
            )

    def test_credit_to_ready_valid_full_pop_push_preserves_exact_order(self) -> None:
        module = compile_source((ROOT / "examples/credit_to_rv.zl").read_text()).ir
        results = simulate_connection_cycles(
            module,
            [
                {"rx": {"payload": 9, "send": 1}, "tx": {"ready": 0}},
                {"rx": {"payload": 10, "send": 1}, "tx": {"ready": 0}},
                # Full receiver storage accepts 11 because 9 is returned on
                # this same edge. Pop-before-append preserves 10, then 11.
                {"rx": {"payload": 11, "send": 1}, "tx": {"ready": 1}},
                {"rx": {"payload": 0, "send": 0}, "tx": {"ready": 1}},
                {"rx": {"payload": 0, "send": 0}, "tx": {"ready": 1}},
            ],
        )
        self.assertEqual(
            [
                (cycle["tx"]["valid"], cycle["tx"]["payload"],
                 cycle["rx"]["return"])
                for cycle in results
            ],
            [(0, 0, 0), (1, 9, 0), (1, 9, 1), (1, 10, 1), (1, 11, 1)],
        )


if __name__ == "__main__":
    unittest.main()


_RV_BUFFER_HARNESS = r'''
#include "VRvBuffer.h"
static void tick(VRvBuffer& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
int main() {
  VRvBuffer d;
  d.rst=1; d.rx_valid=0; d.rx_payload=0; d.tx_ready=0; tick(d);
  d.rst=0; d.rx_valid=1; d.rx_payload=9; d.eval();
  if (!d.rx_ready || d.tx_valid) return 1;
  tick(d); d.rx_payload=10; d.eval();
  if (!d.rx_ready || !d.tx_valid || d.tx_payload != 9) return 2;
  tick(d); d.rx_payload=11; d.tx_ready=1; d.eval();
  if (!d.rx_ready || !d.tx_valid || d.tx_payload != 9) return 3;
  tick(d); d.rx_valid=0; d.eval();
  if (!d.tx_valid || d.tx_payload != 10) return 4;
  tick(d); d.eval();
  if (!d.tx_valid || d.tx_payload != 11) return 5;
  tick(d); return d.tx_valid ? 6 : 0;
}
'''


def _run_rv_buffer_verilator(
    tmp_path: Path, rtl: tuple[Path, ...], *, suffix: str,
) -> None:
    harness = tmp_path / f"rv_buffer_{suffix}.cpp"
    harness.write_text(_RV_BUFFER_HARNESS)
    obj = tmp_path / f"obj_{suffix}"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    built = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "--top-module", "RvBuffer", "--Mdir", str(obj),
            *(str(path) for path in rtl), str(harness),
        ),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    ran = subprocess.run(
        (str(obj / "VRvBuffer"),), cwd=tmp_path,
        capture_output=True, text=True,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr


@pytest.mark.skipif(
    shutil.which("verilator") is None,
    reason="Verilator is required",
)
@pytest.mark.parametrize("backend", ("systemverilog", "clash"))
def test_ready_valid_buffer_full_pop_push_dual_backend_trace(
    tmp_path: Path, backend: str,
) -> None:
    clash = find_clash_executable()
    if backend == "clash" and clash is None:
        pytest.skip("real Clash is required")
    module = compile_source(
        (ROOT / "examples/rv_buffer.zl").read_text(), include_clash=False,
    ).ir
    if backend == "systemverilog":
        source = tmp_path / "RvBuffer.sv"
        source.write_text(emit_sv_artifact(module).text)
        rtl = (source,)
    else:
        assert clash is not None
        rtl = tuple(generate_verilog(
            emit_clash(module), module.name, tmp_path / "clash", clash,
        ))
    lint_with_verilator(rtl, module.name)
    _run_rv_buffer_verilator(tmp_path, rtl, suffix=backend)
