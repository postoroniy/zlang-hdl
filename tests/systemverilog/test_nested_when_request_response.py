"""Standalone request/response must retain nested atomic rule effects."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import (
    SystemVerilogEmissionError,
    emit_artifact,
)
from zlang.compiler import compile_source


SOURCE = """
struct NestedRequest { data:u8 }
struct NestedResponse { data:u8 }

module NestedRequester {
  clock clk reset rst
  interface bus:request_response<NestedRequest,NestedResponse> {
    max_outstanding 1
    ordering in_order
  }
  in fire,accept_response,choose,competing:bit
  out observed,event:u8
  reg state:u8=0

  bus.request.payload=NestedRequest { data=7 }
  bus.request.valid=fire
  bus.response.ready=accept_response

  priority {
    accept: when bus.request.transfer {
      when choose {
        state <- 9
        event <- 0xa5
      } else {
        state <- 3
        event <- 0x3c
      }
    }
    fallback: when competing {
      state <- 0xee
      event <- 0xee
    }
  }
  observed=state
}

module NestedResponder {
  clock clk reset rst
  interface bus:request_response<NestedRequest,NestedResponse> {
    max_outstanding 1
    ordering in_order
  }
  in accept,produce,choose:bit
  out observed,event:u8
  reg state:u8=0

  bus.request.ready=accept
  bus.response.payload=NestedResponse { data=11 }
  bus.response.valid=produce

  update: when bus.request.transfer {
    when choose {
      state <- 9
      event <- 0xa5
    } else {
      state <- 3
      event <- 0x3c
    }
  }
  observed=state
}
"""


OUT_OF_ORDER_STATE = """
struct TaggedRequest { id:u2 data:u8 }
struct TaggedResponse { id:u2 data:u8 }

module StatefulTaggedRequester {
  clock clk reset rst
  interface bus:request_response<TaggedRequest,TaggedResponse> {
    max_outstanding 2
    ordering out_of_order
    match_by id
  }
  in fire,accept_response,choose:bit
  out observed:bit
  reg state:bit=0
  bus.request.payload=TaggedRequest { id=1 data=7 }
  bus.request.valid=fire
  bus.response.ready=accept_response
  update: when bus.request.transfer {
    when choose { state <- 1 }
  }
  observed=state
}
"""


HARNESS = r"""
#include "VNestedRequester.h"
#include "verilated.h"

static void settle(VNestedRequester& d) {
    d.clk = 0;
    d.eval();
}

static void tick(VNestedRequester& d) {
    d.clk = 0; d.eval();
    d.clk = 1; d.eval();
    d.clk = 0; d.eval();
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    VNestedRequester d;
    d.fire = 1;
    d.accept_response = 1;
    d.choose = 0;
    d.competing = 1;
    d.bus_request_ready = 1;
    d.bus_response_payload_data = 0;
    d.bus_response_valid = 0;

    d.rst = 1;
    settle(d);
    if (d.bus_request_valid || d.bus_response_ready || d.zlang_event)
        return 1;
    tick(d);
    if (d.observed != 0) return 2;

    // Both rules are enabled.  The accepted request rule wins and its false
    // nested branch commits both effects; the entire fallback group loses.
    d.rst = 0;
    settle(d);
    if (!d.bus_request_valid || d.bus_request_payload_data != 7) return 3;
    if (d.zlang_event != 0x3c || d.observed != 0) return 4;
    tick(d);
    if (d.observed != 3) return 5;

    // The in-order ledger now blocks a second request.  Therefore the higher
    // rule guard is false and the lower group may commit atomically.
    settle(d);
    if (d.bus_request_valid || d.zlang_event != 0xee) return 6;
    tick(d);
    if (d.observed != 0xee) return 7;

    // Retire the outstanding request.
    d.fire = 0;
    d.competing = 0;
    d.bus_response_valid = 1;
    settle(d);
    if (!d.bus_response_ready) return 8;
    tick(d);

    // At count zero, an accepted request permits its same-cycle response.
    // Both ledger transfers occur, so count remains zero, while the selected
    // true nested branch commits exactly once.
    d.fire = 1;
    d.choose = 1;
    d.competing = 1;
    settle(d);
    if (!d.bus_request_valid || !d.bus_response_ready) return 9;
    if (d.zlang_event != 0xa5) return 10;
    tick(d);
    if (d.observed != 9 || !d.bus_request_valid) return 11;

    // Build a nonzero ledger/state once more, then prove reset owns both
    // state contributors and suppresses every conditional output immediately.
    d.bus_response_valid = 0;
    d.competing = 0;
    tick(d);
    if (d.bus_request_valid) return 12;
    d.rst = 1;
    settle(d);
    if (d.bus_request_valid || d.bus_response_ready || d.zlang_event) return 13;
    tick(d);
    if (d.observed != 0) return 14;
    d.rst = 0;
    d.fire = 0;
    settle(d);
    if (d.bus_request_valid || d.observed != 0) return 15;
    return 0;
}
"""


def _module(top: str):
    return compile_source(SOURCE, top=top).ir


def test_standalone_roles_publish_deterministic_activation_aware_artifacts() -> None:
    for top in ("NestedRequester", "NestedResponder"):
        module = _module(top)
        first = emit_artifact(module)
        second = emit_artifact(module)

        assert first == second
        assert "_outstanding" in first.text
        assert "_request_transfer" in first.text
        assert "_response_transfer" in first.text
        assert "zlang_condition_0_active" in first.text
        assert "rule_" in first.text
        assert "assign zlang_event =" in first.text

        restored = BackendArtifact.from_json(first.to_json())
        assert restored.artifact_hash == first.artifact_hash
        assert restored.selected_ir_identity == first.selected_ir_identity
        assert restored.bindings == first.bindings
        assert {
            item.semantic_signal_id for item in first.bindings
        } >= {
            "port:observed",
            "port:event",
            "port:bus.request.valid",
            "port:bus.response.valid",
        }


def test_out_of_order_user_state_remains_explicitly_fail_closed() -> None:
    module = compile_source(
        OUT_OF_ORDER_STATE,
        top="StatefulTaggedRequester",
    ).ir
    with pytest.raises(SystemVerilogEmissionError) as raised:
        emit_artifact(module)
    assert raised.value.diagnostic.code == (
        "ZL-BACKEND-SYSTEMVERILOG-REQUEST-RESPONSE-STATE"
    )
    assert "out-of-order request/response" in str(raised.value)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
@pytest.mark.parametrize("top", ("NestedRequester", "NestedResponder"))
def test_standalone_nested_request_response_is_strict_lint_clean(
    top: str,
    tmp_path: Path,
) -> None:
    rtl = tmp_path / f"{top}.sv"
    rtl.write_text(emit_artifact(_module(top)).text)
    completed = subprocess.run(
        (
            "verilator",
            "--lint-only",
            "-Wall",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSED",
            "-Wno-UNDRIVEN",
            "--top-module",
            top,
            str(rtl),
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_requester_ledger_nested_effects_reset_and_priority_are_atomic(
    tmp_path: Path,
) -> None:
    rtl = tmp_path / "NestedRequester.sv"
    harness = tmp_path / "nested_requester.cpp"
    rtl.write_text(emit_artifact(_module("NestedRequester")).text)
    harness.write_text(HARNESS)
    object_directory = tmp_path / "obj"
    environment = dict(os.environ)
    environment["CCACHE_DISABLE"] = "1"
    built = subprocess.run(
        (
            "verilator",
            "--cc",
            "--exe",
            "--build",
            "-Wall",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSED",
            "-Wno-UNDRIVEN",
            "--top-module",
            "NestedRequester",
            "--Mdir",
            str(object_directory),
            "-o",
            "nested_requester",
            str(rtl),
            str(harness),
        ),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert built.returncode == 0, built.stderr or built.stdout
    run = subprocess.run(
        (str(object_directory / "nested_requester"),),
        check=False,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout
