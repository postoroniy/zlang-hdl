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
# before being locked. The generic implementation graph now uses a versioned
# physical Module projection rather than hashing unused exploration catalogs,
# cost feedback and source provenance. This intentionally changes the eager
# graph-key surface for every case below; the values were compiled twice again
# before recapture, without changing value semantics or generated RTL. The
# formal-artifact namespace cleanup replaces historical development labels with
# descriptive semantic names; because those names are identity-bearing, every
# eager result below was compiled twice again before this recapture.
EXPECTED = {
    "add": "91d0de6db433e96372a664d6a2a0d7e97394495bc1047cd60b10ddedd1824717",
    "stateful_protocol": "e409feafc0bbe25a25141ffeada82dd2fdfef41e89439348eaa6f326ad86bb24",
    "fixed_dsp": "97c893de9105ede0a9c8336047e1d883982d0df464c12f16788fadcc77e0f004",
    "csr": "223dd1762346c87b23eb99f99256107587979f114a97dded6e7534945a383281",
    "hierarchy": "ed232bcb46b32761fefd3041ff036dfdb2190ab56af99a898557d6c647c83e1d",
    # Companion filenames now use exact typed ROM contents/layout rather than
    # source provenance, so equivalent spellings retain one physical image.
    "rom": "569e7792f7cf98d297c4763a1a9824238afe24f725f8e316212cd2653a47165b",
    # The concise Wi-Fi source refactor uses slices, shared raw views, vector
    # generation and struct update while retaining the public ABI and IEEE
    # behavior.  This source/dependency-sensitive eager-result snapshot was
    # independently compiled twice before being locked here.
    "wifi": "63b3c5594245179fa3c62f80f00824cac768f198a4a0bf03661bf39403161e95",
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
            (ROOT / "examples/hierarchy_composition.zhl").read_text(),
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
