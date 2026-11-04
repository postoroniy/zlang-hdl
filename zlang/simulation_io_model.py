# SPDX-License-Identifier: Apache-2.0
"""Immutable syntax/typed facts for the bounded simulation I/O language.

The expression parameter separates parser AST from checked compiler expressions.
These facts own neither hardware state nor file handles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar

E = TypeVar("E")


@dataclass(frozen=True)
class IOWhen(Generic[E]):
    guard: E
    body: tuple[IOCommand[E], ...]
    origin: object | None = field(default=None, compare=False)


@dataclass(frozen=True)
class IORead(Generic[E]):
    channel: str
    bindings: tuple[tuple[str, E], ...]
    eof: tuple[IOCommand[E], ...]
    origin: object | None = field(default=None, compare=False)


@dataclass(frozen=True)
class IOWrite(Generic[E]):
    channel: str
    bindings: tuple[tuple[str, E], ...]
    origin: object | None = field(default=None, compare=False)


@dataclass(frozen=True)
class IODrive(Generic[E]):
    target: E
    value: E
    origin: object | None = field(default=None, compare=False)


@dataclass(frozen=True)
class IOFinish:
    origin: object | None = field(default=None, compare=False)


IOCommand = IOWhen[E] | IORead[E] | IOWrite[E] | IODrive[E] | IOFinish


@dataclass(frozen=True)
class SimulationBlock(Generic[E]):
    name: str
    clock: str
    commands: tuple[IOCommand[E], ...]
    origin: object | None = field(default=None, compare=False)
    reset_bindings: tuple[tuple[str, str], ...] = ()


def phase_masks(commands):
    """Classify command effects once; 1=drive/read, 2=observe/log."""
    result = {}

    def visit(items):
        mask = 0
        for item in items:
            if isinstance(item, IOWhen):
                value = visit(item.body)
                result[id(item)] = value
            elif isinstance(item, IORead):
                value = 1 | visit(item.eof)
            elif isinstance(item, IOWrite):
                value = 2
            else:
                value = 1
            mask |= value
        return mask

    visit(commands)
    return result
