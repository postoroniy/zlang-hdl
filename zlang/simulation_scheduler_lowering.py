# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Primitive scheduler selection lowering for native simulation plans."""

from __future__ import annotations

from typing import Any

from zlang.simulation_primitive_model import PrimitiveBuilder, PrimitiveLoweringError


def _condition(
    builder: PrimitiveBuilder,
    identifier: int | None,
) -> int:
    return (
        builder.constant(1, 1)
        if identifier is None
        else builder.truthy(builder.lower(identifier))
    )


def lower_scheduler_actions(
    payload: dict[str, Any],
    builder: PrimitiveBuilder,
    next_by_domain: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    """Lower the compiler-minimized scheduler truth table and its actions."""

    scheduler_counts = [
        builder.emit(
            "load_state",
            int(builder.semantic_fifos[name]["count_width"]),
            attributes={"name": builder.semantic_fifos[name]["count"]},
        )
        for name in payload.get("scheduler_fifos", [])
    ]
    transition_by_name = {item["name"]: item for item in payload["transitions"]}
    scheduler_guards = [
        builder.truthy(builder.lower(transition_by_name[name]["guard"]))
        for name in payload.get("scheduler_guards", [])
    ]
    scheduler_activations = [
        builder.truthy(builder.lower(identifier))
        for identifier in payload.get("scheduler_activations", [])
    ]
    scheduler_inputs = [
        *scheduler_counts,
        *scheduler_guards,
        *scheduler_activations,
    ]
    scheduler_fifo_depths = [
        int(builder.semantic_fifos[name]["depth"])
        for name in payload.get("scheduler_fifos", [])
    ]
    atom_cache: dict[tuple[int, object], int] = {}

    def atom(index: int, expected: object, actual: int) -> int:
        key = (index, expected)
        cached = atom_cache.get(key)
        if cached is not None:
            return cached
        if index < len(scheduler_counts):
            width = builder.width(actual)
            empty = builder.binary(
                "eq", actual, builder.constant(0, width), 1
            )
            full = builder.binary(
                "eq",
                actual,
                builder.constant(scheduler_fifo_depths[index], width),
                1,
            )
            if expected == "empty":
                result = empty
            elif expected == "full":
                result = full
            elif expected == "middle":
                result = builder.unary(
                    "not", builder.binary("or", empty, full, 1), 1
                )
            else:
                raise PrimitiveLoweringError(
                    "scheduler region has an invalid FIFO occupancy"
                )
        elif isinstance(expected, bool):
            result = (
                builder.truthy(actual)
                if expected
                else builder.unary("not", builder.truthy(actual), 1)
            )
        else:
            raise PrimitiveLoweringError(
                "scheduler region has an invalid boolean predicate"
            )
        atom_cache[key] = result
        return result

    storage_actions: list[dict[str, Any]] = []
    for group in payload["transitions"]:
        domain = group["domain"]
        if domain is None:
            continue
        fire = builder.constant(0, 1)
        for region in group["selection_regions"]:
            if len(region) != len(scheduler_inputs):
                raise PrimitiveLoweringError(
                    f"scheduler region for rule '{group['name']}' has invalid arity"
                )
            matches = builder.constant(1, 1)
            for index, (expected, actual) in enumerate(
                zip(region, scheduler_inputs, strict=True)
            ):
                if expected is not None:
                    matches = builder.binary(
                        "and", matches, atom(index, expected, actual), 1
                    )
            fire = builder.binary("or", fire, matches, 1)
        for action in group["actions"]:
            commit = builder.binary(
                "and", fire, _condition(builder, action["activation"]), 1
            )
            if action["kind"] != "register_write":
                storage_actions.append(
                    {**action, "domain": domain, "commit": commit}
                )
                continue
            target = action["target"]
            old = next_by_domain[domain][target]
            next_by_domain[domain][target] = builder.select(
                commit,
                builder.lower(action["node"]),
                old,
                builder.width(old),
            )
            # Retain the exact accepted-write predicate for per-bit U/X state
            # ownership. Storage-specific consumers ignore this action kind.
            storage_actions.append(
                {**action, "domain": domain, "commit": commit}
            )
    return storage_actions


__all__ = ["lower_scheduler_actions"]
