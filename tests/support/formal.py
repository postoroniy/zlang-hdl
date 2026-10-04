"""Test-only formal tool availability probes."""

from __future__ import annotations

import shutil

from zlang.common.tool_inventory import discover_tool_inventory


def formal_tools_available() -> tuple[str, ...]:
    """Return the executable closure used by direct equivalence tests."""

    return discover_tool_inventory(
        ("yosys", "sby", "yosys-smtbmc"),
        which=shutil.which,
    ).available


__all__ = ["formal_tools_available"]
