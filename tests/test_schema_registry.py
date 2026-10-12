from __future__ import annotations

from zlang.schema_registry import (
    compiler_compatibility_identity,
    compiler_schema_registry,
)


def test_compiler_schema_registry_is_sorted_unique_and_stable() -> None:
    first = compiler_schema_registry()
    second = compiler_schema_registry()

    assert first == second
    assert tuple(item.owner for item in first) == tuple(
        sorted(item.owner for item in first)
    )
    assert len({item.owner for item in first}) == len(first)
    assert all(item.schema for item in first)
    assert compiler_compatibility_identity() == compiler_compatibility_identity()
