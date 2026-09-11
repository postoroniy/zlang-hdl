"""Scheduled storage plus ordinary state inside compile-time child arrays."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.simulate import simulate_cycles
from zlang.toolchain import lint_with_verilator


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
    return compile_source(SOURCE, top="ScheduledLaneArray").ir




@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_scheduled_storage_array_is_lint_clean(tmp_path: Path) -> None:
    rtl = tmp_path / "ScheduledLaneArray.sv"
    rtl.write_text(emit_sv_artifact(_module()).text)
    lint_with_verilator((rtl,), "ScheduledLaneArray")
