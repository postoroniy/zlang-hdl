"""Shared graph primitives for deterministic fixed-pipeline scheduling."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from hashlib import sha256

from zlang.ir import expressions as expr
from zlang.ir.pipelines import ScheduledPipelineOperation


class PipelineSchedulingError(ValueError):
    """A fixed pipeline body cannot be represented by the bounded scheduler."""


def replace_direct_children(
    value: expr.Expression,
    original: tuple[expr.Expression, ...],
    rewritten: tuple[expr.Expression, ...],
) -> expr.Expression:
    """Rebuild one immutable expression while preserving untouched sharing."""

    if len(original) != len(rewritten):
        raise PipelineSchedulingError("expression child replacement is incomplete")
    replacements = {
        id(before): after
        for before, after in zip(original, rewritten, strict=True)
    }

    def walk(item: object, *, root: bool = False) -> object:
        if isinstance(item, expr.Expression) and not root:
            replacement = replacements.get(id(item))
            return replacement if replacement is not None else item
        if isinstance(item, tuple):
            return tuple(walk(child) for child in item)
        if is_dataclass(item) and not isinstance(item, type):
            updates: dict[str, object] = {}
            for descriptor in fields(item):
                if not descriptor.init or descriptor.name in {
                    "origin",
                    "source_origin",
                    "type",
                    "pipeline_plan",
                }:
                    continue
                current = getattr(item, descriptor.name)
                changed = walk(current)
                if changed is not current:
                    updates[descriptor.name] = changed
            return replace(item, **updates) if updates else item
        return item

    result = walk(value, root=True)
    if not isinstance(result, expr.Expression):
        raise PipelineSchedulingError("expression rewrite did not return typed IR")
    return result


def producer_stage(
    identity: str,
    operations: tuple[ScheduledPipelineOperation, ...],
    leaves: dict[str, str],
) -> int:
    """Return the scheduled stage of an operation or graph leaf."""

    operation = next((item for item in operations if item.identity == identity), None)
    if operation is not None:
        return operation.stage
    if identity in leaves.values():
        return 0
    raise PipelineSchedulingError("timing edge references an unknown producer")


def schedule_identity(*items: object) -> str:
    """Preserve the published fixed-pipeline identity byte contract."""

    return sha256(repr(items).encode("utf-8")).hexdigest()


__all__ = [
    "PipelineSchedulingError",
    "producer_stage",
    "replace_direct_children",
    "schedule_identity",
]
