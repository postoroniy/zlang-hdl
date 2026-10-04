# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Bounded mutable ownership for one direct-SystemVerilog emission."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

from zlang.backend.systemverilog.functional.model import FunctionalExpressionContext


@dataclass
class FunctionalEmissionContext:
    """Mutable state owned only for the lifetime of one design emission."""

    scatter_helpers: dict[str, str] = field(default_factory=dict)
    expression: FunctionalExpressionContext | None = None


@dataclass
class EmissionContext:
    """Explicit root for bounded state shared by direct-SV sub-emitters."""

    module_name: str
    top_boundary: object | None = None
    functional: FunctionalEmissionContext = field(
        default_factory=FunctionalEmissionContext
    )


_CURRENT_EMISSION: ContextVar[EmissionContext | None] = ContextVar(
    "zlang_systemverilog_emission_context",
    default=None,
)


@contextmanager
def emission_scope(module_name: str) -> Iterator[EmissionContext]:
    """Install one design-local context and discard it after rendering."""

    context = EmissionContext(module_name)
    token = _CURRENT_EMISSION.set(context)
    try:
        yield context
    finally:
        _CURRENT_EMISSION.reset(token)


def current_emission_context() -> EmissionContext | None:
    return _CURRENT_EMISSION.get()


@contextmanager
def top_boundary_scope(boundary: object | None) -> Iterator[None]:
    """Temporarily install the selected top's validated physical boundary."""

    emission = _CURRENT_EMISSION.get()
    if emission is None:
        raise RuntimeError("top-boundary rendering requires an active emission context")
    previous = emission.top_boundary
    emission.top_boundary = boundary
    try:
        yield
    finally:
        emission.top_boundary = previous


def current_top_boundary() -> object | None:
    emission = _CURRENT_EMISSION.get()
    return None if emission is None else emission.top_boundary


@contextmanager
def functional_expression_scope(
    expression: FunctionalExpressionContext,
) -> Iterator[None]:
    """Temporarily install one lexical functional-expression environment."""

    emission = _CURRENT_EMISSION.get()
    if emission is None:
        raise RuntimeError(
            "functional expression rendering requires an active emission context"
        )
    previous = emission.functional.expression
    emission.functional.expression = expression
    try:
        yield
    finally:
        emission.functional.expression = previous


def current_functional_expression() -> FunctionalExpressionContext | None:
    emission = _CURRENT_EMISSION.get()
    return None if emission is None else emission.functional.expression


__all__ = [
    "EmissionContext",
    "FunctionalEmissionContext",
    "current_emission_context",
    "current_functional_expression",
    "current_top_boundary",
    "emission_scope",
    "functional_expression_scope",
    "top_boundary_scope",
]
