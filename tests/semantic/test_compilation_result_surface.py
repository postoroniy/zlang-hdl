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
# recaptured for canonical schema v13, which retains conditional action
# activation predicates and scheduled scalar-output resources explicitly.  The
# present values also include the direct-production backend policy and the
# planning-owned scheduled-value/resource products; semantic typing no longer
# prematurely chooses pipeline placement before a target is known.  Stateful
# entities now also retain their resolved physical clock-domain ownership in
# the eager result surface; canonical schema v14 additionally retains the
# domain of each child-output value. Each value below was independently compiled twice
# before being locked.
EXPECTED = {
    "add": "74093c35c7bfd566484b131f0c31b769192c51349775a068094d59c51d3e1b70",
    "stateful_protocol": "08eb7c05972028f90c79cf66a9ea71f3257a7c5c60e0fd29d3b4aa66aefea9f7",
    "fixed_dsp": "11282ec29ae710ef9ed768494c399775b708f22650050533fe44b6bd853b016a",
    "csr": "85797a67a97c652e101d04b3830b54bd1a2c1cafbbe675457d78d81c050c3e9d",
    "hierarchy": "06f3e1560bf38c7cbb038c768e0f65f32764d3e374bbe611704e4e91a1bee61b",
    # Companion filenames now use exact typed ROM contents/layout rather than
    # source provenance, so equivalent spellings retain one physical image.
    "rom": "fd53bac5688437e8d015596e41c3f55110bd23fd1f923b71bf172e44a82a0c24",
    # The concise Wi-Fi source refactor uses slices, shared raw views, vector
    # generation and struct update while retaining the public ABI and IEEE
    # behavior.  This source/dependency-sensitive eager-result snapshot was
    # independently compiled twice before being locked here.
    "wifi": "0117a5c170e2bb8ac8034071d075052ce1309c11a9b5bf06c194b1ddd500c09d",
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
