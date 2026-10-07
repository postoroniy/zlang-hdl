"""Durable compatibility snapshots for the eager CompilationResult surface."""

from __future__ import annotations

from hashlib import sha256
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
# unchanged. Physical input snapshots now use filename/content pairs rather
# than absolute-path order or duplicate installed stdlib copies; file-backed
# hashes were recaptured accordingly. Every value below was reproduced in two
# independent compilations.
EXPECTED = {
    "add": "0d21d7c7533fbefcc082993695b6b43120b859ad22bc11553c9aaddd7bf41a35",
    "stateful_protocol": "56bb8d2f6add0c41605e89f5d1b40f64fa320b9540dfc48ac45ca9cdfadc534d",
    "fixed_dsp": "7809d7403205d3579d01fe19a8dbdc6eb7cc06ce7cbf1c0519b4caec0bd1ebdd",
    "csr": "151d81c9a1b0dd25ff25b287cf8dd2a8bf0d69e8541898282a5b7485a0d97e20",
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
    "wifi": "1fa795b1c8ece74b9e7abde45d9a8bf74605f093baa7be0e158f514f1c3840d5",
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


def _physical_input_role_shape(
    paths: tuple[Path, ...],
) -> tuple[tuple[str, str], ...]:
    # An editable checkout and its installed stdlib may contribute two
    # physical copies of the same bytes. Neither their absolute-path order nor
    # that duplicate copy is part of the compilation-result contract.
    return tuple(sorted({(path.name, sha256(path.read_bytes()).hexdigest()) for path in paths}))


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
            # role/content shape without baking a checkout path or its
            # absolute-path ordering into the gate.
            payload = _physical_input_role_shape(value.all_paths)
        elif isinstance(value, str):
            payload = value
        else:
            payload = repr(value)
        per_field[item.name] = stable_digest(payload)
    assert set(per_field) == {item.name for item in fields(result)}
    return stable_digest(per_field)


def test_physical_input_role_shape_ignores_checkout_layout(tmp_path: Path) -> None:
    # all_paths sorts full host paths; stripping directories alone left the
    # Wi-Fi digest dependent on which checkout or venv sorted first.
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    for root in (first_root, second_root):
        (root / "stdlib").mkdir(parents=True)
        (root / "venv").mkdir()
        (root / "project").mkdir()
        (root / "stdlib" / "core.zhl").write_text("same")
        (root / "venv" / "core.zhl").write_text("same")
        (root / "project" / "controller.zhl").write_text("root")
    first = (
        first_root / "stdlib" / "core.zhl",
        first_root / "project" / "controller.zhl",
        first_root / "venv" / "core.zhl",
    )
    second = (
        second_root / "project" / "controller.zhl",
        second_root / "venv" / "core.zhl",
        second_root / "stdlib" / "core.zhl",
    )
    assert _physical_input_role_shape(first) == _physical_input_role_shape(second)
    assert tuple(name for name, _ in _physical_input_role_shape(first)) == (
        "controller.zhl", "core.zhl",
    )
    (second_root / "venv" / "core.zhl").write_text("different")
    assert len(_physical_input_role_shape(second)) == 3


@pytest.mark.parametrize("name", tuple(EXPECTED))
def test_compilation_result_surface_is_stable(name: str) -> None:
    assert _surface_digest(_compile_case(name)) == EXPECTED[name]


def test_session_services_do_not_destabilize_result_surface() -> None:
    assert _surface_digest(_compile_case("add")) == _surface_digest(
        _compile_case("add")
    )
