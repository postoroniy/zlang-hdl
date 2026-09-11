from __future__ import annotations

from copy import deepcopy
import json

import pytest

from zlang.equivalence_result_codec import (
    EQUIVALENCE_RESULT_CODEC_SCHEMA,
    EquivalenceResultCodecError,
    equivalence_result_from_data,
    equivalence_result_from_json,
    equivalence_result_to_data,
    equivalence_result_to_json,
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
        backend="direct_systemverilog",
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
    ),
)
def test_result_codecs_reject_corruption(
    decode, base, mutation, match: str
) -> None:
    value = deepcopy(base())
    mutation(value)
    with pytest.raises(EquivalenceResultCodecError, match=match):
        decode(value)


@pytest.mark.parametrize("decode", (equivalence_result_from_json,))
def test_result_codecs_reject_invalid_json(decode) -> None:
    with pytest.raises(EquivalenceResultCodecError, match="not valid JSON"):
        decode("{not-json")
