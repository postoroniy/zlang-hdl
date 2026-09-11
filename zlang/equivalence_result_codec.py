"""Strict, lossless JSON codecs for M36 semantic-reference results.

The equivalence result IR deliberately remains owned by :mod:`zlang.ir`.  This
module only supplies a versioned persistence boundary for compiler-owned proof
caches and reports.
"""

from __future__ import annotations

import json
from typing import Mapping

from zlang.common import stable_json
from zlang.ir.equivalence import (
    EquivalenceCounterexample,
    EquivalenceMode,
    EquivalenceRelation,
    EquivalenceResult,
    EquivalenceStatus,
)
from zlang.source import SourceOrigin, SourceSpan


EQUIVALENCE_RESULT_CODEC_SCHEMA = 1


class EquivalenceResultCodecError(ValueError):
    """A serialized M36 result is malformed or internally inconsistent."""


def _mapping(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise EquivalenceResultCodecError(f"{description} must be a JSON object")
    return value  # type: ignore[return-value]


def _exact_keys(
    value: Mapping[str, object], expected: set[str], description: str
) -> None:
    actual = set(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    details: list[str] = []
    if missing:
        details.append("missing " + ", ".join(missing))
    if extra:
        details.append("unexpected " + ", ".join(extra))
    raise EquivalenceResultCodecError(
        f"{description} has " + "; ".join(details)
    )


def _string(value: object, description: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        qualifier = "non-empty " if nonempty else ""
        raise EquivalenceResultCodecError(
            f"{description} must be a {qualifier}string"
        )
    return value


def _optional_string(value: object, description: str) -> str | None:
    if value is None:
        return None
    return _string(value, description)


def _integer(value: object, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EquivalenceResultCodecError(f"{description} must be an integer")
    return value


def _optional_integer(value: object, description: str) -> int | None:
    if value is None:
        return None
    return _integer(value, description)


def _enum(enum_type: type, value: object, description: str):
    text = _string(value, description, nonempty=True)
    try:
        return enum_type(text)
    except ValueError as error:
        raise EquivalenceResultCodecError(
            f"unsupported {description} '{text}'"
        ) from error


def _origin_to_data(origin: SourceOrigin | None) -> dict[str, object] | None:
    return None if origin is None else origin.to_data()


def _origin_from_data(value: object, description: str) -> SourceOrigin | None:
    if value is None:
        return None
    data = _mapping(value, description)
    _exact_keys(
        data,
        {"construct", "digest", "source_unit", "span"},
        description,
    )
    span_data = _mapping(data["span"], f"{description} span")
    _exact_keys(
        span_data,
        {"start_line", "start_column", "end_line", "end_column"},
        f"{description} span",
    )
    coordinates = tuple(
        _integer(span_data[name], f"{description} span {name}")
        for name in ("start_line", "start_column", "end_line", "end_column")
    )
    construct = _string(data["construct"], f"{description} construct", nonempty=True)
    source_unit = _optional_string(data["source_unit"], f"{description} source unit")
    digest = _optional_string(data["digest"], f"{description} digest")
    try:
        return SourceOrigin(
            SourceSpan(*coordinates), construct, source_unit, digest
        )
    except ValueError as error:
        raise EquivalenceResultCodecError(str(error)) from error


def _values_to_data(values: tuple[tuple[str, str], ...]) -> list[list[str]]:
    return [[name, value] for name, value in values]


def _values_from_data(value: object, description: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise EquivalenceResultCodecError(f"{description} must be an array")
    result: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, list) or len(item) != 2:
            raise EquivalenceResultCodecError(
                f"{description}[{index}] must be a two-element string array"
            )
        result.append(
            (
                _string(item[0], f"{description}[{index}] name"),
                _string(item[1], f"{description}[{index}] value"),
            )
        )
    return tuple(result)


def _equivalence_counterexample_to_data(
    value: EquivalenceCounterexample | None,
) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "property_id": value.property_id,
        "failure_cycle": value.failure_cycle,
        "sample_cycle": value.sample_cycle,
        "values": _values_to_data(value.values),
        "raw_trace": value.raw_trace,
    }


def _equivalence_counterexample_from_data(
    value: object, *, property_id: str
) -> EquivalenceCounterexample | None:
    if value is None:
        return None
    data = _mapping(value, "M36 counterexample")
    _exact_keys(
        data,
        {"property_id", "failure_cycle", "sample_cycle", "values", "raw_trace"},
        "M36 counterexample",
    )
    counterexample = EquivalenceCounterexample(
        _string(data["property_id"], "M36 counterexample property identity", nonempty=True),
        _optional_integer(data["failure_cycle"], "M36 counterexample failure cycle"),
        _optional_integer(data["sample_cycle"], "M36 counterexample sample cycle"),
        _values_from_data(data["values"], "M36 counterexample values"),
        _optional_string(data["raw_trace"], "M36 counterexample raw trace"),
    )
    if counterexample.property_id != property_id:
        raise EquivalenceResultCodecError(
            "M36 counterexample property identity differs from its result"
        )
    return counterexample


_M36_FIELDS = {
    "schema_version",
    "kind",
    "property_id",
    "status",
    "mode",
    "engine",
    "solver",
    "depth",
    "relation_kind",
    "latency_delta",
    "backend",
    "reference_hash",
    "implementation_hash",
    "binding_map_version",
    "candidate_identity",
    "source_origin",
    "selected_origin",
    "counterexample",
    "reason",
}


def equivalence_result_to_data(result: EquivalenceResult) -> dict[str, object]:
    """Encode one typed M36 result into the strict version-1 data schema."""

    if not isinstance(result, EquivalenceResult):
        raise TypeError("M36 result codec requires EquivalenceResult")
    return {
        "schema_version": EQUIVALENCE_RESULT_CODEC_SCHEMA,
        "kind": "m36_equivalence_result",
        "property_id": result.property_id,
        "status": result.status.value,
        "mode": result.mode.value,
        "engine": result.engine,
        "solver": result.solver,
        "depth": result.depth,
        "relation_kind": result.relation_kind.value,
        "latency_delta": result.latency_delta,
        "backend": result.backend,
        "reference_hash": result.reference_hash,
        "implementation_hash": result.implementation_hash,
        "binding_map_version": result.binding_map_version,
        "candidate_identity": result.candidate_identity,
        "source_origin": _origin_to_data(result.source_origin),
        "selected_origin": _origin_to_data(result.selected_origin),
        "counterexample": _equivalence_counterexample_to_data(result.counterexample),
        "reason": result.reason,
    }


def equivalence_result_from_data(value: object) -> EquivalenceResult:
    """Decode and validate one strict version-1 M36 result mapping."""

    data = _mapping(value, "M36 result")
    _exact_keys(data, _M36_FIELDS, "M36 result")
    schema = _integer(data["schema_version"], "M36 result schema version")
    if schema != EQUIVALENCE_RESULT_CODEC_SCHEMA:
        raise EquivalenceResultCodecError(
            f"unsupported M36 result codec schema {schema}"
        )
    if data["kind"] != "m36_equivalence_result":
        raise EquivalenceResultCodecError("M36 result has the wrong kind")
    property_id = _string(data["property_id"], "M36 property identity", nonempty=True)
    try:
        return EquivalenceResult(
            property_id,
            _enum(EquivalenceStatus, data["status"], "M36 status"),
            _enum(EquivalenceMode, data["mode"], "M36 mode"),
            _optional_string(data["engine"], "M36 engine"),
            _optional_string(data["solver"], "M36 solver"),
            _optional_integer(data["depth"], "M36 depth"),
            _enum(EquivalenceRelation, data["relation_kind"], "M36 relation"),
            _integer(data["latency_delta"], "M36 latency delta"),
            _string(data["backend"], "M36 backend", nonempty=True),
            _string(data["reference_hash"], "M36 reference hash"),
            _string(data["implementation_hash"], "M36 implementation hash"),
            _integer(data["binding_map_version"], "M36 binding-map version"),
            _string(data["candidate_identity"], "M36 candidate identity", nonempty=True),
            _origin_from_data(data["source_origin"], "M36 source origin"),
            _origin_from_data(data["selected_origin"], "M36 selected origin"),
            _equivalence_counterexample_from_data(
                data["counterexample"], property_id=property_id
            ),
            _optional_string(data["reason"], "M36 reason"),
        )
    except ValueError as error:
        if isinstance(error, EquivalenceResultCodecError):
            raise
        raise EquivalenceResultCodecError(str(error)) from error


def equivalence_result_to_json(result: EquivalenceResult) -> str:
    return stable_json(equivalence_result_to_data(result), indent=2) + "\n"


def equivalence_result_from_json(text: str) -> EquivalenceResult:
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as error:
        raise EquivalenceResultCodecError("M36 result is not valid JSON") from error
    return equivalence_result_from_data(value)




__all__ = [
    "EQUIVALENCE_RESULT_CODEC_SCHEMA",
    "EquivalenceResultCodecError",
    "equivalence_result_from_data",
    "equivalence_result_from_json",
    "equivalence_result_to_data",
    "equivalence_result_to_json",
]
