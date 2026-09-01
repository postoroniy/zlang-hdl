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
# locked here.
EXPECTED = {
    "add": "6b73127fc591f5c5f42c65d57aa69974351898183cf6eaa86c12efb486ecc047",
    "stateful_protocol": "2bcd59ce611f7252938a585a437e0e86eab8858e82331a3cdfaf60bfc77d16e1",
    "fixed_dsp": "85f8ee55ac21fe1e9e5dfa485bcf6dbf6a6f4e3fd67eb89e328153d551fe6c7e",
    "csr": "4d77d36c8f6f2edfddfda81f66ca5a01af5279b9ee6e47aba78e84207c385914",
    "hierarchy": "38daa8c7be62ef94997500865d5c89c62a5bdfe66ecbc65a37c3b5f7991f8638",
    # Companion filenames now use exact typed ROM contents/layout rather than
    # source provenance, so equivalent spellings retain one physical image.
    "rom": "2ec23068c834f2f1b9252b731cbd9c48ccce29ce21e73348b12555a5028cbe54",
    # The concise Wi-Fi source refactor uses slices, shared raw views, vector
    # generation and struct update while retaining the public ABI and IEEE
    # behavior.  This source/dependency-sensitive eager-result snapshot was
    # independently compiled twice before being locked here.
    "wifi": "964cd94a6e3208f9d07433ac5a708f23c9f77292b5e129389d1470f3cdebcc40",
}


def _compile_case(name: str):
    if name == "add":
        return compile_file(ROOT / "examples/add.zl")
    if name == "stateful_protocol":
        return compile_file(ROOT / "examples/fifo_bridge.zl")
    if name == "fixed_dsp":
        return compile_source(
            (ROOT / "examples/fixed_fir_architectures.zl").read_text(),
            top="FixedFIRDspOriented",
        )
    if name == "csr":
        return compile_file(ROOT / "examples/control_csr.zl")
    if name == "hierarchy":
        return compile_source(
            (ROOT / "examples/m40_composition.zl").read_text(),
            top="Composition",
        )
    if name == "rom":
        return compile_source(ROM_SOURCE)
    if name == "wifi":
        return compile_file(
            ROOT / "examples/projects/80211a_transmitter/src/controller.zl",
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
