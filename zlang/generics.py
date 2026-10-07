"""Shared compile-time generic identities and substitutions.

These objects never represent runtime hardware.  Module, struct, protocol,
function, and operator specialization can share this vocabulary while all
published hardware IR remains concrete.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
import hashlib

from zlang.ir.types import HardwareType


GENERIC_SPECIALIZATION_SCHEMA = "zlang-generic-specialization-v2"


@dataclass(frozen=True)
class GenericParameter:
    name: str
    kind: str
    default: int | str | None = None


@dataclass(frozen=True)
class GenericArgument:
    name: str
    kind: str
    # Compile-time aggregate constants and statically selected callables use
    # their canonical digest/identity spelling.  Runtime hardware values never
    # enter generic specialization identity.
    value: HardwareType | int | str

    @property
    def canonical(self) -> str:
        return str(self.value)


@dataclass(frozen=True)
class GenericSubstitution:
    arguments: tuple[GenericArgument, ...]

    def type_bindings(self) -> dict[str, HardwareType]:
        return {
            item.name: item.value
            for item in self.arguments
            if item.kind == "type"
        }  # type: ignore[return-value]

    def value_bindings(self) -> dict[str, int]:
        return {
            item.name: item.value
            for item in self.arguments
            if item.kind == "value"
        }  # type: ignore[return-value]


@dataclass(frozen=True)
class SpecializationIdentity:
    digest: str

    @classmethod
    def create(
        cls,
        *,
        declaration: object,
        owner: str,
        arguments: tuple[GenericArgument, ...],
        source_identity: str | None,
        dependency_identity: tuple[tuple[str, str], ...] = (),
    ) -> "SpecializationIdentity":
        def normalize(value: object) -> object:
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, tuple):
                return tuple(normalize(item) for item in value)
            if is_dataclass(value):
                return (
                    type(value).__name__,
                    tuple(
                        (item.name, normalize(getattr(value, item.name)))
                        for item in fields(value)
                        if item.name not in {"origin", "source_identity"}
                    ),
                )
            return value

        normalized = (
            GENERIC_SPECIALIZATION_SCHEMA,
            owner,
            source_identity,
            dependency_identity,
            tuple((item.name, item.kind, item.canonical) for item in arguments),
            normalize(declaration),
        )
        return cls(hashlib.sha256(repr(normalized).encode("utf-8")).hexdigest())
