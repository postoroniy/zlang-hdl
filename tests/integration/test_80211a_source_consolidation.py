"""Canonical IEEE 802.11a source-layout and migration contracts.

These cases replace the removed historical compatibility fixtures with tests
over the one authoritative IEEE hierarchy.  They deliberately exercise
every concrete source root, the existing safety verification families, and cross-file physical
hierarchy without reviving compatibility modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
from pathlib import Path
import re
import sys

import pytest

from tests.case_matrix import check_cases
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_file
from zlang.formal import build_formal_design, build_recursive_formal_design
from zlang.opt import OptimizationStage, lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError


ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "examples/projects/80211a_transmitter/src"

SOURCE_MODULES = {
    "data_types.zhl": ("WifiRateCodec",),
    "controller.zhl": (
        "IeeeSignalHeader24",
        "IeeeDataFramer24",
        "IeeePacketFramerScrambler24",
    ),
    "scrambler.zhl": ("IeeeDataScrambler24",),
    "conv_encoder.zhl": (
        "IeeeConvolutionalEncode24",
        "IeeeConvolutionalEncoder24",
    ),
    "interleaver.zhl": (
        "IeeeInterleaverBlock48",
        "IeeeInterleaver48",
        "IeeeEncoderInterleaver24",
        "IeeePacketEncoderInterleaver24",
    ),
    "mapper.zhl": (
        "IeeeMapperBlock64",
        "IeeeMapperFrame64",
        "IeeeMapper64",
        "IeeeMapperSerializer64",
        "IeeeMapperStream64",
        "IeeeMappedSampleToIFFT64",
        "IeeePacketMapper64",
    ),
    "ifft_library.zhl": (
        "IFFT64DIFStageExactD4",
        "IFFT64DIFStageExactD8",
        "IFFT64FinalQuantize",
        "IFFT64DIFExactChain",
    ),
    "cyclic_extender.zhl": (
        "IFFT64ReorderCPKernel",
        "IFFT64ReorderCP",
    ),
    "ifft.zhl": (
        "IeeeIFFTFramedInputBoundary",
        "IeeeIFFTFramedOutputBoundary",
        "IeeeIFFTInputStrip",
        "IeeeIFFTOutputAttach",
        "IeeeIFFT64",
        "IeeeFramedIFFT64",
        "IeeeFramedIFFT64Raw",
    ),
    "transmitter.zhl": ("Ieee80211aTransmitter",),
}

_RETIRED_SOURCE_MARKERS = (
    "pro" + "duction_",
    "leg" + "acy_",
    "Pro" + "duction",
)


@dataclass(frozen=True)
class ModuleCase:
    source: str
    module: str
    owner_source: str | None = None
    owner_top: str | None = None


_OWNERS = {
    "IeeeDataScrambler24": (
        "controller.zhl",
        "IeeePacketFramerScrambler24",
    ),
    "IeeeConvolutionalEncoder24": (
        "interleaver.zhl",
        "IeeePacketEncoderInterleaver24",
    ),
    "IeeeInterleaver48": (
        "interleaver.zhl",
        "IeeePacketEncoderInterleaver24",
    ),
    "IeeeMapper64": ("mapper.zhl", "IeeePacketMapper64"),
    "IeeeMapperSerializer64": ("mapper.zhl", "IeeePacketMapper64"),
    "IeeeIFFTFramedOutputBoundary": ("ifft.zhl", "IeeeFramedIFFT64Raw"),
    "IeeeIFFTInputStrip": ("ifft.zhl", "IeeeFramedIFFT64Raw"),
    "IeeeIFFTOutputAttach": ("ifft.zhl", "IeeeFramedIFFT64Raw"),
    "IeeeFramedIFFT64": ("ifft.zhl", "IeeeFramedIFFT64Raw"),
}

_UNINSTANTIATED_CHILDREN = {
    "IeeeEncoderInterleaver24": (
        "interleaver.zhl",
        "IeeePacketEncoderInterleaver24",
    ),
    "IeeeMapperStream64": ("mapper.zhl", "IeeePacketMapper64"),
    "IeeeMappedSampleToIFFT64": ("mapper.zhl", "IeeePacketMapper64"),
}

MODULE_CASES = tuple(
    ModuleCase(source, module, *(_OWNERS.get(module) or (None, None)))
    for source, modules in SOURCE_MODULES.items()
    for module in modules
)
assert len(MODULE_CASES) == 32


def _find_module(root, name: str):
    pending = [root]
    while pending:
        module = pending.pop()
        if module.name == name:
            return module
        pending.extend(module.children)
    raise AssertionError(f"{root.name} hierarchy has no {name}")


def _source_snapshot_digest() -> str:
    """Key test-local reuse by bytes, not mtime or an unvalidated path."""
    digest = hashlib.sha256()
    project = SOURCES.parent
    paths = (
        *project.glob("zlang.toml"),
        *project.glob("zlang.lock"),
        *SOURCES.glob("*.zhl"),
        *(ROOT / "stdlib").rglob("*.zhl"),
    )
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@lru_cache(maxsize=16)
def _cached_owner(path: Path, top: str, snapshot_digest: str):
    return compile_file(path, top=top).ir


def test_owner_reuse_is_bound_to_content_not_timestamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "fixture"
    sources = root / "project" / "src"
    sources.mkdir(parents=True)
    (root / "stdlib").mkdir()
    source = sources / "module.zhl"
    source.write_text("module Witness {}")
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "SOURCES", sources)
    calls: list[str] = []

    class Result:
        def __init__(self, value: str) -> None:
            self.ir = value

    def fake_compile_file(path: Path, *, top: str) -> Result:
        calls.append(path.read_text())
        return Result(f"{top}:{calls[-1]}")

    monkeypatch.setattr(module, "compile_file", fake_compile_file)
    _cached_owner.cache_clear()
    try:
        first_digest = _source_snapshot_digest()
        first = _cached_owner(source, "Witness", first_digest)
        source.touch()
        assert _source_snapshot_digest() == first_digest
        assert _cached_owner(source, "Witness", first_digest) == first
        source.write_text("module Witness { out y:u1 y=1 }")
        next_digest = _source_snapshot_digest()
        assert next_digest != first_digest
        assert _cached_owner(source, "Witness", next_digest) != first
        assert len(calls) == 2
    finally:
        _cached_owner.cache_clear()


def _materialize(case: ModuleCase):
    source = SOURCES / (case.owner_source or case.source)
    top = case.owner_top or case.module
    owner = _cached_owner(source, top, _source_snapshot_digest())
    return owner, _find_module(owner, case.module)


def test_every_concrete_canonical_module_round_trips() -> None:
    fresh_owners = {}

    def check(case: ModuleCase) -> None:
        if case.module in _UNINSTANTIATED_CHILDREN:
            with pytest.raises(
                SemanticError,
                match=(
                    r"top-level input .* cannot expose (?:enum type|type .* "
                    r"because it contains an enum-valued field)"
                ),
            ):
                compile_file(
                    SOURCES / case.source,
                    top=case.module,
                )
            witness_source, witness_top = _UNINSTANTIATED_CHILDREN[case.module]
            witness = compile_file(
                SOURCES / witness_source,
                top=witness_top,
            ).ir
            assert (
                restore(lower(witness, stage=OptimizationStage.HIGH_LEVEL)) == witness
            )
            return

        owner, module = _materialize(case)
        assert module.root_module_identity is not None
        assert module.root_module_identity.logical_path == (
            f"wifi80211a_transmitter.{case.source.removesuffix('.zhl')}"
        )
        round_trip_root = owner if case.owner_source is not None else module
        assert (
            restore(lower(round_trip_root, stage=OptimizationStage.HIGH_LEVEL))
            == round_trip_root
        )
        # Compile once independently per exact owner snapshot. Its hierarchy
        # contains every child checked below, avoiding duplicate large elaborations.
        source = SOURCES / (case.owner_source or case.source)
        top = case.owner_top or case.module
        key = (source, top, _source_snapshot_digest())
        if key not in fresh_owners:
            fresh_owners[key] = compile_file(source, top=top).ir
        again = fresh_owners[key]
        assert again == owner

    check_cases(((case.module, case) for case in MODULE_CASES), check, matrix="802_modules")


def test_canonical_source_unit_has_one_ieee_authoritative_surface() -> None:
    def check(source: str) -> None:
        path = SOURCES / source
        text = path.read_text()
        syntax = parse(text)
        actual = tuple(module.name for module in (*syntax.submodules, syntax))
        if source == "ifft_library.zhl":
            # The parameterized implementation is deliberately a child/template;
            # SOURCE_MODULES lists the four concrete public roots.
            assert actual == (
                "IFFT64DIFStageExactDualBank",
                *SOURCE_MODULES[source],
            )
        else:
            assert actual == SOURCE_MODULES[source]
        assert all(marker not in text for marker in _RETIRED_SOURCE_MARKERS)
        assert re.search(r"(?m)^\s*inst\s+", text) is None
        assert re.search(r"(?m)^\s*connect\s+", text) is None
        assert re.search(r"(?m)^\s*rule\s+\w+\s+when\b", text) is None
        assert (
            re.search(
                r"bitcast<bits<\d+>>\(\s*extend<\d+>\(0\)\s*\)",
                text,
            )
            is None
        )

    check_cases(((source, source) for source in SOURCE_MODULES), check, matrix="802_sources")


@pytest.mark.parametrize(
    ("case", "required_families"),
    (
        (
            ModuleCase("controller.zhl", "IeeeDataFramer24"),
            {"register", "fifo", "ready_valid", "rules", "priority"},
        ),
        (
            ModuleCase(
                "scrambler.zhl",
                "IeeeDataScrambler24",
                "controller.zhl",
                "IeeePacketFramerScrambler24",
            ),
            {"register", "fifo", "ready_valid"},
        ),
        (
            ModuleCase(
                "conv_encoder.zhl",
                "IeeeConvolutionalEncoder24",
                "interleaver.zhl",
                "IeeePacketEncoderInterleaver24",
            ),
            {"register", "fifo", "ready_valid"},
        ),
        (
            ModuleCase(
                "interleaver.zhl",
                "IeeeInterleaver48",
                "interleaver.zhl",
                "IeeePacketEncoderInterleaver24",
            ),
            {"register", "fifo", "ready_valid"},
        ),
    ),
    ids=lambda value: value.module if isinstance(value, ModuleCase) else None,
)
def test_canonical_stateful_modules_retain_existing_safety_verification_families(
    case: ModuleCase,
    required_families: set[str],
) -> None:
    owner, module = _materialize(case)
    design = build_formal_design(module)
    families = {
        (property_.generated_from or "").split(":", 1)[0]
        for property_ in design.properties
    }
    assert required_families <= families

    recursive = build_recursive_formal_design(owner)
    instances = {instance.module_name: instance for instance in recursive.instances}
    instance = instances[case.module]
    state_bindings = [
        binding
        for binding in recursive.bindings
        if binding.ref.instance_identity == instance.instance_identity
        and binding.ref.object_kind == "state"
    ]
    assert state_bindings
    assert all(binding.ref.source_origin is not None for binding in state_bindings)

    artifact = emit_sv_artifact(owner, recursive_design=recursive)
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.recursive_bindings == artifact.recursive_bindings
    assert any(
        binding.instance_identity == instance.instance_identity
        for binding in artifact.recursive_bindings
    )


@pytest.mark.parametrize(
    ("source", "top", "expected_children"),
    (
        (
            "interleaver.zhl",
            "IeeePacketEncoderInterleaver24",
            ("framer", "scrambler", "encoder", "interleaver"),
        ),
        (
            "transmitter.zhl",
            "Ieee80211aTransmitter",
            ("packet_mapper", "transform", "output_boundary"),
        ),
    ),
)
def test_canonical_cross_file_hierarchy_preserves_physical_children(
    source: str,
    top: str,
    expected_children: tuple[str, ...],
) -> None:
    module = compile_file(SOURCES / source, top=top).ir
    assert (
        tuple(instance.instance.name for instance in module.elaborated_instances)
        == expected_children
    )
    instance_ids = {
        instance.instance_identity for instance in module.elaborated_instances
    }
    assert len(instance_ids) == len(module.elaborated_instances)
