from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]
VERILATOR = shutil.which("verilator")


def _simulate(source: str, top: str, body: str, tmp_path: Path) -> None:
    module = compile_source(
        (ROOT / "examples" / source).read_text(), include_clash=False
    ).ir
    rtl = tmp_path / f"{top}.sv"
    harness = tmp_path / "test.cpp"
    rtl.write_text(emit_experimental(module))
    harness.write_text(f'#include "V{top}.h"\n' + body)
    obj = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    result = subprocess.run(
        (
            VERILATOR or "verilator", "--cc", "--exe", "--build",
            "--top-module", top, "--Mdir", str(obj), str(rtl), str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    run = subprocess.run(
        (str(obj / f"V{top}"),), capture_output=True, text=True
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.skipif(VERILATOR is None, reason="Verilator is required")
def test_rv_to_credit_stops_and_resumes_at_exact_credit_limit(tmp_path: Path) -> None:
    _simulate(
        "rv_to_credit.zhl",
        "RvToCredit",
        r'''
static void tick(VRvToCredit& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
int main() {
  VRvToCredit d; d.rst=1; d.rx_valid=0; d.rx_payload=0; d.tx_return=0;
  tick(d); d.rst=0;
  d.rx_valid=1; d.rx_payload=1; d.eval(); if (!d.rx_ready || !d.tx_send) return 1; tick(d);
  d.rx_payload=2; d.eval(); if (!d.rx_ready || !d.tx_send) return 2; tick(d);
  d.rx_payload=3; d.eval(); if (d.rx_ready || d.tx_send) return 3;
  d.tx_return=1; tick(d); d.tx_return=0; d.eval();
  if (!d.rx_ready || !d.tx_send || d.tx_payload != 3) return 4;
  return 0;
}
''',
        tmp_path,
    )


@pytest.mark.skipif(VERILATOR is None, reason="Verilator is required")
def test_credit_to_rv_buffers_stalls_and_returns_only_on_dequeue(tmp_path: Path) -> None:
    _simulate(
        "credit_to_rv.zhl",
        "CreditToRv",
        r'''
static void tick(VCreditToRv& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
int main() {
  VCreditToRv d; d.rst=1; d.rx_send=0; d.rx_payload=0; d.tx_ready=0;
  tick(d); d.rst=0;
  d.rx_send=1; d.rx_payload=9; tick(d);
  if (!d.tx_valid || d.tx_payload != 9 || d.rx_return) return 1;
  d.rx_payload=10; tick(d);
  if (!d.tx_valid || d.tx_payload != 9 || d.rx_return) return 2;
  d.rx_payload=11; d.tx_ready=1; d.eval(); if (!d.rx_return) return 3; tick(d);
  if (!d.tx_valid || d.tx_payload != 10) return 4;
  d.rx_send=0;
  d.eval(); if (!d.rx_return) return 5; tick(d);
  if (!d.tx_valid || d.tx_payload != 11 || !d.rx_return) return 6; tick(d);
  return d.tx_valid ? 7 : 0;
}
''',
        tmp_path,
    )


@pytest.mark.skipif(VERILATOR is None, reason="Verilator is required")
def test_vc_credit_sender_tracks_each_channel_independently(tmp_path: Path) -> None:
    _simulate(
        "vc_credit_source.zhl",
        "VcCreditSource",
        r'''
static void tick(VVcCreditSource& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
int main() {
  VVcCreditSource d; d.rst=1; d.payload=0; d.channel=0; d.request=0;
  d.tx_return=0; d.tx_return_vc=0; tick(d); if (d.tx_send) return 1;
  d.rst=0; d.request=1; d.payload=10; d.eval();
  if (!d.tx_send || d.tx_vc != 0) return 2;
  tick(d); d.payload=11; tick(d); d.payload=12; d.eval();
  if (d.tx_send) return 3;
  d.channel=1; d.payload=20; d.eval();
  if (!d.tx_send || d.tx_vc != 1) return 4;
  tick(d); d.channel=0; d.tx_return=1; d.tx_return_vc=0; d.eval();
  if (d.tx_send) return 5;
  tick(d); d.tx_return=0; d.eval(); return d.tx_send ? 0 : 6;
}
''',
        tmp_path,
    )
