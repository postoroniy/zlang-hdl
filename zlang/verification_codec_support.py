# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Strict shared primitives for verification bundle codecs and records."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
import re

from zlang import source
from zlang.ir import cdc as ir_cdc


HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
IDENTITY_PATTERN = re.compile(r"(?:[a-z][a-z0-9_.-]*:)?[0-9a-f]{64}")
TOKEN_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
KIND_PATTERN = re.compile(r"[a-z][a-z0-9_]*")


class VerificationBundleError(ValueError):
    """An immutable verification bundle is malformed or inconsistent."""


def require_exact_keys(
    data: Mapping[str, object], *, required: Iterable[str], description: str
) -> None:
    required_set = set(required)
    actual = set(data)
    missing = sorted(required_set - actual)
    unknown = sorted(actual - required_set)
    if missing:
        raise VerificationBundleError(
            f"{description} is missing field '{missing[0]}'"
        )
    if unknown:
        raise VerificationBundleError(
            f"{description} contains unknown field '{unknown[0]}'"
        )


def require_string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise VerificationBundleError(f"{description} must be a non-empty string")
    if any(ord(character) < 32 for character in value):
        raise VerificationBundleError(
            f"{description} must not contain control characters"
        )
    return value


def optional_string_field(
    data: Mapping[str, object], name: str, description: str
) -> str | None:
    value = data.get(name)
    if value is not None and not isinstance(value, str):
        raise VerificationBundleError(f"{description} {name} must be a string")
    return value


def string_tuple_field(
    data: Mapping[str, object], name: str, description: str
) -> tuple[str, ...]:
    value = data.get(name, [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise VerificationBundleError(
            f"{description} {name} must be an array of strings"
        )
    return tuple(value)


def require_integer(value: object, description: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise VerificationBundleError(
            f"{description} must be an integer greater than or equal to {minimum}"
        )
    return value


def require_json_value(value: object, description: str) -> None:
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, list):
        for item in value:
            require_json_value(item, description)
        return
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise VerificationBundleError(f"{description} keys must be strings")
        for item in value.values():
            require_json_value(item, description)
        return
    raise VerificationBundleError(f"{description} must contain only JSON values")


def validate_identity(value: object, description: str) -> str:
    identity = require_string(value, description)
    if IDENTITY_PATTERN.fullmatch(identity) is None:
        raise VerificationBundleError(
            f"{description} must be a lowercase SHA-256 identity"
        )
    return identity


def validate_property_id(value: object) -> str:
    property_id = require_string(value, "verification property ID")
    if len(property_id) > 512 or any(character.isspace() for character in property_id):
        raise VerificationBundleError(
            "verification property ID must be one bounded token"
        )
    return property_id


def validate_relative_path(value: object, *, prefix: str | None = None) -> str:
    path = require_string(value, "verification bundle path")
    if "\\" in path:
        raise VerificationBundleError(
            f"verification bundle path '{path}' must use '/' separators"
        )
    logical = PurePosixPath(path)
    if (
        logical.is_absolute()
        or path.endswith("/")
        or any(part in {"", ".", ".."} for part in logical.parts)
        or logical.as_posix() != path
    ):
        raise VerificationBundleError(f"unsafe verification bundle path '{path}'")
    if prefix is not None and not path.startswith(prefix):
        raise VerificationBundleError(
            f"verification bundle path '{path}' must be below '{prefix}'"
        )
    return path


def origin_from_data(value: object) -> source.SourceOrigin | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise VerificationBundleError("verification source origin must be an object")
    try:
        return source.SourceOrigin.from_data(value)
    except ValueError as error:
        raise VerificationBundleError(str(error)) from error


def clock_domain_from_job_data(value: object) -> ir_cdc.ClockDomain | None:
    try:
        return ir_cdc.clock_domain_from_data(value)
    except ValueError as error:
        raise VerificationBundleError(str(error)) from error
