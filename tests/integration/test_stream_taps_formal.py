# SPDX-License-Identifier: Apache-2.0
"""Universal binary actual-RTL equivalence, with independent port contracts."""

from pathlib import Path

import pytest

from tests.support.stdlib_formal import run
from tests.support.stream_taps import PAYLOADS, contract, source
from zlang.backend.systemverilog import emit
from zlang.compiler import compile_source
from zlang.ir.formal import FormalStatus, ProofMode


@pytest.mark.parametrize("payload", PAYLOADS, ids=lambda p: p.name)
@pytest.mark.parametrize("rv", (False, True))
def test_actual_rtl_equivalent_to_independent_assignments(tmp_path, payload, rv):
    rtl = emit(compile_source(source(payload, rv=rv), top="Top").ir)
    result = run(rtl, contract(payload, rv=rv), tmp_path / "prove", ProofMode.PROVE)
    assert result.status is FormalStatus.PROVEN, result.reason


@pytest.mark.parametrize("rv,old,new", (
    (False, "tap = input", "tap = 0"),
    (True, "tap_valid = input.valid", "tap_valid = input.ready"),
    (True, "tap_ready = input.ready", "tap_ready = input.valid"),
    (True, "tap_transfer = input.transfer", "tap_transfer = input.valid"),
    (True, "connect input -> output", "output.payload=input.payload output.valid=input.valid input.ready=input.valid"),
))
def test_actual_rtl_mutations_fail(tmp_path, rv, old, new):
    root = Path(__file__).resolve().parents[2]
    body = (root / "stdlib/stream/composition.zhl").read_text().replace("import std.stream.core", "")
    body = (root / "stdlib/stream/core.zhl").read_text() + body
    fixture = source(PAYLOADS[1], rv=rv).replace("import std.stream.composition", "")
    assert old in body
    original = emit(compile_source(body + fixture, top="Top").ir)
    rtl = emit(compile_source(body.replace(old, new, 1) + fixture, top="Top").ir)
    assert rtl != original
    result = run(rtl, contract(PAYLOADS[1], rv=rv), tmp_path / "mutant", ProofMode.BMC)
    assert result.status is FormalStatus.FAILED, result.reason
