"""Durable compatibility snapshots for the eager CompilationResult surface."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from zlang.common import stable_digest
from zlang.compiler import compile_file, compile_source

ROOT = Path(__file__).resolve().parents[2]

ROM_SOURCE = """
module ScalarRom<D=4> {
    clock clk reset rst
    in address:u2 out data:u8
    rom table:rom<u8,D> {
        read_latency 1
        init generate(i in 0..D) i
    }
    table.read_address=address
    data=table.read_data
}
"""

# Recaptured after executable formal plans began retaining the exact physical
# ClockDomain contract.  FormalDesign therefore has an explicit clock-domain
# tuple even when it is empty; this changes the durable eager-result surface,
# but not hardware, selected-IR, BackendArtifact, or production-RTL identity.
# Session-owned provider and tool-resolver handles remain represented by their
# stable role.  The values were independently compiled twice before being
# locked here.  File-backed cases were recaptured after the canonical source
# suffix changed from ``.zl`` to ``.zhl``: source-unit and physical-input
# provenance belong to this eager surface even though production RTL identity
# is unchanged.  Cases which publish selected/canonical value products were
# recaptured again for canonical schema v13, which retains conditional action
# activation predicates and scheduled scalar-output resources explicitly.  The
# present values also include the direct-production backend policy and the
# planning-owned scheduled-value/resource products; semantic typing no longer
# prematurely chooses pipeline placement before a target is known.  Each value
# below was independently compiled twice before being locked.
EXPECTED = {
    "add": "d37928b429d25aaa37a1d84a5c50faa548868a23ae69c97441d60a299907cd1a",
    "stateful_protocol": "4561ff131d5f10a0e9af172159a2ddb7aea0fc4abf1ddac7dc6360a78440a9a5",
    "fixed_dsp": "0c4386cd3f5f68ec88c39af512b6749c2210838c07ca2f560ef0688a3a44fdf9",
    "csr": "93de423ee0228861961d0de7e2e326f376afb97fae336773c3a9f9c6c745c2d3",
    "hierarchy": "faa57a0cba72bc45d5cad412c3a934bc29d78018e8ad4d384c729866d3f4f7f3",
    # Companion filenames now use exact typed ROM contents/layout rather than
    # source provenance, so equivalent spellings retain one physical image.
    "rom": "edd9b76f4b46b51a0a3ea27b6558b45589c7437b4217b245017ab886cadf114c",
    # The concise Wi-Fi source refactor uses slices, shared raw views, vector
    # generation and struct update while retaining the public ABI and IEEE
    # behavior.  This source/dependency-sensitive eager-result snapshot was
    # independently compiled twice before being locked here.
    "wifi": "876a1df92d1f549e3e09086b32aa81aa9a1219da4c4bcb0c23f7b46b1ad014b6",
}


def _compile_case(name: str):
    if name == "add":
        return compile_file(ROOT / "examples/add.zhl")
    if name == "stateful_protocol":
        return compile_file(ROOT / "examples/fifo_bridge.zhl")
    if name == "fixed_dsp":
        return compile_source(
            (ROOT / "examples/fixed_fir_architectures.zhl").read_text(),
            top="FixedFIRDspOriented",
        )
    if name == "csr":
        return compile_file(ROOT / "examples/control_csr.zhl")
    if name == "hierarchy":
        return compile_source(
            (ROOT / "examples/m40_composition.zhl").read_text(),
            top="Composition",
        )
    if name == "rom":
        return compile_source(ROM_SOURCE)
    if name == "wifi":
        return compile_file(
            ROOT / "examples/projects/80211a_transmitter/src/controller.zhl",
            include_clash=False,
        )
    raise AssertionError(name)


def _surface_digest(result) -> str:
    per_field: dict[str, str] = {}
    for item in fields(result):
        value = getattr(result, item.name)
        if not item.repr:
            # These handles deliberately carry mutable, compilation-local
            # caches and locks.  Their public role is stable; their Python
            # object identity and discovered-tool state are not part of the
            # durable compiler-result compatibility surface.
            payload = (
                None
                if value is None
                else f"{type(value).__module__}.{type(value).__qualname__}"
            )
        elif item.name == "physical_inputs":
            # Physical roots have no semantic identity.  Preserve the returned
            # role/content shape without baking a checkout path into the gate.
            payload = tuple(path.name for path in value.all_paths)
        elif isinstance(value, str):
            payload = value
        else:
            payload = repr(value)
        per_field[item.name] = stable_digest(payload)
    assert set(per_field) == {item.name for item in fields(result)}
    return stable_digest(per_field)


@pytest.mark.parametrize("name", tuple(EXPECTED))
def test_compilation_result_surface_is_stable(name: str) -> None:
    assert _surface_digest(_compile_case(name)) == EXPECTED[name]


def test_session_services_do_not_destabilize_result_surface() -> None:
    assert _surface_digest(_compile_case("add")) == _surface_digest(
        _compile_case("add")
    )
