"""Immutable, dependency-free external-tool discovery records."""

from __future__ import annotations

from dataclasses import dataclass
import shutil
import subprocess
from typing import Callable, Iterable


@dataclass(frozen=True)
class ToolInventory:
    """One deterministic snapshot of requested tool availability and versions."""

    requested: tuple[str, ...]
    available: tuple[str, ...]
    versions: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if any(not name for name in self.requested):
            raise ValueError("tool inventory names must not be empty")
        if len(set(self.requested)) != len(self.requested):
            raise ValueError("tool inventory names must be unique")
        if len(set(self.available)) != len(self.available):
            raise ValueError("available tool names must be unique")
        requested = set(self.requested)
        if any(name not in requested for name in self.available):
            raise ValueError("available tools must belong to the requested inventory")
        expected_available = tuple(
            name for name in self.requested if name in set(self.available)
        )
        if self.available != expected_available:
            raise ValueError("available tools must retain requested order")
        versioned_names = tuple(name for name, _ in self.versions)
        if len(set(versioned_names)) != len(versioned_names):
            raise ValueError("versioned tool names must be unique")
        if any(name not in self.available for name, _ in self.versions):
            raise ValueError("versioned tools must be available")
        expected_versioned = tuple(
            name for name in self.available if name in set(versioned_names)
        )
        if versioned_names != expected_versioned:
            raise ValueError("versioned tools must retain available-tool order")

    @property
    def missing(self) -> tuple[str, ...]:
        available = set(self.available)
        return tuple(name for name in self.requested if name not in available)

    def has(self, name: str) -> bool:
        return name in self.available


def discover_tool_inventory(
    names: Iterable[str],
    *,
    version_commands: Iterable[tuple[str, tuple[str, ...]]] = (),
    which: Callable[[str], str | None] | None = None,
    runner: Callable[..., object] | None = None,
    version_timeout: int = 5,
    require_truthy_path: bool = False,
) -> ToolInventory:
    """Discover tools once while retaining caller-defined order and probes.

    ``require_truthy_path`` exists solely to preserve callers whose historical
    discovery used truth-value checks rather than ``is not None`` checks.
    """

    requested = tuple(names)
    if len(set(requested)) != len(requested):
        raise ValueError("tool inventory names must be unique")
    locate = shutil.which if which is None else which
    run = subprocess.run if runner is None else runner
    command_items = tuple(version_commands)
    command_names = tuple(name for name, _ in command_items)
    if len(set(command_names)) != len(command_names):
        raise ValueError("tool version commands must be unique")
    if any(name not in requested for name in command_names):
        raise ValueError("tool version commands must belong to the requested inventory")
    commands = dict(command_items)
    available: list[str] = []
    versions: list[tuple[str, str]] = []
    for name in requested:
        path = locate(name)
        found = bool(path) if require_truthy_path else path is not None
        if not found:
            continue
        available.append(name)
        command = commands.get(name)
        if command is None:
            continue
        try:
            completed = run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=version_timeout,
            )
            stdout = getattr(completed, "stdout", None)
            stderr = getattr(completed, "stderr", None)
            text = (stdout or stderr).strip().splitlines()
            versions.append((name, text[0] if text else "available"))
        except (OSError, subprocess.TimeoutExpired):
            versions.append((name, "available"))
    return ToolInventory(requested, tuple(available), tuple(versions))


__all__ = ["ToolInventory", "discover_tool_inventory"]
