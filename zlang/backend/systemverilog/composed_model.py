"""Immutable inputs for composed direct-SystemVerilog rendering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from zlang.backend import naming
from zlang.ir import hierarchy as ir_hierarchy
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module


@dataclass(frozen=True)
class FormalBufferCountProjection:
    """One typed formal-only request for a directional FIFO count output."""

    request_response_semantic_id: str
    channel: ir_interfaces.RequestResponseChannel
    signal: str
    width: int
    depth: int


@dataclass(frozen=True)
class ComposedRendering:
    """Per-emission capabilities that genuinely depend on caller state."""

    emit_external: Callable[[ir_module.Module, str], str]
    emit_cdc_child: Callable[[ir_module.Module], str]
    hierarchy_cache: ir_hierarchy.HierarchyTraversalCache
    formal_buffer_counts: tuple[FormalBufferCountProjection, ...] = ()
    component_names: naming.ComponentNamePlan | None = None


__all__ = ["ComposedRendering", "FormalBufferCountProjection"]
