"""Canonical serialization used for cache keys and artifact identities."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeVar


class CanonicalSerializationError(ValueError):
    """A value cannot participate in a compiler-owned canonical identity."""


_Enum = TypeVar("_Enum", bound=Enum)


@dataclass(frozen=True)
class ObjectReader:
    """Small path-aware JSON-object decoder shared by strict codecs.

    Domain codecs still declare their own fields and invariants. This helper
    owns only repetitive JSON shape checks and translates failures to the
    codec's public exception type.
    """

    value: object
    label: str = "value"
    error_type: type[Exception] = ValueError

    def error(self, message: str) -> Exception:
        return self.error_type(f"{self.label} {message}")

    def mapping(self) -> Mapping[str, object]:
        if not isinstance(self.value, Mapping) or any(
            not isinstance(key, str) for key in self.value
        ):
            raise self.error("must be a JSON object")
        return self.value  # type: ignore[return-value]

    def exact_keys(self, expected: Sequence[str] | set[str]) -> Mapping[str, object]:
        mapping = self.mapping()
        expected_set = set(expected)
        actual = set(mapping)
        if actual != expected_set:
            missing = sorted(expected_set - actual)
            extra = sorted(actual - expected_set)
            details: list[str] = []
            if missing:
                details.append("is missing field(s): " + ", ".join(missing))
            if extra:
                details.append("has unknown field(s): " + ", ".join(extra))
            raise self.error("; ".join(details))
        return mapping

    def child(self, value: object, component: str) -> "ObjectReader":
        separator = "" if component.startswith("[") else " "
        return ObjectReader(
            value,
            f"{self.label}{separator}{component}",
            self.error_type,
        )

    def field(self, name: str) -> "ObjectReader":
        mapping = self.mapping()
        if name not in mapping:
            raise self.error(f"is missing field '{name}'")
        return self.child(mapping[name], name)

    def string(self, *, nonempty: bool = False) -> str:
        if not isinstance(self.value, str) or (nonempty and not self.value):
            qualifier = "non-empty " if nonempty else ""
            raise self.error(f"must be a {qualifier}string")
        return self.value

    def optional_string(self, *, nonempty: bool = False) -> str | None:
        if self.value is None:
            return None
        return self.string(nonempty=nonempty)

    def integer(
        self,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise self.error("must be an integer")
        if minimum is not None and self.value < minimum:
            raise self.error(f"must be at least {minimum}")
        if maximum is not None and self.value > maximum:
            raise self.error(f"must be at most {maximum}")
        return self.value

    def optional_integer(
        self,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int | None:
        if self.value is None:
            return None
        return self.integer(minimum=minimum, maximum=maximum)

    def array(self) -> list[object]:
        if not isinstance(self.value, list):
            raise self.error("must be an array")
        return self.value

    def enum(self, enum_type: type[_Enum]) -> _Enum:
        text = self.string(nonempty=True)
        try:
            return enum_type(text)
        except ValueError as error:
            raise self.error(f"has unsupported value '{text}'") from error


def _validate_canonical_value(value: object, path: str = "payload") -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalSerializationError(f"{path} contains a non-finite float")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_canonical_value(item, f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalSerializationError(
                    f"{path} contains a non-string object key"
                )
            _validate_canonical_value(item, f"{path}.{key}")
        return
    raise CanonicalSerializationError(
        f"{path} contains unsupported {type(value).__module__}."
        f"{type(value).__qualname__}"
    )


def stable_json(value: Any, *, indent: int | None = None) -> str:
    """Serialize values with deterministic keys and fallback scalar text."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":") if indent is None else None,
        indent=indent,
        default=str,
    )


def stable_json_bytes(value: Any) -> bytes:
    return stable_json(value).encode("utf-8")


def stable_pretty_json(value: Any) -> str:
    """Serialize one deterministic, human-readable JSON document.

    Artifact codecs retain ownership of their schemas and payload construction;
    this helper owns only the shared formatting contract, including the final
    newline expected by files written by the compiler.
    """

    return stable_json(value, indent=2) + "\n"


def stable_digest(value: Any, *, length: int | None = None) -> str:
    # Preserve the established text-identity behavior used by candidate
    # hashes; structured values use the shared canonical JSON form.
    payload = value.encode("utf-8") if isinstance(value, str) else stable_json_bytes(value)
    digest = hashlib.sha256(payload).hexdigest()
    return digest if length is None else digest[:length]


def canonical_identity(
    schema: str,
    payload: object,
    *,
    length: int | None = None,
) -> str:
    """Hash one explicitly versioned JSON value without fallback coercion."""

    if not isinstance(schema, str) or not schema:
        raise CanonicalSerializationError("identity schema must be non-empty")
    _validate_canonical_value(payload)
    return stable_digest(
        {"payload": payload, "schema": schema},
        length=length,
    )


__all__ = [
    "CanonicalSerializationError",
    "ObjectReader",
    "canonical_identity",
    "stable_digest",
    "stable_json",
    "stable_json_bytes",
]
