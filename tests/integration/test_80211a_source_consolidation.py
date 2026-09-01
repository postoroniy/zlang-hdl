"""Canonical IEEE 802.11a source-layout and migration contracts.

These cases replace the removed historical compatibility fixtures with tests
over the one authoritative IEEE hierarchy.  They deliberately exercise
every concrete source root, the existing M35 families, and cross-file physical
hierarchy without reviving compatibility modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import pytest

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
    "data_types.zl": ("WifiRateCodec",),
    "controller.zl": (
        "IeeeSignalHeader24",
        "IeeeDataFramer24",
        "IeeePacketFramerScrambler24",
    ),
    "scrambler.zl": ("IeeeDataScrambler24",),
    "conv_encoder.zl": (
        "IeeeConvolutionalEncode24",
        "IeeeConvolutionalEncoder24",
    ),
    "interleaver.zl": (
        "IeeeInterleaverBlock48",
        "IeeeInterleaver48",
        "IeeeEncoderInterleaver24",
        "IeeePacketEncoderInterleaver24",
    ),
    "mapper.zl": (
        "IeeeMapperBlock64",
        "IeeeMapperFrame64",
        "IeeeMapper64",
        "IeeeMapperSerializer64",
        "IeeeMapperStream64",
        "IeeeMappedSampleToIFFT64",
        "IeeePacketMapper64",
    ),
    "ifft_library.zl": (
        "IFFT64DIFStageExactD4",
        "IFFT64DIFStageExactD8",
        "IFFT64FinalQuantize",
        "IFFT64DIFExactChain",
    ),
    "cyclic_extender.zl": (
        "IFFT64ReorderCPKernel",
        "IFFT64ReorderCP",
    ),
    "ifft.zl": (
        "IeeeIFFTFramedInputBoundary",
        "IeeeIFFTFramedOutputBoundary",
        "IeeeIFFTInputStrip",
        "IeeeIFFTOutputAttach",
        "IeeeIFFT64",
        "IeeeFramedIFFT64",
        "IeeeFramedIFFT64Raw",
    ),
    "transmitter.zl": ("Ieee80211aTransmitter",),
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
        "controller.zl",
        "IeeePacketFramerScrambler24",
    ),
    "IeeeConvolutionalEncoder24": (
        "interleaver.zl",
        "IeeePacketEncoderInterleaver24",
    ),
    "IeeeInterleaver48": (
        "interleaver.zl",
        "IeeePacketEncoderInterleaver24",
    ),
    "IeeeMapper64": ("mapper.zl", "IeeePacketMapper64"),
    "IeeeMapperSerializer64": ("mapper.zl", "IeeePacketMapper64"),
    "IeeeIFFTFramedOutputBoundary": ("ifft.zl", "IeeeFramedIFFT64Raw"),
    "IeeeIFFTInputStrip": ("ifft.zl", "IeeeFramedIFFT64Raw"),
    "IeeeIFFTOutputAttach": ("ifft.zl", "IeeeFramedIFFT64Raw"),
    "IeeeFramedIFFT64": ("ifft.zl", "IeeeFramedIFFT64Raw"),
}

_UNINSTANTIATED_CHILDREN = {
    "IeeeEncoderInterleaver24": (
        "interleaver.zl",
        "IeeePacketEncoderInterleaver24",
    ),
    "IeeeMapperStream64": ("mapper.zl", "IeeePacketMapper64"),
    "IeeeMappedSampleToIFFT64": ("mapper.zl", "IeeePacketMapper64"),
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


def _materialize(case: ModuleCase):
    source = SOURCES / (case.owner_source or case.source)
    top = case.owner_top or case.module
    owner = compile_file(source, top=top, include_clash=False).ir
    return owner, _find_module(owner, case.module)


@pytest.mark.parametrize("case", MODULE_CASES, ids=lambda case: case.module)
def test_every_concrete_canonical_module_round_trips(case: ModuleCase) -> None:
    if case.module in _UNINSTANTIATED_CHILDREN:
        with pytest.raises(
            SemanticError,
            match="top-level input .* cannot expose enum type",
        ):
            compile_file(
                SOURCES / case.source,
                top=case.module,
                include_clash=False,
            )
        witness_source, witness_top = _UNINSTANTIATED_CHILDREN[case.module]
        witness = compile_file(
            SOURCES / witness_source,
            top=witness_top,
            include_clash=False,
        ).ir
        assert restore(lower(witness, stage=OptimizationStage.HIGH_LEVEL)) == witness
        return

    owner, module = _materialize(case)
    assert module.root_module_identity is not None
    assert module.root_module_identity.logical_path == (
        f"wifi80211a_transmitter.{case.source.removesuffix('.zl')}"
    )
    round_trip_root = owner if case.owner_source is not None else module
    assert (
        restore(lower(round_trip_root, stage=OptimizationStage.HIGH_LEVEL))
        == round_trip_root
    )
    # Owner compilation must stay deterministic even for nominal child ABIs.
    again = compile_file(
        SOURCES / (case.owner_source or case.source),
        top=case.owner_top or case.module,
        include_clash=False,
    ).ir
    assert again == owner


@pytest.mark.parametrize("source", tuple(SOURCE_MODULES))
def test_canonical_source_unit_has_one_ieee_authoritative_surface(
    source: str,
) -> None:
    path = SOURCES / source
    text = path.read_text()
    syntax = parse(text)
    actual = tuple(module.name for module in (*syntax.submodules, syntax))
    if source == "ifft_library.zl":
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
    assert re.search(
        r"bitcast<bits<\d+>>\(\s*extend<\d+>\(0\)\s*\)",
        text,
    ) is None


@pytest.mark.parametrize(
    ("case", "required_families"),
    (
        (
            ModuleCase("controller.zl", "IeeeDataFramer24"),
            {"register", "fifo", "ready_valid", "rules", "priority"},
        ),
        (
            ModuleCase(
                "scrambler.zl",
                "IeeeDataScrambler24",
                "controller.zl",
                "IeeePacketFramerScrambler24",
            ),
            {"register", "fifo", "ready_valid"},
        ),
        (
            ModuleCase(
                "conv_encoder.zl",
                "IeeeConvolutionalEncoder24",
                "interleaver.zl",
                "IeeePacketEncoderInterleaver24",
            ),
            {"register", "fifo", "ready_valid"},
        ),
        (
            ModuleCase(
                "interleaver.zl",
                "IeeeInterleaver48",
                "interleaver.zl",
                "IeeePacketEncoderInterleaver24",
            ),
            {"register", "fifo", "ready_valid"},
        ),
    ),
    ids=lambda value: value.module if isinstance(value, ModuleCase) else None,
)
def test_canonical_stateful_modules_retain_existing_m35_families(
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
    instances = {
        instance.module_name: instance for instance in recursive.instances
    }
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
            "interleaver.zl",
            "IeeePacketEncoderInterleaver24",
            ("framer", "scrambler", "encoder", "interleaver"),
        ),
        (
            "transmitter.zl",
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
    module = compile_file(SOURCES / source, top=top, include_clash=False).ir
    assert tuple(
        instance.instance.name for instance in module.elaborated_instances
    ) == expected_children
    instance_ids = {
        instance.instance_identity for instance in module.elaborated_instances
    }
    assert len(instance_ids) == len(module.elaborated_instances)
