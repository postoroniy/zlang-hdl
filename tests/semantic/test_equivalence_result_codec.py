from __future__ import annotations

from copy import deepcopy
import json

import pytest

from zlang.equivalence_result_codec import (
    EQUIVALENCE_RESULT_CODEC_SCHEMA,
    EquivalenceResultCodecError,
    cross_backend_result_from_data,
    cross_backend_result_from_json,
    cross_backend_result_to_data,
    cross_backend_result_to_json,
    equivalence_result_from_data,
    equivalence_result_from_json,
    equivalence_result_to_data,
    equivalence_result_to_json,
)
from zlang.ir.cross_backend import (
    CrossBackendCounterexample,
    CrossBackendMode,
    CrossBackendRelation,
    CrossBackendResult,
    CrossBackendStatus,
)
from zlang.ir.equivalence import (
    EquivalenceCounterexample,
    EquivalenceMode,
    EquivalenceRelation,
    EquivalenceResult,
    EquivalenceStatus,
)
from zlang.source import SourceOrigin, SourceSpan


SOURCE = SourceOrigin(
    SourceSpan(3, 5, 7, 11),
    "explore",
    "examples/codec.zhl",
    "a" * 64,
)
SELECTED = SourceOrigin(
    SourceSpan(8, 2, 8, 17),
    "selected pipeline",
    "examples/codec.zhl",
    "a" * 64,
)


def _m36() -> EquivalenceResult:
    return EquivalenceResult(
        property_id="m36.equiv.codec",
        status=EquivalenceStatus.FAILED,
        mode=EquivalenceMode.BMC,
        engine="sby",
        solver="z3",
        depth=12,
        relation_kind=EquivalenceRelation.FIXED_LATENCY_VALUE,
        latency_delta=3,
        backend="clash",
        reference_hash="b" * 64,
        implementation_hash="c" * 64,
        binding_map_version=2,
        candidate_identity="candidate.codec.pipeline3",
        source_origin=SOURCE,
        selected_origin=SELECTED,
        counterexample=EquivalenceCounterexample(
            "m36.equiv.codec",
            failure_cycle=9,
            sample_cycle=6,
            values=(
                ("reference_output", "-1 (0xff)"),
                ("implementation_output", "0 (0x00)"),
            ),
            raw_trace="$var wire 8 ! reference_output $end\n#9\n",
        ),
        reason="counterexample found",
    )


def _m38() -> CrossBackendResult:
    return CrossBackendResult(
        property_id="m38.equiv.codec",
        status=CrossBackendStatus.FAILED,
        mode=CrossBackendMode.PROVE,
        engine="sby",
        solver="z3",
        depth=16,
        relation=CrossBackendRelation.SAME_CYCLE_VALUE,
        latency_delta=0,
        selected_ir_identity="candidate.codec.pipeline3",
        left_backend="clash",
        right_backend="direct_systemverilog",
        left_artifact_hash="d" * 64,
        right_artifact_hash="e" * 64,
        manifest_version=10,
        observable_signal_id="port:y",
        source_origin=SOURCE,
        counterexample=CrossBackendCounterexample(
            property_id="m38.equiv.codec",
            semantic_signal_id="port:y",
            cycle=5,
            sample_cycle=5,
            left_backend="clash",
            right_backend="direct_systemverilog",
            left_artifact_hash="d" * 64,
            right_artifact_hash="e" * 64,
            left_rtl_path="CodecClash.y",
            right_rtl_path="CodecSv.y",
            values=(("left:port:y", "1"), ("right:port:y", "0")),
            raw_trace="$scope module m38 $end\n#5\n",
            source_origin=SELECTED,
        ),
        reason="backend outputs differ",
    )


def test_m36_result_codec_is_lossless_and_deterministic() -> None:
    result = _m36()
    data = equivalence_result_to_data(result)

    assert data["schema_version"] == EQUIVALENCE_RESULT_CODEC_SCHEMA
    assert data["kind"] == "m36_equivalence_result"
    assert equivalence_result_from_data(data) == result

    encoded = equivalence_result_to_json(result)
    assert encoded.endswith("\n")
    assert equivalence_result_from_json(encoded) == result
    assert equivalence_result_to_json(equivalence_result_from_json(encoded)) == encoded
    assert json.loads(encoded)["counterexample"]["raw_trace"].endswith("#9\n")


def test_m38_result_codec_is_lossless_and_deterministic() -> None:
    result = _m38()
    data = cross_backend_result_to_data(result)

    assert data["schema_version"] == EQUIVALENCE_RESULT_CODEC_SCHEMA
    assert data["kind"] == "m38_cross_backend_result"
    assert cross_backend_result_from_data(data) == result

    encoded = cross_backend_result_to_json(result)
    assert encoded.endswith("\n")
    assert cross_backend_result_from_json(encoded) == result
    assert cross_backend_result_to_json(cross_backend_result_from_json(encoded)) == encoded
    assert json.loads(encoded)["counterexample"]["source_origin"] == SELECTED.to_data()


def test_non_failure_results_preserve_nulls_and_empty_unavailable_hashes() -> None:
    m36 = EquivalenceResult(
        "m36.unavailable",
        EquivalenceStatus.SKIPPED,
        EquivalenceMode.BMC,
        None,
        None,
        None,
        EquivalenceRelation.SAME_CYCLE_VALUE,
        0,
        "clash",
        "",
        "",
        2,
        "candidate.unavailable",
        reason="route unavailable",
    )
    m38 = CrossBackendResult(
        "m38.unavailable",
        CrossBackendStatus.SKIPPED,
        CrossBackendMode.BMC,
        None,
        None,
        None,
        CrossBackendRelation.SAME_CYCLE_VALUE,
        0,
        "candidate.unavailable",
        "clash",
        "direct_systemverilog",
        "",
        "",
        10,
        reason="route unavailable",
    )

    assert equivalence_result_from_data(equivalence_result_to_data(m36)) == m36
    assert cross_backend_result_from_data(cross_backend_result_to_data(m38)) == m38


@pytest.mark.parametrize("depth", (None, 0, -1))
def test_m36_codec_rejects_decisive_results_without_positive_depth(
    depth: int | None,
) -> None:
    data = equivalence_result_to_data(_m36())
    data["depth"] = depth

    with pytest.raises(
        EquivalenceResultCodecError,
        match="decisive equivalence results require a positive depth",
    ):
        equivalence_result_from_data(data)


@pytest.mark.parametrize("depth", (None, 0, -1))
def test_m38_codec_rejects_decisive_results_without_positive_depth(
    depth: int | None,
) -> None:
    data = cross_backend_result_to_data(_m38())
    data["depth"] = depth

    with pytest.raises(
        EquivalenceResultCodecError,
        match="decisive cross-backend results require a positive depth",
    ):
        cross_backend_result_from_data(data)


@pytest.mark.parametrize(
    "decode,base,mutation,match",
    (
        (
            equivalence_result_from_data,
            lambda: equivalence_result_to_data(_m36()),
            lambda value: value.update({"unexpected": 1}),
            "unexpected unexpected",
        ),
        (
            equivalence_result_from_data,
            lambda: equivalence_result_to_data(_m36()),
            lambda value: value.pop("solver"),
            "missing solver",
        ),
        (
            equivalence_result_from_data,
            lambda: equivalence_result_to_data(_m36()),
            lambda value: value.update({"status": "maybe"}),
            "unsupported M36 status",
        ),
        (
            equivalence_result_from_data,
            lambda: equivalence_result_to_data(_m36()),
            lambda value: value.update({"depth": True}),
            "M36 depth must be an integer",
        ),
        (
            equivalence_result_from_data,
            lambda: equivalence_result_to_data(_m36()),
            lambda value: value["source_origin"].update({"line": 3}),
            "unexpected line",
        ),
        (
            equivalence_result_from_data,
            lambda: equivalence_result_to_data(_m36()),
            lambda value: value["counterexample"].update({"property_id": "other"}),
            "counterexample property identity differs",
        ),
        (
            cross_backend_result_from_data,
            lambda: cross_backend_result_to_data(_m38()),
            lambda value: value.update({"kind": "m36_equivalence_result"}),
            "wrong kind",
        ),
        (
            cross_backend_result_from_data,
            lambda: cross_backend_result_to_data(_m38()),
            lambda value: value["counterexample"].pop("raw_trace"),
            "missing raw_trace",
        ),
        (
            cross_backend_result_from_data,
            lambda: cross_backend_result_to_data(_m38()),
            lambda value: value["counterexample"].update({"left_backend": "wrong"}),
            "metadata differs",
        ),
        (
            cross_backend_result_from_data,
            lambda: cross_backend_result_to_data(_m38()),
            lambda value: value.update({"counterexample": None}),
            "require exactly one counterexample",
        ),
    ),
)
def test_result_codecs_reject_corruption(
    decode, base, mutation, match: str
) -> None:
    value = deepcopy(base())
    mutation(value)
    with pytest.raises(EquivalenceResultCodecError, match=match):
        decode(value)


@pytest.mark.parametrize(
    "decode",
    (equivalence_result_from_json, cross_backend_result_from_json),
)
def test_result_codecs_reject_invalid_json(decode) -> None:
    with pytest.raises(EquivalenceResultCodecError, match="not valid JSON"):
        decode("{not-json")
