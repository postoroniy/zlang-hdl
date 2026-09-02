from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_artifact, emit_experimental
from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]
VERILATOR = shutil.which("verilator")


def _simulate(example: str, top: str, body: str, tmp_path: Path) -> None:
    module = compile_source(
        (ROOT / "examples" / example).read_text(), include_clash=False
    ).ir
    rtl = tmp_path / f"{top}.sv"
    harness = tmp_path / "test.cpp"
    rtl.write_text(emit_experimental(module))
    harness.write_text(f'#include "V{top}.h"\n' + body)
    obj = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    build = subprocess.run(
        (
            VERILATOR or "verilator",
            "--cc",
            "--exe",
            "--build",
            "--top-module",
            top,
            "--Mdir",
            str(obj),
            str(rtl),
            str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run(
        (str(obj / f"V{top}"),), capture_output=True, text=True
    )
    assert run.returncode == 0, run.stderr or run.stdout


def test_handshake_manifest_preserves_typed_physical_directions() -> None:
    module = compile_source(
        (ROOT / "examples" / "cdc_handshake.zhl").read_text(),
        include_clash=False,
    ).ir
    artifact = emit_artifact(module)
    restored = type(artifact).from_json(artifact.to_json())
    bindings = {item.semantic_signal_id: item for item in restored.bindings}
    assert bindings["port:source.payload"].width == 8
    assert bindings["port:source.payload"].role.value == "input"
    assert bindings["port:source.ready"].role.value == "output"
    assert bindings["port:destination.payload"].width == 8
    assert bindings["port:destination.payload"].role.value == "output"
    assert bindings["port:destination.ready"].role.value == "input"
    assert all(
        bindings[name].physical_available
        for name in (
            "port:source.payload",
            "port:source.valid",
            "port:source.ready",
            "port:destination.payload",
            "port:destination.valid",
            "port:destination.ready",
        )
    )


@pytest.mark.skipif(VERILATOR is None, reason="Verilator is required")
def test_sync_level_uses_two_destination_stages(tmp_path: Path) -> None:
    _simulate(
        "cdc_level.zhl",
        "CdcLevel",
        r'''
static void sourceTick(VCdcLevel& d) {
  d.source_clock=0; d.eval(); d.source_clock=1; d.eval(); d.source_clock=0; d.eval();
}
static void destinationTick(VCdcLevel& d) {
  d.destination_clock=0; d.eval(); d.destination_clock=1; d.eval();
  d.destination_clock=0; d.eval();
}
int main() {
  VCdcLevel d; d.source_clock=0; d.destination_clock=0; d.level=0;
  d.source_reset=1; d.destination_reset=1; sourceTick(d); destinationTick(d);
  if (d.synced) return 1;
  d.source_reset=0; d.destination_reset=0; d.level=1;
  destinationTick(d); if (d.synced) return 2;
  destinationTick(d); if (!d.synced) return 3;
  d.destination_reset=1; destinationTick(d); return d.synced ? 4 : 0;
}
''',
        tmp_path,
    )


@pytest.mark.skipif(VERILATOR is None, reason="Verilator is required")
def test_pulse_toggle_emits_one_destination_cycle(tmp_path: Path) -> None:
    _simulate(
        "cdc_pulse.zhl",
        "CdcPulse",
        r'''
static void sourceTick(VCdcPulse& d) {
  d.source_clock=0; d.eval(); d.source_clock=1; d.eval(); d.source_clock=0; d.eval();
}
static void destinationTick(VCdcPulse& d) {
  d.destination_clock=0; d.eval(); d.destination_clock=1; d.eval();
  d.destination_clock=0; d.eval();
}
int main() {
  VCdcPulse d; d.source_clock=0; d.destination_clock=0; d.pulse=0;
  d.source_reset=1; d.destination_reset=1; sourceTick(d); destinationTick(d);
  d.source_reset=0; d.destination_reset=0; d.pulse=1; sourceTick(d); d.pulse=0;
  destinationTick(d); if (d.crossed_pulse) return 1;
  destinationTick(d); if (!d.crossed_pulse) return 2;
  destinationTick(d); if (d.crossed_pulse) return 3;
  return 0;
}
''',
        tmp_path,
    )


@pytest.mark.skipif(VERILATOR is None, reason="Verilator is required")
def test_handshake_holds_payload_until_destination_acceptance(tmp_path: Path) -> None:
    _simulate(
        "cdc_handshake.zhl",
        "CdcHandshake",
        r'''
static void sourceTick(VCdcHandshake& d) {
  d.source_clock=0; d.eval(); d.source_clock=1; d.eval(); d.source_clock=0; d.eval();
}
static void destinationTick(VCdcHandshake& d) {
  d.destination_clock=0; d.eval(); d.destination_clock=1; d.eval();
  d.destination_clock=0; d.eval();
}
int main() {
  VCdcHandshake d; d.source_clock=0; d.destination_clock=0;
  d.source_payload=0; d.source_valid=0; d.destination_ready=0;
  d.source_reset=1; d.destination_reset=1; sourceTick(d); destinationTick(d);
  d.source_reset=0; d.destination_reset=0; d.eval();
  if (!d.source_ready) return 1;
  d.source_payload=42; d.source_valid=1; sourceTick(d);
  d.source_valid=0; d.source_payload=99; d.eval(); if (d.source_ready) return 2;
  destinationTick(d); if (d.destination_valid) return 3;
  destinationTick(d);
  if (!d.destination_valid || d.destination_payload != 42) return 4;
  destinationTick(d);
  if (!d.destination_valid || d.destination_payload != 42) return 5;
  d.destination_ready=1; destinationTick(d); d.destination_ready=0; d.eval();
  if (d.destination_valid) return 6;
  sourceTick(d); if (d.source_ready) return 7;
  sourceTick(d); return d.source_ready ? 0 : 8;
}
''',
        tmp_path,
    )
