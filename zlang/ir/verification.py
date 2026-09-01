"""Backend-independent source verification overlay.

The historical :class:`Contract` records remain the compatibility surface for
M16 ``assume``/``guarantee`` declarations.  The first-class verification UX is
kept in a distinct overlay so adding a source goal cannot change production
hardware or the existing M36/M38/M39 identities.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path
from collections.abc import Iterable, Mapping

from zlang.common import stable_digest, stable_json
from zlang.ir.expressions import Expression
from zlang.ir.types import BitType
from zlang.source import SourceOrigin


class ContractKind(str, Enum):
    ASSUME = "assume"
    GUARANTEE = "guarantee"


@dataclass(frozen=True)
class Contract:
    kind: ContractKind
    name: str
    clock: str
    reset: str
    expression: Expression


class VerificationGoalKind(str, Enum):
    """The bounded source-goal families supported by the first UX slice."""

    ASSERT = "assert"
    ENSURE = "ensure"
    COVER = "cover"


@dataclass(frozen=True)
class VerificationRequirement:
    """One environment-owned precondition local to a verification scope."""

    semantic_id: str
    name: str
    expression: Expression
    source_origin: SourceOrigin | None = None

    def __post_init__(self) -> None:
        if not self.semantic_id:
            raise ValueError("verification requirement semantic ID must not be empty")
        if not self.name:
            raise ValueError("verification requirement name must not be empty")
        if self.expression.type != BitType():
            raise ValueError("verification requirement expression must have type bit")


@dataclass(frozen=True)
class VerificationGoal:
    """One named same-cycle safety or bounded-reachability goal."""

    semantic_id: str
    scope_id: str
    kind: VerificationGoalKind
    name: str
    expression: Expression
    source_origin: SourceOrigin | None = None

    def __post_init__(self) -> None:
        if not self.semantic_id:
            raise ValueError("verification goal semantic ID must not be empty")
        if not self.scope_id:
            raise ValueError("verification goal scope ID must not be empty")
        if not isinstance(self.kind, VerificationGoalKind):
            raise ValueError("verification goal kind must be VerificationGoalKind")
        if not self.name:
            raise ValueError("verification goal name must not be empty")
        if self.expression.type != BitType():
            raise ValueError("verification goal expression must have type bit")


@dataclass(frozen=True)
class VerificationScope:
    """One clock/reset sampling scope and its locally scoped requirements."""

    semantic_id: str
    name: str
    clock: str
    reset: str
    requirements: tuple[VerificationRequirement, ...] = ()
    goals: tuple[VerificationGoal, ...] = ()
    source_origin: SourceOrigin | None = None

    def __post_init__(self) -> None:
        if not self.semantic_id:
            raise ValueError("verification scope semantic ID must not be empty")
        if not self.name:
            raise ValueError("verification scope name must not be empty")
        if not self.clock or not self.reset:
            raise ValueError("verification scope requires a clock and reset")
        requirement_names = tuple(item.name for item in self.requirements)
        goal_names = tuple(item.name for item in self.goals)
        if len(requirement_names) != len(set(requirement_names)):
            raise ValueError("verification requirement names must be unique in a scope")
        if len(goal_names) != len(set(goal_names)):
            raise ValueError("verification goal names must be unique in a scope")
        if set(requirement_names) & set(goal_names):
            raise ValueError(
                "verification requirement and goal names must be unique in a scope"
            )
        if any(item.scope_id != self.semantic_id for item in self.goals):
            raise ValueError("verification goal references the wrong scope")
        if not self.requirements and not self.goals:
            raise ValueError("verification scope must not be empty")


def validate_verification_overlay(
    scopes: Iterable[object],
    *,
    label: str = "verification",
) -> None:
    """Validate identities shared by typed, canonical, and runtime overlays.

    The three layers intentionally use distinct record classes, but identity
    rules are part of the verification UX rather than any one representation.
    Keeping this structural validator here prevents a malformed typed overlay
    from reaching canonical lowering or simulation with weaker checks.
    """

    items = tuple(scopes)
    scope_ids = tuple(str(getattr(item, "semantic_id")) for item in items)
    scope_keys = tuple(
        (str(getattr(item, "name")), str(getattr(item, "clock")))
        for item in items
    )
    requirements = tuple(
        requirement
        for scope in items
        for requirement in tuple(getattr(scope, "requirements"))
    )
    goals = tuple(
        goal
        for scope in items
        for goal in tuple(getattr(scope, "goals"))
    )
    requirement_ids = tuple(
        str(getattr(item, "semantic_id")) for item in requirements
    )
    goal_ids = tuple(str(getattr(item, "semantic_id")) for item in goals)
    id_word = "" if label.startswith("canonical ") else " semantic"

    if len(scope_ids) != len(set(scope_ids)):
        raise ValueError(f"{label} scope{id_word} IDs must be unique")
    if len(scope_keys) != len(set(scope_keys)):
        raise ValueError(f"{label} scope name/domain pairs must be unique")
    if len(requirement_ids) != len(set(requirement_ids)):
        raise ValueError(f"{label} requirement{id_word} IDs must be unique")
    if len(goal_ids) != len(set(goal_ids)):
        raise ValueError(f"{label} goal{id_word} IDs must be unique")
    if set(requirement_ids) & set(goal_ids):
        raise ValueError(f"{label} clause{id_word} IDs must be globally unique")
    for scope in items:
        scope_id = str(getattr(scope, "semantic_id"))
        for goal in tuple(getattr(scope, "goals")):
            if str(getattr(goal, "scope_id")) != scope_id:
                raise ValueError(f"{label} goal references the wrong scope")


def verification_identity(scopes: tuple[VerificationScope, ...]) -> str:
    """Return an origin-insensitive identity for the verification overlay."""

    from hashlib import sha256

    def expression_key(expression: Expression) -> str:
        # Typed expressions exclude source origins from dataclass equality but
        # repr may retain them.  The canonical layer provides the authoritative
        # expression identity; this semantic helper is deliberately used only
        # for source/bundle partitioning before canonicalization.
        from dataclasses import fields, is_dataclass

        def clean(value: object) -> object:
            if isinstance(value, SourceOrigin):
                return None
            if isinstance(value, Enum):
                return (type(value).__qualname__, value.value)
            if is_dataclass(value) and not isinstance(value, type):
                return (
                    type(value).__module__ + "." + type(value).__qualname__,
                    tuple(
                        (item.name, clean(getattr(value, item.name)))
                        for item in fields(value)
                        if item.name not in {"origin", "source_origin"}
                    ),
                )
            if isinstance(value, tuple):
                return tuple(clean(item) for item in value)
            return value

        return repr(clean(expression))

    payload = tuple(
        (
            scope.semantic_id,
            scope.name,
            scope.clock,
            scope.reset,
            tuple(
                (item.semantic_id, item.name, expression_key(item.expression))
                for item in scope.requirements
            ),
            tuple(
                (
                    item.semantic_id,
                    item.kind.value,
                    item.name,
                    expression_key(item.expression),
                )
                for item in scope.goals
            ),
        )
        for scope in scopes
    )
    return sha256(repr(("zlang-verification-overlay-v1", payload)).encode()).hexdigest()


def verification_module_identity(module: object) -> str:
    """Return the origin/overlay-independent semantic module identity.

    Goal IDs must not alias after the implementation changes, but adding or
    relocating verification declarations must leave existing IDs stable.  This
    helper deliberately fingerprints typed hardware rather than source text.
    """

    excluded = {
        "origin", "origins", "source_origin", "source_identity", "source_hash",
        "source_path", "verification_scopes",
    }

    def clean(value: object) -> object:
        if isinstance(value, SourceOrigin):
            return {"$source_origin": "omitted"}
        if isinstance(value, Enum):
            return {
                "$enum": f"{type(value).__module__}.{type(value).__qualname__}",
                "value": value.value,
            }
        if is_dataclass(value) and not isinstance(value, type):
            return {
                "$type": f"{type(value).__module__}.{type(value).__qualname__}",
                "fields": [
                    [item.name, clean(getattr(value, item.name))]
                    for item in fields(value)
                    if item.name not in excluded
                ],
            }
        if isinstance(value, Mapping):
            pairs = [(clean(key), clean(item)) for key, item in value.items()]
            pairs.sort(key=lambda pair: stable_json(pair[0]))
            return {"$mapping": pairs}
        if isinstance(value, (tuple, list)):
            return [clean(item) for item in value]
        if isinstance(value, (set, frozenset)):
            items = [clean(item) for item in value]
            items.sort(key=stable_json)
            return {"$set": items}
        if isinstance(value, Path):
            return {"$path": "omitted"}
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return {
            "$opaque": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": str(value),
        }

    return "verification-module:" + stable_digest({
        "schema": "zlang-verification-module-semantics-v1",
        "module": clean(module),
    })


__all__ = [
    "Contract",
    "ContractKind",
    "VerificationGoal",
    "VerificationGoalKind",
    "VerificationRequirement",
    "VerificationScope",
    "validate_verification_overlay",
    "verification_identity",
    "verification_module_identity",
]
