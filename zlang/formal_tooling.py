"""Lazy formal-tool discovery owned by one compiler session."""

from __future__ import annotations

from threading import RLock

from zlang.formal import FormalToolchainContext


class FormalToolResolver:
    """Thread-safe, lazy discovery cache for one compilation session."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._formal: dict[tuple[str, str], FormalToolchainContext] = {}

    def formal_context(
        self,
        *,
        engine: str = "sby",
        solver: str = "z3",
    ) -> FormalToolchainContext:
        key = (engine, solver)
        with self._lock:
            context = self._formal.get(key)
            if context is None:
                context = FormalToolchainContext.discover(
                    engine=engine,
                    solver=solver,
                )
                self._formal[key] = context
            return context

    @property
    def discovered_formal_routes(self) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple(sorted(self._formal))

__all__ = ["FormalToolResolver"]
