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
# eager result below was compiled twice again before this recapture.  The
# current values also bind the content-addressed generic physical graph v2,
# backend DAG-planning schema, and canonical schema v18; semantic behavior is
# unchanged.  Every value below was reproduced in two independent compilations.
EXPECTED = {
    "add": "1138d33309fb4caa5a6a0fff8b9b9c1555a52b2dbfdaa8e415fa9de555d8b7cb",
    "stateful_protocol": "d69f1dd92f4f800c3f45562151b47773c49fbd31adf4cb3c9b3ca9235ae4dd2c",
    "fixed_dsp": "7809d7403205d3579d01fe19a8dbdc6eb7cc06ce7cbf1c0519b4caec0bd1ebdd",
    "csr": "7f4df34013f1e64e399547daa37efce91a208878f686cbf92a9f7ee8745cd2f2",
    "hierarchy": "11add9b266518a902d3dc1a2d04b09378761df78ab78dbd61b1e7494df0747e4",
    # Companion filenames use exact typed ROM contents/layout rather than
    # source provenance, so equivalent spellings retain one physical image.
    # Compile-time evaluator schema v2 records the expanded deterministic-real
    # surface in ROM identity; this snapshot was reproduced in separate runs.
    "rom": "2d545a1115cc2674fb1ca650ad0178894ad93acac0c4ee1db6c79b08bea2f29d",
    # The concise Wi-Fi source refactor uses slices, shared raw views, vector
    # generation and struct update while retaining the public ABI and IEEE
    # behavior.  Its large shared value DAG now uses the bounded Merkle value
    # identity rather than materializing the historical expanded-tree
    # spelling.  This source/dependency-sensitive eager-result snapshot was
    # independently compiled twice before being locked here.
    "wifi": "d28cb374eed5d0a68241df1abb8cfc96afe239c3effb38b8cda635dd5f7dac3b",
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
