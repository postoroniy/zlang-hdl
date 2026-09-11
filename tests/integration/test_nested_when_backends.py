"""Executable backend parity for nested atomic ``when`` action selection."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.toolchain import lint_with_verilator


TOP = "NestedWhenBackendSuite"
VERILATOR = shutil.which("verilator")


SOURCE = r"""
module FirstFaultNested {
    clock clk reset rst
    in fault, clear_valid, clear_overflow : bit
    in code : u4
    out valid, overflow : bit
    out sticky : u4

    reg valid_state : bit = 0
    reg overflow_state : bit = 0
    reg sticky_state : u4 = 0

    when fault {
        when valid_state {
            overflow_state <- 1
        } else {
            valid_state <- 1
            overflow_state <- 0
            sticky_state <- code
        }
    } else when clear_valid {
        valid_state <- 0
        overflow_state <- 0
    } else when clear_overflow {
        overflow_state <- 0
    }

    valid = valid_state
    overflow = overflow_state
    sticky = sticky_state
}

module NestedCounterChild {
    clock clk reset rst
    in enable, choose : bit
    in data : u8
    out value : u8

    reg state : u8 = 0
    when enable {
        when choose {
            state <- data
        } else {
            state <- truncate<8>(state + 1)
        }
    }
    value = state
}

module NestedResourceChild {
    clock clk reset rst
    in fifo_go, fifo_push : bit
    in memory_go, memory_write : bit
    in high, low : bit
    in address : u2
    in data : u8

    out fifo_count : u1
    out fifo_front : u8
    out fifo_attempts : u8
    out memory_data : u8
    out event_flag, high_seen, low_seen : bit

    fifo queue : fifo<u8,1>
    memory table : mem<u8,4> {
        read_latency 1
        collision write_first
    }
    reg attempts_state : u8 = 0
    reg high_state : bit = 0
    reg low_state : bit = 0

    fifo_action: when fifo_go {
        attempts_state <- truncate<8>(attempts_state + 1)
        when fifo_push {
            queue.push(data)
        } else {
            queue.pop()
        }
    }

    memory_action: when memory_go {
        when memory_write {
            table.write(address, data)
        } else {
            table.read(address)
        }
    }

    priority {
        higher: when high {
            event_flag <- 1
            high_state <- 1
        }
        lower: when low {
            event_flag <- 0
            low_state <- 1
        }
    }

    fifo_count = queue.count
    fifo_front = queue.front
    fifo_attempts = attempts_state
    memory_data = table.read_data
    high_seen = high_state
    low_seen = low_state
}

module NestedWhenBackendSuite {
    clock clk reset rst
    in fault, clear_valid, clear_overflow : bit
    in code : u4
    in counter_enable, counter_choose : bit
    in counter_data : u8
    in fifo_go, fifo_push : bit
    in memory_go, memory_write : bit
    in high, low : bit
    in address : u2
    in data : u8

    out valid, overflow : bit
    out sticky : u4
    out counter_observed : u8
    out fifo_count : u1
    out fifo_front : u8
    out fifo_attempts : u8
    out memory_data : u8
    out event_flag, high_seen, low_seen : bit

    first : FirstFaultNested {
        fault
        clear_valid
        clear_overflow
        code
    }
    counter : NestedCounterChild {
        enable = counter_enable
        choose = counter_choose
        data = counter_data
    }
    resources : NestedResourceChild {
        fifo_go
        fifo_push
        memory_go
        memory_write
        high
        low
        address
        data
    }

    valid = first.valid
    overflow = first.overflow
    sticky = first.sticky
    counter_observed = counter.value
    fifo_count = resources.fifo_count
    fifo_front = resources.fifo_front
    fifo_attempts = resources.fifo_attempts
    memory_data = resources.memory_data
    event_flag = resources.event_flag
    high_seen = resources.high_seen
    low_seen = resources.low_seen
}
"""


HARNESS = r"""
#include "VNestedWhenBackendSuite.h"
#include "verilated.h"

static void tick(VNestedWhenBackendSuite& d) {
    d.clk = 0; d.eval();
    d.clk = 1; d.eval();
    d.clk = 0; d.eval();
}

static void idle(VNestedWhenBackendSuite& d) {
    d.fault = 0; d.clear_valid = 0; d.clear_overflow = 0; d.code = 0;
    d.counter_enable = 0; d.counter_choose = 0; d.counter_data = 0;
    d.fifo_go = 0; d.fifo_push = 0;
    d.memory_go = 0; d.memory_write = 0;
    d.high = 0; d.low = 0; d.address = 0; d.data = 0;
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    VNestedWhenBackendSuite d;
    idle(d); d.rst = 1; tick(d); d.rst = 0;
    if (d.valid || d.overflow || d.sticky || d.counter_observed ||
        d.fifo_count || d.fifo_attempts || d.memory_data ||
        d.high_seen || d.low_seen) return 1;

    d.fault = 1; d.code = 10; tick(d);
    if (!d.valid || d.overflow || d.sticky != 10) return 2;
    d.code = 11; tick(d);
    if (!d.valid || !d.overflow || d.sticky != 10) return 3;
    d.fault = 0; d.clear_overflow = 1; tick(d);
    if (d.overflow) return 4;
    d.clear_overflow = 0; d.clear_valid = 1; tick(d);
    if (d.valid || d.overflow || d.sticky != 10) return 5;
    d.clear_valid = 0;

    d.counter_enable = 1; d.counter_choose = 1; d.counter_data = 5; tick(d);
    if (d.counter_observed != 5) return 6;
    d.counter_choose = 0; tick(d);
    if (d.counter_observed != 6) return 7;
    d.counter_enable = 0;

    d.fifo_go = 1; d.fifo_push = 1; d.data = 7; tick(d);
    if (d.fifo_count != 1 || d.fifo_front != 7 || d.fifo_attempts != 1)
        return 8;
    d.data = 9; tick(d);
    if (d.fifo_count != 1 || d.fifo_front != 7 || d.fifo_attempts != 1)
        return 9;
    d.fifo_push = 0; tick(d);
    if (d.fifo_count != 0 || d.fifo_attempts != 2) return 10;
    d.fifo_go = 0;

    d.memory_go = 1; d.memory_write = 1; d.address = 1; d.data = 42; tick(d);
    d.memory_write = 0; tick(d);
    if (d.memory_data != 42) return 11;
    d.memory_go = 0;

    d.high = 1; d.low = 1; tick(d);
    if (!d.event_flag || !d.high_seen || d.low_seen) return 12;
    idle(d); tick(d);
    if (d.event_flag || !d.high_seen || d.low_seen) return 13;

    d.rst = 1; tick(d); idle(d); d.rst = 0; d.eval();
    return (d.valid || d.overflow || d.sticky || d.counter_observed ||
            d.fifo_count || d.fifo_attempts || d.memory_data ||
            d.high_seen || d.low_seen) ? 14 : 0;
}
"""


def _module():
    return compile_source(SOURCE, top=TOP).ir


def _run_verilator(
    rtl: tuple[Path, ...],
    tmp_path: Path,
    tag: str,
) -> None:
    harness = tmp_path / f"nested_when_{tag}.cpp"
    harness.write_text(HARNESS)
    object_directory = tmp_path / f"obj_nested_when_{tag}"
    environment = dict(os.environ)
    environment["CCACHE_DISABLE"] = "1"
    build = subprocess.run(
        (
            "verilator",
            "--cc",
            "--exe",
            "--build",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSED",
            "-Wno-UNDRIVEN",
            "--top-module",
            TOP,
            "--Mdir",
            str(object_directory),
            "-o",
            "nested_when_sim",
            *(str(path) for path in rtl),
            str(harness),
        ),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert build.returncode == 0, build.stderr or build.stdout
    run = subprocess.run(
        (str(object_directory / "nested_when_sim"),),
        check=False,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout




@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
def test_nested_when_direct_sv_is_strict_lint_clean_and_cycle_exact(
    tmp_path: Path,
) -> None:
    artifact = emit_sv_artifact(_module())
    rtl = tmp_path / f"{TOP}.sv"
    rtl.write_text(artifact.text)
    lint_with_verilator((rtl,), TOP)
    _run_verilator((rtl,), tmp_path, "direct")
