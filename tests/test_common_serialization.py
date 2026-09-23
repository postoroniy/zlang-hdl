"""Shared representation helpers keep cache/artifact identities consistent."""

import pytest

from zlang.common import (
    CanonicalSerializationError,
    ObjectReader,
    canonical_identity,
    stable_digest,
    stable_json,
    stable_pretty_json,
    subprocess_text,
)


def test_stable_json_is_order_independent() -> None:
    assert stable_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    assert stable_json({"a": 1, "b": 2}) == stable_json({"b": 2, "a": 1})


def test_stable_pretty_json_owns_artifact_formatting_contract() -> None:
    assert stable_pretty_json({"b": 2, "a": 1}) == (
        '{\n  "a": 1,\n  "b": 2\n}\n'
    )


def test_stable_digest_preserves_text_identity_and_structured_identity() -> None:
    assert stable_digest("candidate") == stable_digest("candidate")
    assert stable_digest({"a": 1, "b": 2}) == stable_digest({"b": 2, "a": 1})
    assert stable_digest("candidate") != stable_digest({"value": "candidate"})


def test_object_reader_reports_nested_paths_with_domain_error() -> None:
    class CodecError(ValueError):
        pass

    reader = ObjectReader(
        {"name": "ok", "count": True}, "record", CodecError
    )
    data = reader.exact_keys({"name", "count"})
    assert reader.child(data["name"], "name").string(nonempty=True) == "ok"
    with pytest.raises(CodecError, match="record count must be an integer"):
        reader.child(data["count"], "count").integer()


def test_canonical_identity_rejects_fallback_object_coercion() -> None:
    assert canonical_identity("fixture-v1", {"value": 1}) == canonical_identity(
        "fixture-v1", {"value": 1}
    )
    assert canonical_identity("fixture-v1", {"value": 1}) != canonical_identity(
        "fixture-v2", {"value": 1}
    )
    with pytest.raises(CanonicalSerializationError, match="unsupported"):
        canonical_identity("fixture-v1", {"value": object()})


def test_subprocess_text_normalizes_timeout_stream_variants() -> None:
    assert subprocess_text(None) == ""
    assert subprocess_text("already text") == "already text"
    assert subprocess_text(b"partial\xff") == "partial\ufffd"
