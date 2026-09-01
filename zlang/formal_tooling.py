"""Lazy external-tool discovery owned by one compiler session.

Creating a :class:`FormalToolResolver` performs no host inspection.  The first
consumer of one exact formal or Clash route records an immutable snapshot;
later candidates in the same compilation reuse it.  This keeps ordinary
compilation independent of installed formal tools and prevents unrelated
solvers from entering a selected route's identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from threading import RLock

from zlang.formal import FormalToolchainContext
from zlang.toolchain import (
    clash_subprocess_environment,
    find_clash_executable,
)


@dataclass(frozen=True)
class ClashToolContext:
    """One immutable Clash executable/version snapshot."""

    executable: str | None
    version: str | None

    @property
    def recipe_data(self) -> dict[str, object]:
        return {
            "executable": self.executable,
            "version": self.version,
        }


def _discover_clash_context() -> ClashToolContext:
    executable = find_clash_executable()
    if executable is None:
        return ClashToolContext(None, None)
    resolved = str(Path(executable).resolve())
    try:
        completed = subprocess.run(
            (executable, "--version"),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=clash_subprocess_environment(executable),
        )
    except (OSError, subprocess.SubprocessError) as error:
        version = f"unavailable:{type(error).__name__}"
    else:
        output = (completed.stdout or completed.stderr).strip()
        version = f"exit={completed.returncode}:{output}"
    return ClashToolContext(resolved, version)


class FormalToolResolver:
    """Thread-safe, lazy discovery cache for one compilation session."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._formal: dict[tuple[str, str], FormalToolchainContext] = {}
        self._clash: ClashToolContext | None = None

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

    def clash_context(self) -> ClashToolContext:
        with self._lock:
            if self._clash is None:
                self._clash = _discover_clash_context()
            return self._clash

    @property
    def discovered_formal_routes(self) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple(sorted(self._formal))

    @property
    def clash_was_discovered(self) -> bool:
        with self._lock:
            return self._clash is not None


__all__ = ["ClashToolContext", "FormalToolResolver"]
