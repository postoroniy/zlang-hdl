# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Prepared formal-route model, cache codec, and backend connection."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import PurePosixPath
import re

from zlang.backend.companions import CompanionArtifactError
from zlang.backend.manifest import BackendArtifact
from zlang.backend.naming import RTL_NAMING_SCHEMA
from zlang.backend.source_map import GeneratedSourceMap, build_generated_source_map
from zlang.compilation_products import CompilationResult
from zlang.common import stable_digest
from zlang import formal as formal
from zlang.ir import formal as ir_formal
from zlang.ir import formal_planning as formal_planning
from zlang.ir import formal_predicates as formal_predicates
from zlang.ir.module import dependency_context_identity
from zlang.opt.identity import CANONICAL_IR_IDENTITY_SCHEMA


_PATH_TOKEN = re.compile(r"[^A-Za-z0-9_.-]+")

@dataclass(frozen=True)
class _PreparedFormalRoute:
    """One immutable backend artifact connected independently per property."""

    artifact: object
    connected: ir_formal.FormalDesign
    backend: str
    artifact_hash: str
    implementation: str
    implementation_path: str
    source_map_path: str
    source_map: str
    companion_paths: tuple[str, ...]


@dataclass(frozen=True)
class _PreparedFormalGoal:
    """One exact binding set and checker emitted from one prepared route."""

    bindings: tuple[object, ...]
    binding_identity: str
    route: formal_planning.FormalExecutableRoute
    harness_path: str
    checker: str


def _binding_payload(binding: object) -> dict[str, object]:
    return {
        "semantic_signal_id": getattr(binding, "semantic_signal_id"),
        "rtl_module": getattr(binding, "rtl_module"),
        "rtl_name": getattr(binding, "rtl_name"),
        "width": getattr(binding, "width"),
        "direction": getattr(binding, "direction"),
    }


def _binding_recipe_payload(binding: object) -> dict[str, object]:
    return {
        **_binding_payload(binding),
        "clock_domain": getattr(binding, "clock_domain", None),
    }


def _binding_identity(bindings: tuple[object, ...]) -> str:
    return "bindings:" + stable_digest([
        _binding_payload(item)
        for item in sorted(
            bindings,
            key=lambda value: getattr(value, "semantic_signal_id"),
        )
    ])


def _checker_reset_trace_bindings(
    design: ir_formal.FormalDesign,
    *,
    top: str,
    cover: bool = False,
) -> tuple[ir_formal.SignalBinding, ...]:
    """Publish normalized and raw reset leaves from the exact checker plan."""

    by_id = {item.semantic_signal_id: item for item in design.bindings}
    raw_reset = by_id.get("reset")
    if raw_reset is None:
        raise ir_formal.FormalError("connected formal checker has no physical reset binding")
    rendered = ir_formal.formal_harness_domain_rendering(
        design,
        top=top,
        cover=cover,
    )
    return (
        ir_formal.SignalBinding(
            "trace:reset",
            top,
            rendered.reset_active,
            1,
            "internal" if rendered.reset_active != raw_reset.rtl_name else "input",
            raw_reset.clock_domain,
            raw_reset.source_origin,
        ),
        ir_formal.SignalBinding(
            "physical_reset",
            raw_reset.rtl_module,
            raw_reset.rtl_name,
            1,
            "input",
            raw_reset.clock_domain,
            raw_reset.source_origin,
        ),
    )


def _origin_payload(value: object | None) -> object | None:
    if value is None:
        return None
    to_data = getattr(value, "to_data", None)
    return to_data() if callable(to_data) else str(value)


def _formal_property_recipe_payload(value: object) -> dict[str, object]:
    def enum_value(name: str) -> object | None:
        item = getattr(value, name, None)
        return getattr(item, "value", item)

    predicate = getattr(value, "predicate", None)
    return {
        "id": getattr(value, "id"),
        "kind": enum_value("kind"),
        "clock": getattr(value, "clock"),
        "reset_condition": getattr(value, "reset_condition", None),
        "expression": getattr(value, "expression"),
        "temporal_form": enum_value("temporal_form"),
        "ownership": enum_value("ownership"),
        "classification": enum_value("classification"),
        "predicate": None if predicate is None else predicate.to_data(),
        "generated_from": getattr(value, "generated_from", None),
        "relevant_signals": list(getattr(value, "relevant_signals", ())),
        "antecedent": getattr(value, "antecedent", None),
        "consequent": getattr(value, "consequent", None),
        "min_delay": getattr(value, "min_delay", None),
        "max_delay": getattr(value, "max_delay", None),
        "non_executable_reason": getattr(value, "non_executable_reason", None),
        "source_origin": _origin_payload(getattr(value, "source_origin", None)),
    }


def _formal_design_recipe_payload(design: ir_formal.FormalDesign) -> dict[str, object]:
    return {
        "module": design.module_name,
        "properties": [
            _formal_property_recipe_payload(item) for item in design.properties
        ],
        "covers": [
            _formal_property_recipe_payload(item) for item in design.covers
        ],
        "bindings": [
            _binding_recipe_payload(item) for item in design.bindings
        ],
        "clock_domains": [
            {
                "clock": item.clock,
                "reset": item.reset,
                "edge": item.edge.value,
                "reset_mode": item.reset_mode.value,
                "reset_polarity": item.reset_polarity.value,
                "reset_release_mode": item.reset_release_mode.value,
                "reset_release_cycles": item.reset_release_cycles,
                "power_up": item.power_up.value,
            }
            for item in design.clock_domains
        ],
    }


def _recursive_formal_recipe_payload(result: CompilationResult) -> object | None:
    design = result.recursive_formal_design
    if design is None:
        return None
    to_dict = getattr(design, "to_dict", None)
    if not callable(to_dict):
        raise ir_formal.FormalError(
            "recursive formal design has no deterministic recipe serialization"
        )
    return to_dict()


def _prepared_route_recipe(
    result: CompilationResult,
    backend: str,
    *,
    tool_route: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema": "zlang-verification-prepared-route-recipe-v1",
        "backend": backend,
        "selected_ir_identity": result.selected_ir_identity,
        "source_context": {
            "source_identity": result.ir.source_identity or result.ir.name,
            "source_hash": result.ir.source_hash,
            "dependency_identity": dependency_context_identity(result.ir),
        },
        "formal_design": _formal_design_recipe_payload(result.formal_design),
        "recursive_formal_design": _recursive_formal_recipe_payload(result),
        "compiler": {
            "version": _compiler_version(),
            "canonical_ir": CANONICAL_IR_IDENTITY_SCHEMA,
            "formal_predicate": formal_predicates.FORMAL_PREDICATE_SCHEMA,
            # v7 binds rule-fire projection to the exact effective reset.
            # Older prepared artifacts can otherwise retain the raw active-high
            # predicate even when the production reset contract is different.
            "verification_publication": 7,
            "rtl_naming": RTL_NAMING_SCHEMA,
        },
        "tool_route": tool_route,
    }


def _prepared_route_fingerprint(route: _PreparedFormalRoute) -> dict[str, object]:
    companions = tuple(getattr(route.artifact, "companions", ()))
    artifact_manifest = json.loads(route.artifact.to_json())
    return {
        "backend": route.backend,
        "artifact_hash": route.artifact_hash,
        # RTL text can remain byte-identical while a formal-only physical ABI
        # correction changes its top module, public-domain ownership, or
        # observation locators.  Bind exact-goal caches to that complete
        # canonical metadata rather than to text and a partial binding list.
        "artifact_manifest_identity": stable_digest(artifact_manifest),
        "artifact_module": getattr(route.artifact, "module"),
        "bindings": [
            _binding_recipe_payload(item)
            for item in sorted(
                route.connected.bindings,
                key=lambda value: getattr(value, "semantic_signal_id"),
            )
        ],
        "implementation_hash": hashlib.sha256(
            route.implementation.encode("utf-8")
        ).hexdigest(),
        "source_map_hash": hashlib.sha256(
            route.source_map.encode("utf-8")
        ).hexdigest(),
        "companions": [
            {
                "logical_path": item.logical_path,
                "file_hash": item.file_hash,
                "text_hash": hashlib.sha256(item.text.encode("ascii")).hexdigest(),
            }
            for item in companions
        ],
    }


_PREPARED_ROUTE_CACHE_SCHEMA = "zlang-prepared-formal-route-v1"


def _encode_prepared_route(route: _PreparedFormalRoute) -> dict[str, object]:
    """Serialize every immutable input needed to replay one prepared route."""

    if not isinstance(route.artifact, BackendArtifact):
        raise ir_formal.FormalError("prepared formal route requires a BackendArtifact")
    if hashlib.sha256(route.artifact.text.encode("utf-8")).hexdigest() != (
        route.artifact.artifact_hash
    ):
        raise ir_formal.FormalError("prepared formal artifact text does not match its hash")
    # Parsing here validates the sidecar before it can be persisted.
    source_map = GeneratedSourceMap.from_json(route.source_map)
    if source_map.artifact_hash != route.artifact.artifact_hash:
        raise ir_formal.FormalError("prepared formal source map does not match its artifact")
    companions = tuple(route.artifact.companions)
    if len(route.companion_paths) != len(companions):
        raise ir_formal.FormalError("prepared formal route companion paths are incomplete")
    return {
        "schema": _PREPARED_ROUTE_CACHE_SCHEMA,
        "backend": route.backend,
        "artifact_hash": route.artifact_hash,
        "artifact_manifest": json.loads(route.artifact.to_json()),
        "artifact_text": route.artifact.text,
        "implementation": route.implementation,
        "implementation_path": route.implementation_path,
        "source_map_path": route.source_map_path,
        "source_map": json.loads(route.source_map),
        "companion_paths": list(route.companion_paths),
        "companions": [
            {
                "logical_path": item.logical_path,
                "file_hash": item.file_hash,
                "text": item.text,
            }
            for item in companions
        ],
    }


def _prepared_route_path(value: object, *, prefix: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ir_formal.FormalError(f"cached prepared route {field} must be non-empty")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ir_formal.FormalError(f"cached prepared route {field} is unsafe")
    if path.parts[0] != prefix:
        raise ir_formal.FormalError(
            f"cached prepared route {field} must be below '{prefix}/'"
        )
    return value


def _decode_prepared_route(
    result: CompilationResult,
    value: object,
) -> _PreparedFormalRoute:
    """Strictly restore a complete route without invoking either backend."""

    if not isinstance(value, dict):
        raise ir_formal.FormalError("cached prepared formal route must be an object")
    expected_fields = {
        "schema", "backend", "artifact_hash", "artifact_manifest",
        "artifact_text", "implementation", "implementation_path",
        "source_map_path", "source_map", "companion_paths", "companions",
    }
    if set(value) != expected_fields:
        raise ir_formal.FormalError("cached prepared formal route fields are invalid")
    if value.get("schema") != _PREPARED_ROUTE_CACHE_SCHEMA:
        raise ir_formal.FormalError("cached prepared formal route schema is unsupported")
    manifest = value.get("artifact_manifest")
    if not isinstance(manifest, dict):
        raise ir_formal.FormalError("cached prepared formal artifact manifest must be an object")
    artifact_text = value.get("artifact_text")
    implementation = value.get("implementation")
    source_map_data = value.get("source_map")
    if not isinstance(artifact_text, str) or not isinstance(implementation, str):
        raise ir_formal.FormalError("cached prepared formal route text must be UTF-8 text")
    if not isinstance(source_map_data, dict):
        raise ir_formal.FormalError("cached prepared formal source map must be an object")
    artifact = BackendArtifact.from_json(manifest)
    if hashlib.sha256(artifact_text.encode("utf-8")).hexdigest() != artifact.artifact_hash:
        raise ir_formal.FormalError("cached prepared formal artifact text hash does not match")

    raw_companions = value.get("companions")
    if not isinstance(raw_companions, list):
        raise ir_formal.FormalError("cached prepared formal companions must be an array")
    by_path: dict[str, dict[str, object]] = {}
    for item in raw_companions:
        if not isinstance(item, dict) or set(item) != {
            "logical_path", "file_hash", "text"
        }:
            raise ir_formal.FormalError("cached prepared formal companion fields are invalid")
        logical_path = item.get("logical_path")
        file_hash = item.get("file_hash")
        text = item.get("text")
        if (
            not isinstance(logical_path, str)
            or not isinstance(file_hash, str)
            or not isinstance(text, str)
        ):
            raise ir_formal.FormalError("cached prepared formal companion values are invalid")
        if logical_path in by_path:
            raise ir_formal.FormalError("cached prepared formal companion path is duplicated")
        by_path[logical_path] = item
    restored_companions = []
    for companion in artifact.companions:
        item = by_path.pop(companion.logical_path, None)
        if item is None or item["file_hash"] != companion.file_hash:
            raise ir_formal.FormalError(
                f"cached prepared companion '{companion.logical_path}' metadata differs"
            )
        try:
            restored_companions.append(replace(companion, text=item["text"]))
        except CompanionArtifactError as error:
            raise ir_formal.FormalError(str(error)) from error
    if by_path:
        raise ir_formal.FormalError("cached prepared formal route has an unknown companion")
    artifact = replace(
        artifact,
        text=artifact_text,
        companions=tuple(restored_companions),
    )
    if json.loads(artifact.to_json()) != manifest:
        raise ir_formal.FormalError("cached prepared formal artifact manifest does not round-trip")

    backend = value.get("backend")
    artifact_hash = value.get("artifact_hash")
    if backend != artifact.backend or artifact_hash != artifact.artifact_hash:
        raise ir_formal.FormalError("cached prepared formal route artifact identity differs")
    implementation_path = _prepared_route_path(
        value.get("implementation_path"),
        prefix="implementation",
        field="implementation path",
    )
    source_map_path = _prepared_route_path(
        value.get("source_map_path"),
        prefix="source-map",
        field="source-map path",
    )
    raw_paths = value.get("companion_paths")
    if not isinstance(raw_paths, list) or any(
        not isinstance(item, str) for item in raw_paths
    ):
        raise ir_formal.FormalError("cached prepared companion paths must be strings")
    companion_paths = tuple(
        _prepared_route_path(
            item,
            prefix="implementation",
            field="companion path",
        )
        for item in raw_paths
    )
    source_map = GeneratedSourceMap.from_json(source_map_data)
    if (
        source_map.backend != artifact.backend
        or source_map.module != artifact.module
        or source_map.selected_ir_identity != artifact.selected_ir_identity
        or source_map.artifact_hash != artifact.artifact_hash
    ):
        raise ir_formal.FormalError("cached prepared formal source map identity differs")

    # Reuse the same whole-design compatibility adapter as a fresh route.
    # A valid multi-domain artifact cannot be connected through the historical
    # shared ``clock``/``reset`` IDs until each exact goal is isolated; calling
    # ``connect_formal_design`` directly here made enabling the disk cache turn
    # otherwise executable per-goal routes into backend-unavailable failures.
    expected = _prepare_route(result, artifact)
    restored = _PreparedFormalRoute(
        artifact,
        expected.connected,
        artifact.backend,
        artifact.artifact_hash,
        implementation,
        implementation_path,
        source_map_path,
        source_map.to_json(),
        companion_paths,
    )
    if (
        restored.backend != expected.backend
        or restored.artifact_hash != expected.artifact_hash
        or restored.implementation != expected.implementation
        or restored.implementation_path != expected.implementation_path
        or restored.source_map_path != expected.source_map_path
        or restored.source_map != expected.source_map
        or restored.companion_paths != expected.companion_paths
    ):
        raise ir_formal.FormalError("cached prepared formal route is not canonical")
    return restored


def _prepare_route(result: CompilationResult, artifact: object) -> _PreparedFormalRoute:
    backend = str(getattr(artifact, "backend"))
    artifact_hash = str(
        getattr(artifact, "formal_artifact_hash", None)
        or getattr(artifact, "artifact_hash")
    )
    route_token = _token(f"{backend}:{artifact_hash}")
    implementation = str(getattr(artifact, "text"))
    try:
        connected = formal.connect_formal_design(result.formal_design, artifact)
    except ir_formal.FormalError:
        # Generic harness-local IDs ``clock``/``reset`` are intentionally
        # reused by each independent goal.  A whole-design connection can
        # therefore collide for a valid multi-domain module; exact connection
        # happens later in ``_connect_exact_goal``.  Retain only immutable
        # artifact metadata here.  Single-domain/recursive routes keep the
        # richer connected view when it is available.
        connected = replace(
            result.formal_design,
            bindings=(),
            connected_backend=backend,
            connected_artifact_hash=artifact_hash,
            connected_module=str(getattr(artifact, "module")),
            implementation_text=implementation,
            dut_ports=(),
            non_executable_reason=None,
        )
    implementation = connected.implementation_text or implementation
    return _PreparedFormalRoute(
        artifact,
        connected,
        backend,
        artifact_hash,
        implementation,
        f"implementation/{route_token}/formal.sv",
        f"source-map/{route_token}.json",
        build_generated_source_map(result.ir, artifact).to_json(),
        tuple(
            f"implementation/companions/{route_token}/{item.logical_path}"
            for item in getattr(artifact, "companions", ())
        ),
    )



def _token(value: str) -> str:
    return _PATH_TOKEN.sub("_", value).strip("_") or "artifact"


def _compiler_version() -> str:
    try:
        return version("zlang-hdl")
    except PackageNotFoundError:
        return "source-checkout"
