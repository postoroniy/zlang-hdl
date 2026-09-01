"""Scheduled storage plus ordinary state inside compile-time child arrays."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from zlang.backend.clash import emit as emit_clash
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.simulate import simulate_cycles
from zlang.toolchain import find_clash_executable, generate_verilog, lint_with_verilator


SOURCE = r"""
module ScheduledLane {
    clock clk reset rst
    in data:u8 in push:bit in pop:bit
    out front:u8 out seen:u8
    fifo q:fifo<u8,2>
    reg last:u8=0
    rule enqueue when push { q.push(data) last <- data }
    rule dequeue when pop { q.pop() }
    front=q.front
    seen=last
}
module ScheduledLaneArray {
    clock clk reset rst
    in data:vec<2,u8> in push:vec<2,bit> in pop:vec<2,bit>
    out front:vec<2,u8> out seen:vec<2,u8>
    inst lane[2]:ScheduledLane
    generate(i in 0..2) {
        lane[i].data=data[i]
        lane[i].push=push[i]
        lane[i].pop=pop[i]
    }
    front=generate(i in 0..2) lane[i].front
    seen=generate(i in 0..2) lane[i].seen
}
"""


def _module():
    return compile_source(SOURCE, top="ScheduledLaneArray", include_clash=False).ir


def test_scheduled_storage_array_has_independent_state_reset_and_artifacts() -> None:
    module = _module()
    trace = simulate_cycles(
        module,
        (
            {"data":(0,0), "push":(0,0), "pop":(0,0)},
            {"data":(0x11,0x22), "push":(1,1), "pop":(0,0)},
            {"data":(0x33,0), "push":(1,0), "pop":(0,1)},
            {"data":(0,0x44), "push":(0,1), "pop":(1,0)},
            {"data":(0,0), "push":(0,0), "pop":(0,0)},
        ),
        reset=(True,False,False,False,True),
    )
    assert trace[2]["front"] == [0x11,0x22]
    assert trace[2]["seen"] == [0x11,0x22]
    assert trace[3]["front"] == [0x11,0]
    assert trace[3]["seen"] == [0x33,0x22]
    assert trace[4] == {"front":[0,0], "seen":[0,0]}

    first = emit_sv_artifact(module)
    second = emit_sv_artifact(module)
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    assert BackendArtifact.from_json(first.to_json()).to_json() == first.to_json()
    assert first.text.count("module ScheduledLane__") == 1
    assert first.text.count("ScheduledLane__") == 3
    clash = emit_clash(module)
    assert clash == emit_clash(module)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_scheduled_storage_array_is_lint_clean(tmp_path: Path) -> None:
    rtl = tmp_path / "ScheduledLaneArray.sv"
    rtl.write_text(emit_sv_artifact(_module()).text)
    lint_with_verilator((rtl,), "ScheduledLaneArray")


@pytest.mark.skipif(
    shutil.which("verilator") is None or find_clash_executable() is None,
    reason="Clash or Verilator unavailable",
)
def test_real_clash_scheduled_storage_array_is_lint_clean(tmp_path: Path) -> None:
    module = _module()
    files = generate_verilog(
        emit_clash(module),
        module.name,
        tmp_path / "clash",
        find_clash_executable(),
    )
    lint_with_verilator(files, module.name)
