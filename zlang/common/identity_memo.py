"""Object-identity memoization for immutable compiler products.

The cached object is retained beside its result so reuse of a Python object ID
cannot return a value computed for an object that has already been collected.
"""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from typing import Generic, TypeVar


_Input = TypeVar("_Input")
_Output = TypeVar("_Output")


class IdentityMemo(Generic[_Input, _Output]):
    """Own or adapt one object-identity keyed memo table."""

    def __init__(
        self,
        storage: MutableMapping[int, tuple[_Input, _Output]] | None = None,
    ) -> None:
        self._storage = {} if storage is None else storage

    def get_or_compute(
        self,
        value: _Input,
        compute: Callable[[_Input], _Output],
    ) -> _Output:
        cached = self._storage.get(id(value))
        if cached is not None and cached[0] is value:
            return cached[1]
        result = compute(value)
        self._storage[id(value)] = (value, result)
        return result


__all__ = ["IdentityMemo"]
