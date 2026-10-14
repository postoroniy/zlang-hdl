# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned immutable metadata for one native debug session.

The ordinary :class:`~zlang.simulation_plan_model.SimulationPlan` remains the
execution product.  This module owns only source presentation, executable-site
and typed-signal metadata required by a debugger.  Physical paths are excluded
from stable identities deliberately.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Mapping

from zlang import sim
from zlang.compilation_inputs import PhysicalCompilationInputs
from zlang.ir.module import Module
from zlang.source import SourceOrigin


DEBUG_BUNDLE_SCHEMA = "zlang-native-debug-bundle-v1"
DEBUG_OVERLAY_SCHEMA = "zlang-debug-overlay-v1"


def _digest(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, order=True)
class DebugSourceUnit:
    logical_path: str
    digest: str
    presentation_path: str | None = field(default=None, compare=False)

    def identity_data(self) -> dict[str, str]:
        return {"digest": self.digest, "logical_path": self.logical_path}

    def to_data(self) -> dict[str, object]:
        return {
            **self.identity_data(),
            "presentation_path": self.presentation_path,
        }


@dataclass(frozen=True, order=True)
class DebugSignal:
    name: str
    kind: str
    width: int
    type_data: str
    direction: str | None = None
    depth: int | None = None

    def to_data(self) -> dict[str, object]:
        return {
            "direction": self.direction,
            "depth": self.depth,
            "kind": self.kind,
            "name": self.name,
            "type": self.type_data,
            "width": self.width,
        }


@dataclass(frozen=True, order=True)
class DebugClockReset:
    clock: str
    reset: str | None
    edge: str
    reset_mode: str
    reset_polarity: str

    def to_data(self) -> dict[str, str | None]:
        return {
            "clock": self.clock,
            "edge": self.edge,
            "reset": self.reset,
            "reset_mode": self.reset_mode,
            "reset_polarity": self.reset_polarity,
        }


@dataclass(frozen=True, order=True)
class DebugExecutableSite:
    site_id: str
    kind: str
    name: str
    source_unit: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    condition_node: int | None = None
    clock: str | None = None
    event_id: int | None = None

    def to_data(self) -> dict[str, object]:
        return {
            "condition_node": self.condition_node,
            "clock": self.clock,
            "end": {"column": self.end_column, "line": self.end_line},
            "id": self.site_id,
            "kind": self.kind,
            "name": self.name,
            "event_id": self.event_id,
            "source_unit": self.source_unit,
            "start": {"column": self.start_column, "line": self.start_line},
        }


@dataclass(frozen=True)
class NativeDebugBundle:
    plan_bytes: bytes = field(repr=False, compare=False)
    plan_identity: str
    selected_ir_identity: str
    sources: tuple[DebugSourceUnit, ...]
    signals: tuple[DebugSignal, ...]
    clocks: tuple[DebugClockReset, ...]
    sites: tuple[DebugExecutableSite, ...]
    identity: str
    overlay_identity: str
    schema: str = DEBUG_BUNDLE_SCHEMA

    def identity_data(self) -> dict[str, object]:
        return {
            "clocks": [item.to_data() for item in self.clocks],
            "plan_identity": self.plan_identity,
            "schema": self.schema,
            "selected_ir_identity": self.selected_ir_identity,
            "signals": [item.to_data() for item in self.signals],
            "sites": [item.to_data() for item in self.sites],
            "sources": [item.identity_data() for item in self.sources],
        }

    def to_data(self) -> dict[str, object]:
        return {
            **self.identity_data(),
            "identity": self.identity,
            "overlay_schema": DEBUG_OVERLAY_SCHEMA,
            "overlay_identity": self.overlay_identity,
            "plan": base64.b64encode(self.plan_bytes).decode("ascii"),
            "sources": [item.to_data() for item in self.sources],
        }


def _site(
    *,
    identity: str,
    kind: str,
    name: str,
    origin: SourceOrigin | None,
    fallback_unit: str,
    condition_node: int | None = None,
    clock: str | None = None,
    event_id: int | None = None,
) -> DebugExecutableSite | None:
    if origin is None:
        return None
    source_unit = origin.source_unit or fallback_unit
    return DebugExecutableSite(
        identity,
        kind,
        name,
        source_unit,
        origin.span.start_line,
        origin.span.start_column,
        origin.span.end_line,
        origin.span.end_column,
        condition_node,
        clock,
        event_id,
    )


def _module_sites(
    module: Module,
    *,
    fallback_unit: str,
    scheduler_fire_nodes: Mapping[str, int],
    instrumentation_events: Mapping[str, int],
) -> tuple[DebugExecutableSite, ...]:
    result: list[DebugExecutableSite] = []
    transition = module.resolved_transition
    if transition is not None:
        for group in transition.action_groups:
            construct = "" if group.source_origin is None else group.source_origin.construct
            kind = "fsm_transition" if "transition" in construct else "rule"
            item = _site(
                identity=group.semantic_id,
                kind=kind,
                name=group.rule_name,
                origin=group.source_origin,
                fallback_unit=fallback_unit,
                condition_node=scheduler_fire_nodes.get(group.rule_name),
                clock=group.domain or transition.domain or module.clock,
            )
            if item is not None:
                result.append(item)
    for scope in module.verification_scopes:
        for clause in (*scope.requirements, *scope.goals):
            item = _site(
                identity=clause.semantic_id,
                kind="verification",
                name=clause.name,
                origin=clause.source_origin,
                fallback_unit=fallback_unit,
                event_id=instrumentation_events.get(clause.semantic_id),
            )
            if item is not None:
                result.append(item)
    for child in module.children:
        result.extend(_module_sites(
            child,
            fallback_unit=fallback_unit,
            scheduler_fire_nodes=scheduler_fire_nodes,
            instrumentation_events=instrumentation_events,
        ))
    return tuple(sorted(set(result)))


def _signal_catalog(plan: Mapping[str, object]) -> tuple[DebugSignal, ...]:
    result: list[DebugSignal] = []
    for item in plan["ports"]:  # type: ignore[index]
        result.append(DebugSignal(
            str(item["name"]),
            "port",
            int(item["width"]),
            str(item["api_type"]["kind"]),
            str(item["direction"]),
        ))
    for item in plan["registers"]:  # type: ignore[index]
        result.append(DebugSignal(
            str(item["name"]), "register", int(item["width"]),
            "packed", None,
        ))
    for item in plan["memories"]:  # type: ignore[index]
        result.append(DebugSignal(
            str(item["name"]), "memory", int(item["width"]),
            "packed", None, int(item["depth"]),
        ))
    return tuple(sorted(result))


def _source_units(
    plan: Mapping[str, object],
    source_path: Path,
    physical_inputs: PhysicalCompilationInputs | None,
) -> tuple[DebugSourceUnit, ...]:
    root_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    by_unit: dict[str, DebugSourceUnit] = {
        source_path.name: DebugSourceUnit(
            source_path.name, root_digest, str(source_path.resolve())
        )
    }
    origins = [
        origin
        for node in plan["nodes"]  # type: ignore[index]
        for origin in node["origins"]  # type: ignore[index]
    ]
    origins.extend(
        event["metadata"].get("source_origin")
        for event in plan["events"]  # type: ignore[index]
    )
    paths_by_digest: dict[str, Path] = {root_digest: source_path}
    if physical_inputs is not None:
        for path in physical_inputs.all_paths:
            if path.suffix == ".zhl":
                paths_by_digest.setdefault(
                    hashlib.sha256(path.read_bytes()).hexdigest(), path,
                )
    for origin in origins:
        if not isinstance(origin, Mapping):
            continue
        unit = origin.get("source_unit")
        digest = origin.get("digest")
        if isinstance(unit, str) and isinstance(digest, str):
            path = paths_by_digest.get(digest)
            presentation = None if path is None else str(path.resolve())
            by_unit[unit] = DebugSourceUnit(unit, digest, presentation)
    return tuple(sorted(by_unit.values()))


def build_native_debug_bundle(
    source: Path | str,
    *,
    top: str | None = None,
    project: Path | str | None = None,
    profile: str | None = None,
    logic_state: bool = True,
) -> NativeDebugBundle:
    """Compile one saved source snapshot into an execution plan plus overlay."""

    source_path = Path(source).resolve()
    program = sim.compile(
        source_path,
        top=top,
        project=project,
        profile=profile,
        logic_state=logic_state,
    )
    plan = program.plan.payload
    fallback_unit = source_path.name
    instrumentation_events = {
        str(item["metadata"]["clause_id"]): int(item["id"])
        for item in plan["events"]
    }
    sites = _module_sites(
        program.module,
        fallback_unit=fallback_unit,
        scheduler_fire_nodes=dict(program.plan.scheduler_fire_nodes),
        instrumentation_events=instrumentation_events,
    )
    sources = _source_units(plan, source_path, program.physical_inputs)
    signals = _signal_catalog(plan)
    clocks = tuple(sorted(DebugClockReset(
        str(item["clock"]),
        None if item["reset"] is None else str(item["reset"]),
        str(item["edge"]),
        str(item["reset_mode"]),
        str(item["reset_polarity"]),
    ) for item in plan["domains"]))
    selected_ir_identity = program.selected_ir_identity or ""
    overlay_identity = _digest({
        "schema": DEBUG_OVERLAY_SCHEMA,
        "sites": [item.to_data() for item in sites],
    })
    identity_payload = {
        "clocks": [item.to_data() for item in clocks],
        "plan_identity": program.plan.identity,
        "schema": DEBUG_BUNDLE_SCHEMA,
        "selected_ir_identity": selected_ir_identity,
        "signals": [item.to_data() for item in signals],
        "sites": [item.to_data() for item in sites],
        "sources": [item.identity_data() for item in sources],
    }
    return NativeDebugBundle(
        program.plan.to_bytes(),
        program.plan.identity,
        selected_ir_identity,
        sources,
        signals,
        clocks,
        sites,
        _digest(identity_payload),
        overlay_identity,
    )


__all__ = [
    "DEBUG_BUNDLE_SCHEMA",
    "DEBUG_OVERLAY_SCHEMA",
    "DebugClockReset",
    "DebugExecutableSite",
    "DebugSignal",
    "DebugSourceUnit",
    "NativeDebugBundle",
    "build_native_debug_bundle",
]
