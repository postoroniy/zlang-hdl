"""End-to-end validation for one source-authored numerical SDF stage.

The oracle below deliberately does not call the ZLang evaluator.  It models
the fixed-point recurrence with exact ``Fraction`` values and is therefore an
independent check of simulator and RTL behavior.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import fields, is_dataclass, replace
from fractions import Fraction
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

from zlang.backend.clash import emit as emit_clash
from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.clash.public_wrapper import ClashPublicTopWrapper
from zlang.backend.companions import publish_companion_bundle
from zlang.backend.manifest import (
    BackendArtifact,
    COMPANION_MANIFEST_VERSION,
    RECURSIVE_MANIFEST_VERSION,
)
from zlang.backend.systemverilog import emit_experimental
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.build_manifest import WholeBuildManifest
from zlang.compiler import _inline_locals, compile_file, compile_source
from zlang.formal import build_formal_design, build_recursive_formal_design, run_formal
from zlang.ir import FixedConvert, FixedType, FormalStatus, ProofMode
from zlang.ir.callables import expand_callable_calls
from zlang.opt import OptimizationStage, lower, restore
from zlang.parser import parse
from zlang.semantic import analyze
from zlang.simulate import simulate_cycles
from zlang.targets import select_implementation_graph
from zlang.toolchain import find_clash_executable, generate_verilog


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "examples" / "fft" / "sdf_stage_numeric.zhl"
D = 4
FFT4_TOP = "FFT4SDFReference"
FFT8_TOP = "FFT8SDFReference"
FFT16_TOP = "FFT16SDFReference"
FFT32_TOP = "FFT32SDFReference"
FFT512_TOP = "FFT512SDFReference"
SAMPLE_WIDTH = 18
SAMPLE_FRACTION = 16
TWIDDLE_WIDTH = 16
TWIDDLE_FRACTION = 14


def test_concrete_d4_wrapper_elaborates_explicit_protocol_hierarchy() -> None:
    result = compile_source(SOURCE.read_text(), top="FFTSDFStageNumericD4")
    assert result.ir.instances[0].name == "stage"
    assert result.ir.elaborated_instances[0].semantic_path == (
        "FFTSDFStageNumericD4", "stage"
    )
    assert result.ir.elaborated_instances[0].specialization_identity
    assert len(result.ir.hierarchical_connections) == 2
    assert {
        (edge.source.owner, edge.source.name, edge.destination.owner, edge.destination.name)
        for edge in result.ir.hierarchical_connections
    } == {
        ("FFTSDFStageNumericD4", "input", "stage", "input"),
        ("stage", "output", "FFTSDFStageNumericD4", "output"),
    }
    assert "module FFTSDFStageNumericD4" in emit_experimental(result.ir)


def test_concrete_d4_backend_artifact_v4_preserves_hierarchy_and_bindings() -> None:
    """Both emitters publish the same typed hierarchy, even when physical
    formal observation ports are not available in the production ABI.

    The recursive manifest is intentionally checked by semantic identity and
    instance path.  No RTL name is inferred by this test: direct-SV locators
    are checked only as backend-published metadata, while Clash's production
    artifact keeps unavailable observations explicit until its formal wrapper
    materializes them.
    """
    result = compile_source(SOURCE.read_text(), top="FFTSDFStageNumericD4")
    design = build_recursive_formal_design(result.ir)
    child_node = next(
        item for item in design.instances
        if item.physical_instance_path == ("FFTSDFStageNumericD4", "stage")
    )
    expected_paths = {
        ("FFTSDFStageNumericD4",),
        ("FFTSDFStageNumericD4", "stage"),
    }
    expected_top_ports = {
        "port:input.payload", "port:input.valid", "port:input.ready",
        "port:output.payload", "port:output.valid", "port:output.ready",
    }
    expected_child_ports = {
        "port:input.payload", "port:input.valid",
        "port:input.ready", "port:output.payload", "port:output.valid",
        "port:output.ready",
    }
    for emit_backend in (emit_clash_artifact, emit_sv_artifact):
        artifact = emit_backend(result.ir, recursive_design=design)
        assert artifact.manifest_version == max(
            RECURSIVE_MANIFEST_VERSION, COMPANION_MANIFEST_VERSION
        )
        restored = BackendArtifact.from_json(artifact.to_json())
        assert restored.instances == artifact.instances
        assert restored.recursive_bindings == artifact.recursive_bindings

        instances = {
            item.physical_instance_path: item for item in artifact.instances
        }
        assert set(instances) == expected_paths
        child = instances[("FFTSDFStageNumericD4", "stage")]
        assert child.source_instance_name == "stage"
        assert child.instance_identity != child.specialization_identity
        assert child.specialization_identity == child_node.specialization_identity

        by_path = {}
        for binding in artifact.recursive_bindings:
            by_path.setdefault(binding.physical_instance_path, []).append(binding)
        top_ids = {item.local_semantic_id for item in by_path[("FFTSDFStageNumericD4",)]}
        child_ids = {item.local_semantic_id for item in by_path[("FFTSDFStageNumericD4", "stage")]}
        assert expected_top_ports <= top_ids
        assert expected_child_ports <= child_ids
        assert {item.width for item in by_path[("FFTSDFStageNumericD4",)]
                if item.local_semantic_id == "port:input.payload"} == {36}
        assert len(artifact.companions) == 1
        companion = artifact.companions[0]
        assert companion.object_kind == "rom_image"
        assert companion.word_width == 32
        assert companion.depth == D
        assert companion.read_latency == 1
        assert companion.content_hash == result.ir.children[0].roms[0].content_hash

        child_state = [
            item for item in by_path[("FFTSDFStageNumericD4", "stage")]
            if item.object_kind == "state"
            and not item.local_semantic_id.startswith("rule:")
        ]
        rule_fire = [
            item for item in by_path[("FFTSDFStageNumericD4", "stage")]
            if item.local_semantic_id.startswith("rule:")
        ]
        assert any(item.local_semantic_id == "register:phase" for item in child_state)
        assert any(item.local_semantic_id == "fifo:feedback.count" for item in child_state)
        assert child_state and all(item.source_origin for item in child_state)
        assert rule_fire and all(item.signal_token is None for item in rule_fire)

        if artifact.backend == "direct_systemverilog":
            # Direct-SV publishes stable physical tokens for the production
            # component ABI.  They remain non-formal until an allow-listed
            # observation wrapper is requested.
            stage_ports = [item for item in by_path[("FFTSDFStageNumericD4", "stage")]
                           if item.object_kind == "port"
                           and item.local_semantic_id not in {
                               "port:input", "port:output",
                           }]
            assert stage_ports and all(item.rtl_module and item.signal_token
                                       for item in stage_ports)
            protocol_bases = [
                item for item in by_path[("FFTSDFStageNumericD4", "stage")]
                if item.local_semantic_id in {"port:input", "port:output"}
            ]
            assert protocol_bases and all(
                item.signal_token is None for item in protocol_bases
            )
            assert all(item.rtl_module and item.rtl_path == ("stage",)
                       and item.signal_token for item in stage_ports + child_state)
            assert all(not item.physical_available for item in stage_ports + child_state)
        else:
            # Clash production emission does not fabricate formal observation
            # ports; unavailable physical locations are explicit.
            assert all(not item.physical_available
                       for item in artifact.recursive_bindings)


def _semantic_stage(depth: int = D):
    parsed = parse(SOURCE.read_text())
    # The promoted source also contains a concrete wrapper.  The reusable
    # generic child remains the semantic unit for this Python-specialized
    # regression; the wrapper's ordinary CLI boundary is tested separately.
    syntax = replace(
        parsed.submodules[0],
        imports=parsed.imports,
        type_aliases=parsed.type_aliases,
        structs=parsed.structs,
        functions=parsed.functions,
        operators=parsed.operators,
        protocols=parsed.protocols,
        submodules=(),
    )
    defaults = {
        "D": depth,
        "CW": (depth * 2 - 1).bit_length(),
        "IW": max(1, depth.bit_length() - 1),
    }
    syntax = replace(
        syntax,
        parameters=tuple(
            replace(parameter, default=defaults.get(parameter.name, parameter.default))
            for parameter in syntax.parameters
        ),
    )
    return analyze(
        syntax,
        specialization_type_bindings={
            "S": FixedType(SAMPLE_WIDTH, SAMPLE_FRACTION),
            "W": FixedType(TWIDDLE_WIDTH, TWIDDLE_FRACTION),
        },
    )


def _stage(depth: int = D):
    return _inline_locals(_semantic_stage(depth))


def _fixed_converts(value: object) -> list[FixedConvert]:
    found: list[FixedConvert] = []

    def visit(item: object) -> None:
        if isinstance(item, FixedConvert):
            found.append(item)
        if is_dataclass(item):
            for field in fields(item):
                if field.name != "origin":
                    visit(getattr(item, field.name))
        elif isinstance(item, tuple):
            for child in item:
                visit(child)

    visit(value)
    return found


def _input_ref_names(value: object) -> set[str]:
    """Collect immutable-local references from a typed action/expression tree."""
    names: set[str] = set()

    def visit(item: object) -> None:
        if getattr(item, "__class__", None).__name__ == "InputRef":
            name = getattr(item, "name", None)
            if isinstance(name, str):
                names.add(name)
        if is_dataclass(item):
            for field in fields(item):
                if field.name != "origin":
                    visit(getattr(item, field.name))
        elif isinstance(item, tuple):
            for child in item:
                visit(child)

    visit(value)
    return names


def _nearest_even(value: Fraction) -> int:
    numerator, denominator = value.numerator, value.denominator
    sign = -1 if numerator < 0 else 1
    numerator = abs(numerator)
    quotient, remainder = divmod(numerator, denominator)
    if 2 * remainder > denominator or (
        2 * remainder == denominator and quotient & 1
    ):
        quotient += 1
    return sign * quotient


def _decoded(raw: int, width: int, fraction: int) -> Fraction:
    if raw >= 1 << (width - 1):
        raw -= 1 << width
    return Fraction(raw, 1 << fraction)


def _sample_quantize(value: Fraction) -> int:
    raw = _nearest_even(value * (1 << SAMPLE_FRACTION))
    return max(
        -(1 << (SAMPLE_WIDTH - 1)),
        min((1 << (SAMPLE_WIDTH - 1)) - 1, raw),
    )


def _twiddles(depth: int) -> tuple[dict[str, int], ...]:
    # Independent precomputed nearest-even Q2.14 values for
    # exp(-j*2*pi*k/(2*depth)); these do not come from semantic ROM contents.
    by_depth = {
        1: ((16384, 0),),
        2: ((16384, 0), (0, -16384)),
        4: (
            (16384, 0), (11585, -11585), (0, -16384), (-11585, -11585),
        ),
        8: (
            (16384, 0), (15137, -6270), (11585, -11585), (6270, -15137),
            (0, -16384), (-6270, -15137), (-11585, -11585), (-15137, -6270),
        ),
        16: (
            (16384, 0), (16069, -3196), (15137, -6270), (13623, -9102),
            (11585, -11585), (9102, -13623), (6270, -15137),
            (3196, -16069), (0, -16384), (-3196, -16069),
            (-6270, -15137), (-9102, -13623), (-11585, -11585),
            (-13623, -9102), (-15137, -6270), (-16069, -3196),
        ),
    }
    return tuple({"re": real, "im": imag} for real, imag in by_depth[depth])


def _cycles(depth: int = D) -> tuple[list[dict[str, object]], list[bool]]:
    count = max(44, depth * 7)
    cycles: list[dict[str, object]] = []
    resets: list[bool] = []
    for index in range(count):
        resets.append(index in (0, count // 2))
        cycles.append(
            {
                "input": {
                    "payload": {
                        "re": (index * 701) % 24000 - 12000,
                        "im": (index * 433) % 10000 - 5000,
                    },
                    "valid": int(index not in (3, 4, 11, 24)),
                },
                "output": {"ready": int(index not in (8, 9, 10, 18, 19, 28))},
            }
        )
    return cycles, resets


def _oracle(
    cycles: list[dict[str, object]], resets: list[bool], depth: int = D
) -> list[dict[str, object]]:
    """Exact radix-2 DIF SDF recurrence, observed before each clock edge."""

    fifo: list[dict[str, int]] = []
    phase = 0
    output = {"re": 0, "im": 0}
    output_valid = 0
    results: list[dict[str, object]] = []
    for cycle, reset in zip(cycles, resets, strict=True):
        if reset:
            fifo = []
            phase = 0
            output = {"re": 0, "im": 0}
            output_valid = 0

        input_port = cycle["input"]
        output_port = cycle["output"]
        input_ready = int((not output_valid) or bool(output_port["ready"]))
        input_transfer = bool(input_port["valid"]) and bool(input_ready)
        output_transfer = bool(output_valid) and bool(output_port["ready"])
        results.append(
            {
                "input": {"ready": input_ready, "transfer": int(input_transfer)},
                "output": {
                    "payload": dict(output),
                    "valid": output_valid,
                    "transfer": int(output_transfer),
                },
            }
        )
        if reset:
            continue

        action: str | None = None
        if input_transfer and phase < depth and len(fifo) < depth:
            action = "fill"
        elif input_transfer and phase < depth and len(fifo) >= depth:
            action = "low"
        elif input_transfer and phase >= depth:
            action = "high"

        if action == "fill":
            fifo.append(dict(input_port["payload"]))
            phase = (phase + 1) % (2 * depth)
        elif action == "low":
            delayed = fifo.pop(0)
            fifo.append(dict(input_port["payload"]))
            twiddle = _twiddles(depth)[phase]
            delayed_re = _decoded(delayed["re"], SAMPLE_WIDTH, SAMPLE_FRACTION)
            delayed_im = _decoded(delayed["im"], SAMPLE_WIDTH, SAMPLE_FRACTION)
            twiddle_re = _decoded(twiddle["re"], TWIDDLE_WIDTH, TWIDDLE_FRACTION)
            twiddle_im = _decoded(twiddle["im"], TWIDDLE_WIDTH, TWIDDLE_FRACTION)
            output = {
                "re": _sample_quantize(delayed_re * twiddle_re - delayed_im * twiddle_im),
                "im": _sample_quantize(delayed_re * twiddle_im + delayed_im * twiddle_re),
            }
            output_valid = 1
            phase = (phase + 1) % (2 * depth)
        elif action == "high":
            delayed = fifo.pop(0)
            current = input_port["payload"]
            delayed_re = _decoded(delayed["re"], SAMPLE_WIDTH, SAMPLE_FRACTION)
            delayed_im = _decoded(delayed["im"], SAMPLE_WIDTH, SAMPLE_FRACTION)
            current_re = _decoded(current["re"], SAMPLE_WIDTH, SAMPLE_FRACTION)
            current_im = _decoded(current["im"], SAMPLE_WIDTH, SAMPLE_FRACTION)
            fifo.append(
                {
                    "re": _sample_quantize(delayed_re - current_re),
                    "im": _sample_quantize(delayed_im - current_im),
                }
            )
            output = {
                "re": _sample_quantize(delayed_re + current_re),
                "im": _sample_quantize(delayed_im + current_im),
            }
            output_valid = 1
            phase = (phase + 1) % (2 * depth)
        elif output_transfer:
            output_valid = 0
    return results


def test_stage_specialization_round_trip_and_target_report() -> None:
    module = _stage()
    assert module.fifos[0].depth == D
    assert module.fifos[0].element_type == module.ports[0].type
    assert module.resolved_transition is not None
    restored = restore(lower(module, stage=OptimizationStage.HIGH_LEVEL))
    assert restored == module
    graph = select_implementation_graph(module, target="xc7z030ffg676-1")
    assert graph.is_generic


def test_named_complex_quantization_has_one_typed_boundary_per_result() -> None:
    module = _semantic_stage()
    names = {item.name for item in module.locals}
    assert {
        "low_exact", "low_quantized", "high_sum_exact", "high_sum",
        "high_diff_exact", "high_diff",
    } <= names
    quantized = {
        item.name: item.expression
        for item in module.locals
        if item.name in {"low_quantized", "high_sum", "high_diff"}
    }
    definitions = (*module.functions, *module.callable_definitions)
    conversions = [
        conversion
        for expression in quantized.values()
        for conversion in _fixed_converts(
            expand_callable_calls(expression, definitions)
        )
    ]
    assert len(conversions) == 6
    assert len(set(conversions)) == 6
    # Rule actions refer to the named aggregate once and project fields only
    # after that aggregate conversion; no FixedConvert is duplicated in the
    # resolved transition itself.
    assert not _fixed_converts(module.resolved_transition)
    low_actions = next(item for item in module.rules if item.name == "low").actions
    high_actions = next(item for item in module.rules if item.name == "high").actions
    assert _input_ref_names(low_actions) >= {"low_quantized"}
    # FIFO push operands are represented by StateAction in the resolved
    # transition rather than by a NextAssignment in the source rule list.
    assert _input_ref_names(high_actions) | _input_ref_names(module.resolved_transition) >= {
        "high_sum", "high_diff",
    }
    rtl = emit_experimental(_stage())
    push_assignment = next(
        line for line in rtl.splitlines() if "assign feedback_push_data =" in line
    )
    # Retained calls pass the live operands to two shared monomorphic helpers
    # (complex subtraction, then final quantization).  The conversion body is
    # no longer cloned into this rule assignment.
    assert "feedback_front" in push_assignment
    assert push_assignment.count("zlang_spec_") == 2


def test_stage_m35_generation_and_unbound_execution_are_explicitly_skipped() -> None:
    design = build_formal_design(_stage())
    families = {item.id.split(".")[1] for item in design.properties}
    assert {"register", "fifo", "ready_valid", "rules"} <= families
    results = run_formal(design, mode=ProofMode.BMC, depth=4, solver="z3")
    assert results
    assert all(item.status is FormalStatus.SKIPPED for item in results)
    assert all(item.reason for item in results)


@pytest.mark.parametrize("depth", (8, 16))
def test_same_stage_specializes_for_larger_fft_delay_depth(depth: int) -> None:
    module = _stage(depth)
    assert module.fifos[0].depth == depth
    assert module.roms[0].depth == depth
    assert str(module.roms[0].element_type) == "Complex<fixed<16,14>>"
    assert tuple(
        (word.fields[0][1].value, word.fields[1][1].value)
        for word in module.roms[0].contents
    ) == tuple((item["re"], item["im"]) for item in _twiddles(depth))


@pytest.mark.parametrize("depth", (4, 8, 16))
def test_stage_simulator_matches_exact_fraction_oracle_under_gaps_stalls_reset(
    depth: int,
) -> None:
    cycles, resets = _cycles(depth)
    expected = _oracle(cycles, resets, depth)
    actual = simulate_cycles(_stage(depth), cycles, reset=resets)
    assert actual == expected
    assert any(item["output"]["valid"] for item in actual)
    assert any(not item["input"]["transfer"] for item in actual)
    assert all(
        item["output"]["payload"]["re"] == int(item["output"]["payload"]["re"])
        for item in actual
    )


def test_stage_continuous_ready_valid_traffic_reaches_ii_one() -> None:
    cycles = [
        {
            "input": {"payload": {"re": index * 257, "im": -index * 101}, "valid": 1},
            "output": {"ready": 1},
        }
        for index in range(20)
    ]
    actual = simulate_cycles(_stage(), cycles, reset=[True] + [False] * 19)
    transfers = [
        index for index, item in enumerate(actual) if item["output"]["transfer"]
    ]
    assert transfers
    assert transfers == list(range(transfers[0], len(actual)))


def _complex_add(
    left: dict[str, int], right: dict[str, int]
) -> dict[str, int]:
    return {
        field: _sample_quantize(
            _decoded(left[field], SAMPLE_WIDTH, SAMPLE_FRACTION)
            + _decoded(right[field], SAMPLE_WIDTH, SAMPLE_FRACTION)
        )
        for field in ("re", "im")
    }


def _complex_subtract(
    left: dict[str, int], right: dict[str, int]
) -> dict[str, int]:
    return {
        field: _sample_quantize(
            _decoded(left[field], SAMPLE_WIDTH, SAMPLE_FRACTION)
            - _decoded(right[field], SAMPLE_WIDTH, SAMPLE_FRACTION)
        )
        for field in ("re", "im")
    }


def _complex_twiddle_multiply(
    value: dict[str, int], twiddle: dict[str, int]
) -> dict[str, int]:
    value_re = _decoded(value["re"], SAMPLE_WIDTH, SAMPLE_FRACTION)
    value_im = _decoded(value["im"], SAMPLE_WIDTH, SAMPLE_FRACTION)
    twiddle_re = _decoded(twiddle["re"], TWIDDLE_WIDTH, TWIDDLE_FRACTION)
    twiddle_im = _decoded(twiddle["im"], TWIDDLE_WIDTH, TWIDDLE_FRACTION)
    return {
        "re": _sample_quantize(value_re * twiddle_re - value_im * twiddle_im),
        "im": _sample_quantize(value_re * twiddle_im + value_im * twiddle_re),
    }


def _fft4_frame_oracle(
    frame: tuple[dict[str, int], dict[str, int], dict[str, int], dict[str, int]],
) -> tuple[dict[str, int], ...]:
    """Independent staged numerical contract in stream order 0, 2, 1, 3.

    Every helper above quantizes once to the architectural sample type.  This
    deliberately models the D=2 and D=1 stage boundaries, rather than applying
    one final quantization to an ideal mathematical DFT.
    """

    x0, x1, x2, x3 = frame
    p0 = _complex_add(x0, x2)
    d0 = _complex_subtract(x0, x2)
    p1 = _complex_add(x1, x3)
    d1 = _complex_subtract(x1, x3)
    a2 = _complex_twiddle_multiply(d0, {"re": 16384, "im": 0})
    a3 = _complex_twiddle_multiply(d1, {"re": 0, "im": -16384})
    return (
        _complex_add(p0, p1),
        _complex_twiddle_multiply(
            _complex_subtract(p0, p1), {"re": 16384, "im": 0}
        ),
        _complex_add(a2, a3),
        _complex_twiddle_multiply(
            _complex_subtract(a2, a3), {"re": 16384, "im": 0}
        ),
    )


def _dif_sdf_frame_oracle(
    frame: tuple[dict[str, int], ...],
) -> tuple[dict[str, int], ...]:
    """Apply the exact quantized DIF stages, retaining streaming order."""

    size = len(frame)
    assert size >= 2 and size & (size - 1) == 0
    values = [dict(item) for item in frame]
    span = size
    while span >= 2:
        half = span // 2
        twiddles = _twiddles(half)
        for base in range(0, size, span):
            source = [dict(item) for item in values[base : base + span]]
            for index in range(half):
                upper = _complex_add(source[index], source[index + half])
                difference = _complex_subtract(
                    source[index], source[index + half]
                )
                lower = _complex_twiddle_multiply(
                    difference, twiddles[index]
                )
                values[base + index] = upper
                values[base + half + index] = lower
        span //= 2
    return tuple(values)


def _fft4_fixture() -> tuple[dict[str, int], ...]:
    return (
        {"re": 1000, "im": 200},
        {"re": -300, "im": 500},
        {"re": 700, "im": -100},
        {"re": -200, "im": -400},
    )


def _fft8_fixture() -> tuple[dict[str, int], ...]:
    return (
        {"re": 1000, "im": 200},
        {"re": -300, "im": 500},
        {"re": 700, "im": -100},
        {"re": -200, "im": -400},
        {"re": 400, "im": 300},
        {"re": -600, "im": 100},
        {"re": 250, "im": -350},
        {"re": -150, "im": 450},
    )


def _fft16_fixture() -> tuple[dict[str, int], ...]:
    return (
        {"re": 1000, "im": 200},
        {"re": -300, "im": 500},
        {"re": 700, "im": -100},
        {"re": -200, "im": -400},
        {"re": 400, "im": 300},
        {"re": -600, "im": 100},
        {"re": 250, "im": -350},
        {"re": -150, "im": 450},
        {"re": 350, "im": -250},
        {"re": -450, "im": -150},
        {"re": 550, "im": 50},
        {"re": -750, "im": 250},
        {"re": 125, "im": -225},
        {"re": -275, "im": 375},
        {"re": 625, "im": -475},
        {"re": -50, "im": 150},
    )


def _fft32_fixture() -> tuple[dict[str, int], ...]:
    return tuple(
        {"re": 300 + index * 29, "im": -700 + index * 31}
        for index in range(32)
    )


def _fft4_cycles() -> tuple[list[dict[str, object]], list[bool]]:
    # Three accepted zeros advance the remaining bins of the finite fixture.
    # They are the beginning of the following stream frame, not a special
    # flush operation.  The two-cycle output stall exercises backpressure and
    # payload holding after all source tokens have been accepted.
    tokens = [*_fft4_fixture(), *({"re": 0, "im": 0} for _ in range(3))]
    cycles: list[dict[str, object]] = [
        {
            "input": {"payload": {"re": 0, "im": 0}, "valid": 0},
            "output": {"ready": 1},
        }
    ]
    cycles.extend(
        {
            "input": {"payload": dict(token), "valid": 1},
            "output": {"ready": 1},
        }
        for token in tokens
    )
    cycles.extend(
        {
            "input": {"payload": {"re": 0, "im": 0}, "valid": 0},
            "output": {"ready": int(index >= 2)},
        }
        for index in range(7)
    )
    return cycles, [True] + [False] * (len(cycles) - 1)


def _fft4_reset_gap_cycles() -> tuple[list[dict[str, object]], list[bool]]:
    idle = {"input": {"payload": {"re": 0, "im": 0}, "valid": 0},
            "output": {"ready": 1}}
    cycles: list[dict[str, object]] = [
        {"input": {"payload": {"re": 10, "im": 20}, "valid": 1},
         "output": {"ready": 1}},
        {"input": {"payload": {"re": 30, "im": 40}, "valid": 1},
         "output": {"ready": 1}},
        dict(idle),
    ]
    frame_and_padding = [
        _fft4_fixture()[0], None, _fft4_fixture()[1], _fft4_fixture()[2],
        None, _fft4_fixture()[3],
        {"re": 0, "im": 0}, {"re": 0, "im": 0}, {"re": 0, "im": 0},
    ]
    cycles.extend(
        {
            "input": {
                "payload": dict(token or {"re": 0, "im": 0}),
                "valid": int(token is not None),
            },
            "output": {"ready": 1},
        }
        for token in frame_and_padding
    )
    cycles.extend(
        {
            "input": {"payload": {"re": 0, "im": 0}, "valid": 0},
            "output": {"ready": int(index >= 2)},
        }
        for index in range(8)
    )
    resets = [False, False, True] + [False] * (len(cycles) - 3)
    return cycles, resets


def _fft8_cycles() -> tuple[list[dict[str, object]], list[bool]]:
    tokens = [*_fft8_fixture(), *({"re": 0, "im": 0} for _ in range(7))]
    cycles: list[dict[str, object]] = [
        {
            "input": {"payload": {"re": 0, "im": 0}, "valid": 0},
            "output": {"ready": 1},
        }
    ]
    cycles.extend(
        {
            "input": {"payload": dict(token), "valid": 1},
            "output": {"ready": 1},
        }
        for token in tokens
    )
    cycles.extend(
        {
            "input": {"payload": {"re": 0, "im": 0}, "valid": 0},
            "output": {"ready": int(index >= 2)},
        }
        for index in range(10)
    )
    return cycles, [True] + [False] * (len(cycles) - 1)


def _fft8_reset_gap_cycles() -> tuple[list[dict[str, object]], list[bool]]:
    frame = _fft8_fixture()
    stream: list[dict[str, int] | None] = [
        {"re": 9, "im": 11},
        {"re": 13, "im": 17},
        None,
        frame[0], frame[1], None, frame[2], frame[3], frame[4], None,
        frame[5], frame[6], frame[7],
        *({"re": 0, "im": 0} for _ in range(10)),
        *(None for _ in range(12)),
    ]
    cycles = [
        {
            "input": {
                "payload": dict(payload or {"re": 0, "im": 0}),
                "valid": int(payload is not None),
            },
            "output": {"ready": int(index not in (15, 16, 17))},
        }
        for index, payload in enumerate(stream)
    ]
    return cycles, [False, False, True] + [False] * (len(cycles) - 3)


def _fft16_cycles() -> tuple[list[dict[str, object]], list[bool]]:
    tokens = [*_fft16_fixture(), *({"re": 0, "im": 0} for _ in range(15))]
    cycles: list[dict[str, object]] = [
        {
            "input": {"payload": {"re": 0, "im": 0}, "valid": 0},
            "output": {"ready": 1},
        }
    ]
    cycles.extend(
        {
            "input": {"payload": dict(token), "valid": 1},
            "output": {"ready": 1},
        }
        for token in tokens
    )
    cycles.extend(
        {
            "input": {"payload": {"re": 0, "im": 0}, "valid": 0},
            "output": {"ready": int(index >= 3)},
        }
        for index in range(10)
    )
    return cycles, [True] + [False] * (len(cycles) - 1)


def _fft16_reset_gap_cycles() -> tuple[list[dict[str, object]], list[bool]]:
    frame = _fft16_fixture()
    post_reset_stream: list[dict[str, int] | None] = [
        *frame[:3], None, *frame[3:10], None, *frame[10:],
    ]
    stream: list[dict[str, int] | None] = [
        {"re": 9, "im": 11},
        {"re": 13, "im": 17},
        {"re": 19, "im": 23},
        None,
        *post_reset_stream,
        *({"re": 0, "im": 0} for _ in range(19)),
        *(None for _ in range(16)),
    ]
    stalled_cycles = (25, 26, 27, 28)
    cycles = [
        {
            "input": {
                "payload": dict(payload or {"re": 0, "im": 0}),
                "valid": int(payload is not None),
            },
            "output": {"ready": int(index not in stalled_cycles)},
        }
        for index, payload in enumerate(stream)
    ]
    return cycles, [False, False, False, True] + [False] * (len(cycles) - 4)


def _fft32_cycles() -> tuple[list[dict[str, object]], list[bool]]:
    tokens = [*_fft32_fixture(), *({"re": 0, "im": 0} for _ in range(31))]
    cycles: list[dict[str, object]] = [
        {
            "input": {"payload": {"re": 0, "im": 0}, "valid": 0},
            "output": {"ready": 1},
        }
    ]
    cycles.extend(
        {
            "input": {"payload": dict(token), "valid": 1},
            "output": {"ready": 1},
        }
        for token in tokens
    )
    cycles.extend(
        {
            "input": {"payload": {"re": 0, "im": 0}, "valid": 0},
            "output": {"ready": int(index >= 4)},
        }
        for index in range(12)
    )
    return cycles, [True] + [False] * (len(cycles) - 1)


def _fft32_reset_gap_cycles() -> tuple[list[dict[str, object]], list[bool]]:
    frame = _fft32_fixture()
    post_reset_stream: list[dict[str, int] | None] = [
        *frame[:5], None, *frame[5:18], None,
        *frame[18:26], None, *frame[26:],
    ]
    stream: list[dict[str, int] | None] = [
        {"re": 1, "im": 2},
        {"re": 3, "im": 4},
        {"re": 5, "im": 6},
        {"re": 7, "im": 8},
        None,
        *post_reset_stream,
        *({"re": 0, "im": 0} for _ in range(36)),
        *(None for _ in range(8)),
    ]
    stalled_cycles = tuple(range(44, 49))
    cycles = [
        {
            "input": {
                "payload": dict(payload or {"re": 0, "im": 0}),
                "valid": int(payload is not None),
            },
            "output": {"ready": int(index not in stalled_cycles)},
        }
        for index, payload in enumerate(stream)
    ]
    return cycles, [False, False, False, False, True] + [False] * (len(cycles) - 5)


def test_fft4_reference_elaboration_round_trip_and_companions() -> None:
    result = compile_source(SOURCE.read_text(), top=FFT4_TOP, include_clash=False)
    module = result.ir
    assert [item.name for item in module.instances] == ["stage_d2", "stage_d1"]
    assert [
        tuple((item.name, item.value) for item in instance.specializations)
        for instance in module.instances
    ] == [
        (
            ("S", "fixed<18,16>"), ("W", "fixed<16,14>"),
            ("D", 2), ("CW", 2), ("IW", 1),
        ),
        (
            ("S", "fixed<18,16>"), ("W", "fixed<16,14>"),
            ("D", 1), ("CW", 1), ("IW", 1),
        ),
    ]
    assert [child.fifos[0].depth for child in module.children] == [2, 1]
    assert [child.roms[0].depth for child in module.children] == [2, 1]
    assert len({item.instance_identity for item in module.elaborated_instances}) == 2
    assert len({item.specialization_identity for item in module.elaborated_instances}) == 2
    assert {
        (
            connection.source.owner, connection.source.name,
            connection.destination.owner, connection.destination.name,
        )
        for connection in module.hierarchical_connections
    } == {
        (FFT4_TOP, "input", "stage_d2", "input"),
        ("stage_d2", "output", "stage_d1", "input"),
        ("stage_d1", "output", FFT4_TOP, "output"),
    }
    assert restore(lower(module, stage=OptimizationStage.HIGH_LEVEL)) == module
    graph = select_implementation_graph(module, target="xc7z030ffg676-1")
    assert graph.is_generic
    assert graph.realization_backend == "backend_independent"
    assert graph.latency_knowledge == "unknown"

    for emit_backend in (emit_clash_artifact, emit_sv_artifact):
        artifact = emit_backend(module)
        restored_artifact = BackendArtifact.from_json(artifact.to_json())
        assert restored_artifact.components == artifact.components
        assert restored_artifact.instances == artifact.instances
        assert restored_artifact.recursive_bindings == artifact.recursive_bindings
        assert [
            (
                item.logical_path, item.file_hash, item.semantic_id,
                item.word_width, item.depth, item.content_hash,
            )
            for item in restored_artifact.companions
        ] == [
            (
                item.logical_path, item.file_hash, item.semantic_id,
                item.word_width, item.depth, item.content_hash,
            )
            for item in artifact.companions
        ]
        assert sorted(item.depth for item in artifact.companions) == [1, 2]
        assert {item.word_width for item in artifact.companions} == {32}
        assert len({item.content_hash for item in artifact.companions}) == 2


def test_fft4_staged_numerical_contract_has_exact_known_fixture() -> None:
    assert _fft4_frame_oracle(_fft4_fixture()) == (
        {"re": 1200, "im": 200},
        {"re": 2200, "im": 0},
        {"re": 1200, "im": 400},
        {"re": -600, "im": 200},
    )


def test_fft8_reference_elaboration_round_trip_and_companions() -> None:
    result = compile_source(SOURCE.read_text(), top=FFT8_TOP, include_clash=False)
    module = result.ir
    assert [item.name for item in module.instances] == [
        "stage_d4", "stage_d2", "stage_d1",
    ]
    assert [
        tuple((item.name, item.value) for item in instance.specializations)
        for instance in module.instances
    ] == [
        (
            ("S", "fixed<18,16>"), ("W", "fixed<16,14>"),
            ("D", 4), ("CW", 3), ("IW", 2),
        ),
        (
            ("S", "fixed<18,16>"), ("W", "fixed<16,14>"),
            ("D", 2), ("CW", 2), ("IW", 1),
        ),
        (
            ("S", "fixed<18,16>"), ("W", "fixed<16,14>"),
            ("D", 1), ("CW", 1), ("IW", 1),
        ),
    ]
    assert [child.fifos[0].depth for child in module.children] == [4, 2, 1]
    assert [child.roms[0].depth for child in module.children] == [4, 2, 1]
    assert len({item.instance_identity for item in module.elaborated_instances}) == 3
    assert len({item.specialization_identity for item in module.elaborated_instances}) == 3
    assert len(module.hierarchical_connections) == 4
    assert restore(lower(module, stage=OptimizationStage.HIGH_LEVEL)) == module

    graph = select_implementation_graph(module, target="xc7z030ffg676-1")
    assert graph.is_generic
    assert graph.realization_backend == "backend_independent"
    assert graph.latency_knowledge == "unknown"

    for emit_backend in (emit_clash_artifact, emit_sv_artifact):
        artifact = emit_backend(module)
        assert sorted(item.depth for item in artifact.companions) == [1, 2, 4]
        assert {item.word_width for item in artifact.companions} == {32}
        assert len({item.logical_path for item in artifact.companions}) == 3
        assert len({item.content_hash for item in artifact.companions}) == 3


def test_fft8_staged_numerical_contract_has_exact_known_fixture() -> None:
    assert _dif_sdf_frame_oracle(_fft4_fixture()) == _fft4_frame_oracle(
        _fft4_fixture()
    )
    assert _dif_sdf_frame_oracle(_fft8_fixture()) == (
        {"re": 1100, "im": 700},
        {"re": 3600, "im": -600},
        {"re": 1000, "im": 1500},
        {"re": -100, "im": 400},
        {"re": 779, "im": 157},
        {"re": 921, "im": -1257},
        {"re": -215, "im": -711},
        {"re": 915, "im": 1411},
    )


def test_fft16_staged_numerical_contract_has_exact_known_fixture() -> None:
    assert _dif_sdf_frame_oracle(_fft16_fixture()) == (
        {"re": 1225, "im": 425},
        {"re": 6775, "im": -2125},
        {"re": 125, "im": 1375},
        {"re": -625, "im": 425},
        {"re": 1600, "im": 384},
        {"re": 1600, "im": -1384},
        {"re": -1188, "im": 250},
        {"re": 1288, "im": 250},
        {"re": 1603, "im": 93},
        {"re": 1455, "im": 187},
        {"re": 2766, "im": -230},
        {"re": -1124, "im": 650},
        {"re": 254, "im": 1598},
        {"re": -782, "im": 560},
        {"re": -543, "im": -35},
        {"re": 1571, "im": 777},
    )


def test_fft32_staged_numerical_contract_has_exact_known_fixture() -> None:
    assert _dif_sdf_frame_oracle(_fft32_fixture()) == (
        {"re": 23984, "im": -7024},
        {"re": -464, "im": -496},
        {"re": -960, "im": -32},
        {"re": 32, "im": -960},
        {"re": -1662, "im": 624},
        {"re": -258, "im": -688},
        {"re": -670, "im": -304},
        {"re": 734, "im": -1616},
        {"re": -2958, "im": 1836},
        {"re": -366, "im": -588},
        {"re": -796, "im": -186},
        {"re": 280, "im": -1190},
        {"re": -1206, "im": 198},
        {"re": -134, "im": -806},
        {"re": -562, "im": -404},
        {"re": 2030, "im": -2828},
        {"re": -5501, "im": 4213},
        {"re": -415, "im": -541},
        {"re": -871, "im": -115},
        {"re": 139, "im": -1061},
        {"re": -1393, "im": 372},
        {"re": -199, "im": -744},
        {"re": -612, "im": -355},
        {"re": 1172, "im": -2025},
        {"re": -2100, "im": 1032},
        {"re": -312, "im": -636},
        {"re": -730, "im": -248},
        {"re": 462, "im": -1364},
        {"re": -1069, "im": 70},
        {"re": -55, "im": -878},
        {"re": -512, "im": -451},
        {"re": 4572, "im": -5205},
    )


@pytest.mark.parametrize(
    ("top_name", "expected_depths"),
    (
        (FFT4_TOP, [1, 2]),
        (FFT8_TOP, [1, 2, 4]),
        (FFT16_TOP, [1, 2, 4, 8]),
        (FFT32_TOP, [1, 2, 4, 8, 16]),
        (FFT512_TOP, [1, 2, 4, 8, 16, 32, 64, 128, 256]),
    ),
)
def test_fft_cli_publishes_roms_source_map_and_whole_build(
    tmp_path: Path, top_name: str, expected_depths: list[int],
) -> None:
    rtl = tmp_path / f"{top_name}.sv"
    source_map = tmp_path / f"{top_name}.source-map.json"
    manifest_path = tmp_path / f"{top_name}.build.json"
    completed = subprocess.run(
        (
            sys.executable, "-m", "zlang.cli", str(SOURCE),
            "--top", top_name,
            "--systemverilog", str(rtl),
            "--source-map", str(source_map),
            "--build-manifest", str(manifest_path),
        ),
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert rtl.is_file()
    assert isinstance(json.loads(source_map.read_text()), dict)

    manifest = WholeBuildManifest.from_json(manifest_path.read_text())
    compilation = compile_file(SOURCE, top=top_name, include_clash=False)
    assert manifest.selected_ir.identity == compilation.selected_ir_identity
    assert manifest.high_level_ir.identity == compilation.high_level_ir_identity
    assert len(manifest.backend_builds) == 1
    backend = manifest.backend_builds[0]
    assert backend.source_map_hash is not None
    expected_companions = emit_sv_artifact(compilation.ir).companions
    assert sorted(item.depth for item in expected_companions) == expected_depths
    assert {
        (Path(item.logical_path).name, item.content_hash)
        for item in backend.companions
    } == {
        (Path(item.logical_path).name, item.file_hash)
        for item in expected_companions
    }
    assert all((rtl.parent / Path(item.logical_path).name).is_file()
               for item in backend.companions)


def test_fft4_hierarchy_simulator_matches_independent_staged_oracle() -> None:
    cycles, resets = _fft4_cycles()
    module = compile_source(
        SOURCE.read_text(), top=FFT4_TOP, include_clash=False
    ).ir
    results = simulate_cycles(module, cycles, reset=resets)
    transfers = tuple(
        item["output"]["payload"]
        for item in results
        if item["output"]["transfer"]
    )
    assert transfers == _fft4_frame_oracle(_fft4_fixture())
    stalled = [
        item["output"]["payload"]
        for item in results
        if item["output"]["valid"] and not item["output"]["transfer"]
    ]
    assert len(stalled) >= 2
    assert all(item == stalled[0] for item in stalled)


def _rtl_records(
    rtl: Path | Sequence[Path],
    *,
    direct: bool,
    cycles,
    resets,
    top_name: str = "FFTSDFStageNumeric",
    lint_waivers: tuple[str, ...] = ("-Wno-fatal",),
) -> list[tuple[int, int, int, int, int]]:
    rtl_sources = (
        (rtl,)
        if isinstance(rtl, Path)
        else tuple(sorted((Path(item) for item in rtl), key=lambda item: item.as_posix()))
    )
    assert rtl_sources, "Verilator requires at least one generated RTL source"
    rtl_working_directory = rtl_sources[0].parent
    rtl_arguments = tuple(str(item) for item in rtl_sources)
    connection = (
        ".clk(clk), .rst(rst), "
        ".input_payload_re(in_payload_re), .input_payload_im(in_payload_im), "
        ".input_valid(in_valid), .input_ready(in_ready), "
        ".output_payload_re(out_payload_re), .output_payload_im(out_payload_im), "
        ".output_valid(out_valid), .output_ready(out_ready)"
    )
    lines = [
        "module tb;",
        "logic clk=0; always #5 clk=~clk;",
        "logic rst; logic signed [17:0] in_payload_re, in_payload_im;",
        "logic in_valid; wire in_ready;",
        "wire signed [17:0] out_payload_re, out_payload_im;",
        "wire out_valid; logic out_ready;",
        f"{top_name} dut({connection});",
        "initial begin",
    ]
    for index, (cycle, reset) in enumerate(zip(cycles, resets, strict=True)):
        payload = cycle["input"]["payload"]
        lines.extend(
            [
                f"rst={int(reset)}; in_valid={cycle['input']['valid']}; "
                f"out_ready={cycle['output']['ready']}; "
                f"in_payload_re=18'h{payload['re'] & 0x3FFFF:05x}; "
                f"in_payload_im=18'h{payload['im'] & 0x3FFFF:05x};",
                f"#1; $display(\"REC %0d %0d %0d %0d %0d\", {index}, in_ready, out_valid, "
                "$signed(out_payload_re), $signed(out_payload_im));",
                "@(posedge clk); #1;",
            ]
        )
    lines.extend(["$finish;", "end", "endmodule"])
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        bench = root / "tb.sv"
        bench.write_text("\n".join(lines))
        output = root / "obj"
        environment = os.environ.copy()
        environment["CCACHE_DISABLE"] = "1"
        lint = subprocess.run(
            (
                "verilator", "--lint-only", *lint_waivers, "-Wno-DECLFILENAME",
                "-Wno-UNUSED", "-Wno-UNDRIVEN", "--top-module", top_name,
                *rtl_arguments,
            ),
            capture_output=True, text=True,
        )
        assert lint.returncode == 0, lint.stderr
        build = subprocess.run(
            (
                "verilator", "--binary", "--timing", "-Wno-fatal",
                "--Mdir", str(output), "--top-module", "tb", *rtl_arguments,
                str(bench),
            ),
            cwd=root, env=environment, capture_output=True, text=True,
        )
        assert build.returncode == 0, build.stderr
        run = subprocess.run(
            (str(output / "Vtb"),), cwd=rtl_working_directory,
            capture_output=True, text=True
        )
        assert run.returncode == 0, run.stderr or run.stdout
        pattern = re.compile(r"REC (\d+) (\d+) (\d+) (-?\d+) (-?\d+)")
        return [tuple(map(int, match.groups())) for match in map(pattern.search, run.stdout.splitlines()) if match]


@pytest.mark.skipif(
    shutil.which("verilator") is None or find_clash_executable() is None,
    reason="Clash and Verilator are required for RTL parity",
)
def test_stage_direct_sv_and_clash_verilator_match_simulator() -> None:
    cycles, resets = _cycles()
    expected = _oracle(cycles, resets)
    module = _stage()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        direct_artifact = emit_sv_artifact(module)
        direct = root / "direct.sv"
        direct.write_text(direct_artifact.text)
        publish_companion_bundle(direct_artifact.companions, root)
        direct_records = _rtl_records(direct, direct=True, cycles=cycles, resets=resets)
        clash_artifact = emit_clash_artifact(module)
        clash = generate_verilog(
            clash_artifact.text,
            "FFTSDFStageNumeric",
            root / "clash",
            find_clash_executable(),
            companions=clash_artifact.companions,
            public_wrapper=ClashPublicTopWrapper.build(module),
        )
        clash_records = _rtl_records(clash, direct=False, cycles=cycles, resets=resets)

    assert len(direct_records) == len(cycles)
    assert direct_records == clash_records
    for index, record in enumerate(direct_records):
        cycle, input_ready, output_valid, output_re, output_im = record
        if resets[index]:
            continue
        assert input_ready == expected[index]["input"]["ready"]
        assert output_valid == expected[index]["output"]["valid"]
        if output_valid:
            assert (output_re, output_im) == (
                expected[index]["output"]["payload"]["re"],
                expected[index]["output"]["payload"]["im"],
            )


@pytest.mark.skipif(
    shutil.which("verilator") is None or find_clash_executable() is None,
    reason="Clash and Verilator are required for wrapper RTL parity",
)
def test_concrete_d4_wrapper_direct_sv_and_clash_match_oracle() -> None:
    cycles, resets = _cycles()
    expected = _oracle(cycles, resets)
    result = compile_source(SOURCE.read_text(), top="FFTSDFStageNumericD4")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        direct_artifact = emit_sv_artifact(result.ir)
        direct = root / "direct.sv"
        direct.write_text(direct_artifact.text)
        publish_companion_bundle(direct_artifact.companions, root)
        direct_records = _rtl_records(
            direct,
            direct=True,
            cycles=cycles,
            resets=resets,
            top_name="FFTSDFStageNumericD4",
        )
        clash_artifact = emit_clash_artifact(result.ir)
        clash = generate_verilog(
            clash_artifact.text,
            "FFTSDFStageNumericD4",
            root / "clash",
            find_clash_executable(),
            companions=clash_artifact.companions,
            public_wrapper=ClashPublicTopWrapper.build(result.ir),
        )
        clash_records = _rtl_records(
            clash,
            direct=False,
            cycles=cycles,
            resets=resets,
            top_name="FFTSDFStageNumericD4",
        )

    assert direct_records == clash_records
    for index, record in enumerate(direct_records):
        _cycle, input_ready, output_valid, output_re, output_im = record
        if resets[index]:
            continue
        assert input_ready == expected[index]["input"]["ready"]
        assert output_valid == expected[index]["output"]["valid"]
        if output_valid:
            assert (output_re, output_im) == (
                expected[index]["output"]["payload"]["re"],
                expected[index]["output"]["payload"]["im"],
            )


@pytest.mark.skipif(
    shutil.which("verilator") is None or find_clash_executable() is None,
    reason="Clash and Verilator are required for FFT4 RTL parity",
)
@pytest.mark.parametrize(
    ("cycles", "resets"),
    (_fft4_cycles(), _fft4_reset_gap_cycles()),
    ids=("continuous-stall", "gaps-midstream-reset"),
)
def test_fft4_reference_direct_sv_and_clash_match_staged_oracle(
    cycles: list[dict[str, object]], resets: list[bool],
) -> None:
    expected = _fft4_frame_oracle(_fft4_fixture())
    result = compile_source(SOURCE.read_text(), top=FFT4_TOP, include_clash=False)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        direct_artifact = emit_sv_artifact(result.ir)
        direct = root / "direct.sv"
        direct.write_text(direct_artifact.text)
        published = publish_companion_bundle(direct_artifact.companions, root)
        assert sorted(item.depth for item in direct_artifact.companions) == [1, 2]
        assert len(published) == 2
        direct_records = _rtl_records(
            direct,
            direct=True,
            cycles=cycles,
            resets=resets,
            top_name=FFT4_TOP,
            lint_waivers=(),
        )

        clash_artifact = emit_clash_artifact(result.ir)
        clash = generate_verilog(
            clash_artifact.text,
            FFT4_TOP,
            root / "clash",
            find_clash_executable(),
            companions=clash_artifact.companions,
            public_wrapper=ClashPublicTopWrapper.build(result.ir),
        )
        clash_records = _rtl_records(
            clash,
            direct=False,
            cycles=cycles,
            resets=resets,
            top_name=FFT4_TOP,
            # Clash 1.11's romFile blackbox widens its Verilog array index to
            # the host Int width.  This established generated-ROM waiver does
            # not hide payload, state, or arithmetic width diagnostics.
            lint_waivers=("-Wno-WIDTHTRUNC",),
        )

    assert direct_records == clash_records
    transfers = [
        {"re": record[3], "im": record[4]}
        for record in direct_records
        if not resets[record[0]]
        and record[2]
        and cycles[record[0]]["output"]["ready"]
    ]
    assert tuple(transfers[:4]) == expected
    assert len(transfers) == 4

    stalled = [
        (record[3], record[4])
        for record in direct_records
        if record[2] and not cycles[record[0]]["output"]["ready"]
    ]
    assert len(stalled) >= 2
    assert len(set(stalled)) == 1


@pytest.mark.skipif(
    shutil.which("verilator") is None or find_clash_executable() is None,
    reason="Clash and Verilator are required for FFT8 RTL parity",
)
@pytest.mark.parametrize(
    ("cycles", "resets"),
    (_fft8_cycles(), _fft8_reset_gap_cycles()),
    ids=("continuous-stall", "gaps-midstream-reset"),
)
def test_fft8_reference_direct_sv_and_clash_match_staged_oracle(
    cycles: list[dict[str, object]], resets: list[bool],
) -> None:
    expected = _dif_sdf_frame_oracle(_fft8_fixture())
    result = compile_source(SOURCE.read_text(), top=FFT8_TOP, include_clash=False)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        direct_artifact = emit_sv_artifact(result.ir)
        direct = root / "direct.sv"
        direct.write_text(direct_artifact.text)
        published = publish_companion_bundle(direct_artifact.companions, root)
        assert sorted(item.depth for item in direct_artifact.companions) == [1, 2, 4]
        assert len(published) == 3
        direct_records = _rtl_records(
            direct,
            direct=True,
            cycles=cycles,
            resets=resets,
            top_name=FFT8_TOP,
            lint_waivers=(),
        )

        clash_artifact = emit_clash_artifact(result.ir)
        clash = generate_verilog(
            clash_artifact.text,
            FFT8_TOP,
            root / "clash",
            find_clash_executable(),
            companions=clash_artifact.companions,
            public_wrapper=ClashPublicTopWrapper.build(result.ir),
        )
        clash_records = _rtl_records(
            clash,
            direct=False,
            cycles=cycles,
            resets=resets,
            top_name=FFT8_TOP,
            # Clash 1.11's romFile blackbox widens its Verilog array index to
            # the host Int width. Arithmetic and payload widths stay fatal.
            lint_waivers=("-Wno-WIDTHTRUNC",),
        )

    assert direct_records == clash_records
    transfers = [
        {"re": record[3], "im": record[4]}
        for record in direct_records
        if not resets[record[0]]
        and record[2]
        and cycles[record[0]]["output"]["ready"]
    ]
    assert tuple(transfers) == expected

    stalled = [
        (record[3], record[4])
        for record in direct_records
        if record[2] and not cycles[record[0]]["output"]["ready"]
    ]
    assert len(stalled) >= 2
    assert len(set(stalled)) == 1


@pytest.mark.skipif(
    shutil.which("verilator") is None or find_clash_executable() is None,
    reason="Clash and Verilator are required for FFT16 RTL parity",
)
@pytest.mark.parametrize(
    ("cycles", "resets"),
    (_fft16_cycles(), _fft16_reset_gap_cycles()),
    ids=("continuous-stall", "gaps-midstream-reset"),
)
def test_fft16_reference_direct_sv_and_clash_match_staged_oracle(
    cycles: list[dict[str, object]], resets: list[bool],
) -> None:
    expected = _dif_sdf_frame_oracle(_fft16_fixture())
    result = compile_source(SOURCE.read_text(), top=FFT16_TOP, include_clash=False)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        direct_artifact = emit_sv_artifact(result.ir)
        direct = root / "direct.sv"
        direct.write_text(direct_artifact.text)
        published = publish_companion_bundle(direct_artifact.companions, root)
        assert sorted(item.depth for item in direct_artifact.companions) == [1, 2, 4, 8]
        assert len(published) == 4
        direct_records = _rtl_records(
            direct,
            direct=True,
            cycles=cycles,
            resets=resets,
            top_name=FFT16_TOP,
            lint_waivers=(),
        )

        clash_artifact = emit_clash_artifact(result.ir)
        clash = generate_verilog(
            clash_artifact.text,
            FFT16_TOP,
            root / "clash",
            find_clash_executable(),
            companions=clash_artifact.companions,
            public_wrapper=ClashPublicTopWrapper.build(result.ir),
        )
        clash_records = _rtl_records(
            clash,
            direct=False,
            cycles=cycles,
            resets=resets,
            top_name=FFT16_TOP,
            # The only waiver is Clash 1.11 romFile's host-width array index.
            lint_waivers=("-Wno-WIDTHTRUNC",),
        )

    assert direct_records == clash_records
    transfers = [
        {"re": record[3], "im": record[4]}
        for record in direct_records
        if not resets[record[0]]
        and record[2]
        and cycles[record[0]]["output"]["ready"]
    ]
    assert tuple(transfers) == expected

    stalled = [
        (record[3], record[4])
        for record in direct_records
        if record[2] and not cycles[record[0]]["output"]["ready"]
    ]
    assert len(stalled) >= 3
    assert len(set(stalled)) == 1


@pytest.mark.skipif(
    shutil.which("verilator") is None or find_clash_executable() is None,
    reason="Clash and Verilator are required for FFT32 RTL parity",
)
@pytest.mark.parametrize(
    ("cycles", "resets"),
    (_fft32_cycles(), _fft32_reset_gap_cycles()),
    ids=("continuous-stall", "gaps-midstream-reset"),
)
def test_fft32_reference_direct_sv_and_clash_match_staged_oracle(
    cycles: list[dict[str, object]], resets: list[bool],
) -> None:
    expected = _dif_sdf_frame_oracle(_fft32_fixture())
    result = compile_source(SOURCE.read_text(), top=FFT32_TOP, include_clash=False)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        direct_artifact = emit_sv_artifact(result.ir)
        direct = root / "direct.sv"
        direct.write_text(direct_artifact.text)
        published = publish_companion_bundle(direct_artifact.companions, root)
        assert sorted(item.depth for item in direct_artifact.companions) == [
            1, 2, 4, 8, 16,
        ]
        assert len(published) == 5
        direct_records = _rtl_records(
            direct,
            direct=True,
            cycles=cycles,
            resets=resets,
            top_name=FFT32_TOP,
            lint_waivers=(),
        )

        clash_artifact = emit_clash_artifact(result.ir)
        clash = generate_verilog(
            clash_artifact.text,
            FFT32_TOP,
            root / "clash",
            find_clash_executable(),
            companions=clash_artifact.companions,
            public_wrapper=ClashPublicTopWrapper.build(result.ir),
        )
        clash_records = _rtl_records(
            clash,
            direct=False,
            cycles=cycles,
            resets=resets,
            top_name=FFT32_TOP,
            lint_waivers=("-Wno-WIDTHTRUNC",),
        )

    assert direct_records == clash_records
    transfers = [
        {"re": record[3], "im": record[4]}
        for record in direct_records
        if not resets[record[0]]
        and record[2]
        and cycles[record[0]]["output"]["ready"]
    ]
    assert tuple(transfers) == expected

    stalled = [
        (record[3], record[4])
        for record in direct_records
        if record[2] and not cycles[record[0]]["output"]["ready"]
    ]
    assert len(stalled) >= 4
    assert len(set(stalled)) == 1
