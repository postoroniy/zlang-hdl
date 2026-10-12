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
    top_physical_abis: dict[int, tuple[object, object]] = field(default_factory=dict)
    rtl_name_plans: dict[tuple[int, tuple[str, ...]], tuple[object, object]] = field(
        default_factory=dict
    )
    # Materialization owns module-local physical expression rewrites and the
    # exact DAG plan.  Keep those owners here so validation and rendering of
    # the same module share one preparation without making the context know
    # their backend-specific implementation.
    materialization_owners: dict[
        tuple[int, int], tuple[object, object, object]
    ] = field(default_factory=dict)
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


def top_physical_abi(module: object) -> object:
    """Return one module's public ABI projection within the active emission."""

    emission = _CURRENT_EMISSION.get()
    if emission is None:
        from zlang.ir.top_abi import build_top_physical_abi

        return build_top_physical_abi(module)
    key = id(module)
    cached = emission.top_physical_abis.get(key)
    if cached is not None and cached[0] is module:
        return cached[1]
    from zlang.ir.top_abi import build_top_physical_abi

    abi = build_top_physical_abi(module)
    emission.top_physical_abis[key] = (module, abi)
    return abi


def cached_module_rtl_names(
    module: object, *, reserved: tuple[str, ...] = ()
) -> object:
    """Return one module's RTL names within the active emission."""

    emission = _CURRENT_EMISSION.get()
    if emission is None:
        from zlang.backend import naming

        return naming.module_rtl_names(module, reserved=reserved)
    key = (id(module), tuple(reserved))
    cached = emission.rtl_name_plans.get(key)
    if cached is not None and cached[0] is module:
        return cached[1]
    from zlang.backend import naming

    names = naming.module_rtl_names(module, reserved=reserved)
    emission.rtl_name_plans[key] = (module, names)
    return names


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
    "cached_module_rtl_names",
    "top_physical_abi",
    "top_boundary_scope",
]
