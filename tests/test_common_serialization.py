"""Shared representation helpers keep cache/artifact identities consistent."""

from zlang.common import stable_digest, stable_json


def test_stable_json_is_order_independent() -> None:
    assert stable_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    assert stable_json({"a": 1, "b": 2}) == stable_json({"b": 2, "a": 1})


def test_stable_digest_preserves_text_identity_and_structured_identity() -> None:
    assert stable_digest("candidate") == stable_digest("candidate")
    assert stable_digest({"a": 1, "b": 2}) == stable_digest({"b": 2, "a": 1})
    assert stable_digest("candidate") != stable_digest({"value": "candidate"})
