from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import (
    SystemVerilogEmissionError,
    emit_artifact,
    emit_experimental,
)
from zlang.compiler import compile_file
from zlang.ir.interfaces import RequestResponseRole
from zlang.opt import OptimizationStage, lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError


ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
INITIALIZED_INTERNAL_WIRE = re.compile(r"(?m)^[ \t]*wire\b[^;\n]*=")


def _assert_explicit_internal_drivers(text: str, context: str) -> None:
    match = INITIALIZED_INTERNAL_WIRE.search(text)
    assert match is None, f"{context}: declaration assignment {match.group(0)!r}"


@dataclass(frozen=True)
class ChildExpectation:
    diagnostic: str
    witness_top: str
    witness_relative: str | None = None


CHILD_OR_TEMPLATE_ONLY = {
    ("ztpu_banked_memory.zhl", "ZtpuMemoryReplica"): ChildExpectation(
        "parameter constraint cannot be discharged.*runtime value 'D'",
        "ZtpuBankedMemory",
    ),
    ("ztpu_banked_memory.zhl", "ReplicatedBanked2R1W"): ChildExpectation(
        "parameter constraint cannot be discharged.*runtime value 'BANKS'",
        "ZtpuBankedMemory",
    ),
    ("fft/sdf_stage_numeric.zhl", "FFTSDFStageNumeric"): ChildExpectation(
        "unknown type 'S'", "FFTSDFStageNumericD4"
    ),
    ("simple_dma_m40.zhl", "TransferEngine"): ChildExpectation(
        "request/response interfaces require a module clock and reset",
        "SimpleDMA",
    ),
    (
        "projects/80211a_transmitter/src/ifft_library.zhl",
        "IFFT64DIFStageExactDualBank",
    ): ChildExpectation(
        "unknown type, module, or protocol 'Complex'",
        "IFFT64DIFStageExactD4",
    ),
    (
        "projects/80211a_transmitter/src/conv_encoder.zhl",
        "IeeeConvolutionalEncoder24",
    ): ChildExpectation(
        "top-level input 'input' cannot expose enum type",
        "IeeePacketEncoderInterleaver24",
        "projects/80211a_transmitter/src/interleaver.zhl",
    ),
    (
        "projects/80211a_transmitter/src/interleaver.zhl",
        "IeeeInterleaver48",
    ): ChildExpectation(
        "top-level input 'input' cannot expose enum type",
        "IeeePacketEncoderInterleaver24",
    ),
    (
        "projects/80211a_transmitter/src/interleaver.zhl",
        "IeeeEncoderInterleaver24",
    ): ChildExpectation(
        "top-level input 'input' cannot expose enum type",
        "IeeePacketEncoderInterleaver24",
    ),
    (
        "projects/80211a_transmitter/src/scrambler.zhl",
        "IeeeDataScrambler24",
    ): ChildExpectation(
        "top-level input 'input' cannot expose enum type",
        "IeeePacketFramerScrambler24",
        "projects/80211a_transmitter/src/controller.zhl",
    ),
    (
        "projects/80211a_transmitter/src/ifft.zhl",
        "IeeeIFFTFramedOutputBoundary",
    ): ChildExpectation(
        "top-level input 'input' cannot expose enum type",
        "IeeeFramedIFFT64Raw",
    ),
    (
        "projects/80211a_transmitter/src/ifft.zhl",
        "IeeeIFFTInputStrip",
    ): ChildExpectation(
        "top-level input 'input' cannot expose enum type",
        "IeeeFramedIFFT64Raw",
    ),
    (
        "projects/80211a_transmitter/src/ifft.zhl",
        "IeeeIFFTOutputAttach",
    ): ChildExpectation(
        "top-level input 'frame_meta' cannot expose enum type",
        "IeeeFramedIFFT64Raw",
    ),
    (
        "projects/80211a_transmitter/src/ifft.zhl",
        "IeeeFramedIFFT64",
    ): ChildExpectation(
        "top-level input 'input' cannot expose enum type",
        "IeeeFramedIFFT64Raw",
    ),
    (
        "projects/80211a_transmitter/src/mapper.zhl",
        "IeeeMapper64",
    ): ChildExpectation(
        "top-level input 'input' cannot expose enum type",
        "IeeePacketMapper64",
    ),
    (
        "projects/80211a_transmitter/src/mapper.zhl",
        "IeeeMapperSerializer64",
    ): ChildExpectation(
        "top-level input 'input' cannot expose enum type",
        "IeeePacketMapper64",
    ),
    (
        "projects/80211a_transmitter/src/mapper.zhl",
        "IeeeMapperStream64",
    ): ChildExpectation(
        "top-level input 'input' cannot expose enum type",
        "IeeePacketMapper64",
    ),
    (
        "projects/80211a_transmitter/src/mapper.zhl",
        "IeeeMappedSampleToIFFT64",
    ): ChildExpectation(
        "top-level input 'input' cannot expose enum type",
        "IeeePacketMapper64",
    ),
}


DIRECT_UNSUPPORTED: dict[tuple[str, str], str] = {}


def _roots():
    for path in sorted(EXAMPLES.rglob("*.zhl")):
        source = path.read_text()
        syntax = parse(source)
        relative = path.relative_to(EXAMPLES).as_posix()
        for module in (*syntax.submodules, syntax):
            yield path, relative, source, module.name


@cache
def _direct_result(path: Path, top: str):
    # File-backed compilation is part of this exhaustive corpus contract:
    # project examples may import sibling modules through their zlang.toml
    # namespace, which a detached source string deliberately cannot resolve.
    return compile_file(path, top=top, include_clash=False)


def test_every_example_module_root_has_an_explicit_direct_status() -> None:
    roots = tuple(_roots())
    assert len({path for path, *_ in roots}) == 88
    assert len(roots) == 178

    standalone = 0
    child_only = 0
    unsupported = 0
    witnesses: set[tuple[str, str]] = set()
    for path, relative, _, top in roots:
        key = (relative, top)
        if key in CHILD_OR_TEMPLATE_ONLY:
            child_only += 1
            expectation = CHILD_OR_TEMPLATE_ONLY[key]
            with pytest.raises(
                (SemanticError, SystemVerilogEmissionError),
                match=expectation.diagnostic,
            ):
                emit_experimental(_direct_result(path, top).ir)
            witness_relative = expectation.witness_relative or relative
            witness = (witness_relative, expectation.witness_top)
            if witness not in witnesses:
                witness_path = EXAMPLES / witness_relative
                witness_result = _direct_result(
                    witness_path, expectation.witness_top
                )
                artifact = emit_artifact(witness_result.ir)
                _assert_explicit_internal_drivers(
                    artifact.text,
                    f"{witness_relative}::{expectation.witness_top}",
                )
                assert artifact.to_json()
                witnesses.add(witness)
            continue
        if key in DIRECT_UNSUPPORTED:
            unsupported += 1
            with pytest.raises(
                SystemVerilogEmissionError,
                match=DIRECT_UNSUPPORTED[key],
            ):
                emit_experimental(_direct_result(path, top).ir)
            continue

        standalone += 1
        result = _direct_result(path, top)
        assert result.clash == ""
        artifact = emit_artifact(result.ir)
        _assert_explicit_internal_drivers(artifact.text, f"{relative}::{top}")
        assert artifact.to_json()

    assert (standalone, child_only, unsupported) == (161, 17, 0)


@pytest.mark.parametrize(
    ("relative", "top"),
    (
        ("hierarchical_request_response_m40.zhl", "Requester"),
        ("hierarchical_request_response_m40.zhl", "Responder"),
        ("simple_dma_m40.zhl", "MemoryModel"),
        ("multichannel_dma.zhl", "DMAMemoryModel"),
    ),
)
def test_request_response_role_survives_canonical_round_trip(
    relative: str,
    top: str,
) -> None:
    result = _direct_result(EXAMPLES / relative, top)
    restored = restore(lower(result.ir, stage=OptimizationStage.HIGH_LEVEL))
    assert restored == result.ir
    expected_role = (
        RequestResponseRole.REQUESTER
        if top == "Requester"
        else RequestResponseRole.RESPONDER
    )
    assert result.ir.request_responses[0].role is expected_role
    assert all(
        assignment.target is result.ir.request_responses[0]
        for assignment in result.ir.assignments
        if assignment.target.name == result.ir.request_responses[0].name
    )


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator is required")
@pytest.mark.toolchain_smoke
@pytest.mark.exhaustive_toolchain
def test_every_standalone_supported_example_root_passes_strict_lint(tmp_path: Path) -> None:
    checked = 0
    for path, relative, _, top in _roots():
        key = (relative, top)
        if key in CHILD_OR_TEMPLATE_ONLY or key in DIRECT_UNSUPPORTED:
            continue
        generated = emit_experimental(_direct_result(path, top).ir)
        _assert_explicit_internal_drivers(generated, f"{relative}::{top}")
        path = tmp_path / f"{checked:03d}_{top}.sv"
        path.write_text(generated)
        completed = subprocess.run(
            (
                "verilator", "--lint-only", "-Wno-DECLFILENAME",
                "-Wno-UNUSED", "-Wno-UNDRIVEN", "--top-module", top,
                str(path),
            ),
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, (
            f"{relative}::{top}\n{completed.stderr}"
        )
        checked += 1
    assert checked == 161
