"""Strict, backend-neutral JSON primitives for :mod:`zlang.backend.manifest`.

The Clash and direct-SystemVerilog emitters publish the same ``BackendArtifact``
schema.  This module owns only that schema's lexical JSON boundary and binding
records; backend-specific naming and RTL/source rendering deliberately remain in
their emitters.
"""

from __future__ import annotations

import json
import re
from typing import Any

from zlang.backend.naming import RTL_NAMING_SCHEMA
from zlang.ir.equivalence import (
    BindingSide,
    EquivalenceBinding,
    SignalRole,
)
from zlang.source import SourceOrigin


_ARTIFACT_FIELDS = frozenset(
    {
        "manifest_version",
        "naming_schema",
        "backend",
        "module",
        "selected_ir_identity",
        "artifact_hash",
        "build_identity",
        "library_dependencies",
        "bindings",
        "root_module_identity",
        "dependency_closure",
        "module_signature",
        "physical_domains",
        "timing_contract",
        "output_timings",
        "instance_output_timings",
        "companions",
        "formal_artifact_hash",
        "components",
        "instances",
        "recursive_bindings",
        "formal_observations",
        "implementation",
    }
)
_REQUIRED_ARTIFACT_FIELDS = frozenset(
    {
        "manifest_version",
        "backend",
        "module",
        "selected_ir_identity",
        "artifact_hash",
        "bindings",
    }
)
_COLLECTION_FIELDS = frozenset(
    {
        "library_dependencies",
        "bindings",
        "output_timings",
        "instance_output_timings",
        "companions",
        "components",
        "instances",
        "recursive_bindings",
        "formal_observations",
        "physical_domains",
    }
)
_BINDING_FIELDS = frozenset(
    {
        "map_version",
        "side",
        "semantic_signal_id",
        "selected_ir_identity",
        "rtl_module",
        "rtl_path",
        "width",
        "signedness",
        "role",
        "clock_domain",
        "reset_domain",
        "backend",
        "artifact_hash",
        "source_origin",
        "aggregate_endpoint_id",
        "protocol_specialization_id",
        "protocol_role",
        "member_path",
        "ownership",
        "signal_kind",
        "physical_available",
        "canonical_type",
    }
)


def canonical_json(data: dict[str, object]) -> str:
    """Encode one manifest without changing the established byte format."""

    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def decode_artifact_payload(
    payload: str | bytes | dict[str, object],
    *,
    minimum_version: int,
    maximum_version: int,
) -> dict[str, object]:
    """Decode and validate the stable BackendArtifact envelope.

    Nested semantic records retain their existing version-specific decoders in
    ``manifest.py``.  This boundary rejects malformed container shapes before
    those decoders can accidentally iterate strings or mappings as sequences.
    """

    decoded: object = (
        json.loads(payload) if isinstance(payload, (str, bytes)) else payload
    )
    if not isinstance(decoded, dict):
        raise ValueError("backend manifest must be a JSON object")
    data = decoded
    unknown = set(data) - _ARTIFACT_FIELDS
    if unknown:
        raise ValueError(
            "backend manifest contains unsupported field(s): "
            + ", ".join(sorted(str(item) for item in unknown))
        )
    missing = _REQUIRED_ARTIFACT_FIELDS - set(data)
    if missing:
        raise ValueError(
            "backend manifest is missing required field(s): "
            + ", ".join(sorted(missing))
        )
    version = _strict_int(data["manifest_version"], "manifest_version")
    if version < minimum_version or version > maximum_version:
        raise ValueError(f"unsupported backend manifest version: {version}")
    for field in ("backend", "module", "selected_ir_identity"):
        value = data[field]
        if not isinstance(value, str) or not value:
            raise ValueError(f"backend manifest {field} must be a non-empty string")
    validate_sha256(data["artifact_hash"], "artifact_hash")
    validate_naming_schema(data.get("naming_schema"))
    build_identity = data.get("build_identity")
    if build_identity is not None:
        validate_sha256(build_identity, "build_identity")
    formal_hash = data.get("formal_artifact_hash")
    if formal_hash is not None:
        validate_sha256(formal_hash, "formal_artifact_hash")
    for field in _COLLECTION_FIELDS:
        value = data.get(field, ())
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"backend manifest {field} must be an array")
    # Check newest features first so a deliberately downgraded manifest reports
    # the feature that selected its original version, not lower-version empty
    # compatibility sections also present in the canonical JSON.
    versioned_fields = (
        (10, ("physical_domains",)),
        (9, ("module_signature",)),
        (8, ("root_module_identity", "dependency_closure")),
        (
            7,
            (
                "timing_contract",
                "output_timings",
                "instance_output_timings",
                "implementation",
            ),
        ),
        (5, ("companions",)),
        (
            4,
            (
                "formal_artifact_hash",
                "components",
                "instances",
                "recursive_bindings",
                "formal_observations",
            ),
        ),
    )
    for required_version, fields in versioned_fields:
        illegal = tuple(field for field in fields if field in data and version < required_version)
        if illegal:
            raise ValueError(
                f"backend manifest field(s) {', '.join(illegal)} requires manifest "
                f"version {required_version} or newer"
            )
    for collection in ("components", "instances"):
        for index, item in enumerate(data.get(collection, ())):
            if not isinstance(item, dict):
                raise ValueError(
                    f"backend manifest {collection}[{index}] must be an object"
                )
            if version < 10 and "physical_domain_identity" in item:
                raise ValueError(
                    "backend manifest physical_domain_identity requires manifest "
                    "version 10 or newer"
                )
    return data


def origin_to_data(
    origin: SourceOrigin | str | None,
) -> dict[str, object] | str | None:
    """Encode structured origins while retaining legacy manifest strings."""

    return origin.to_data() if isinstance(origin, SourceOrigin) else origin


def origin_from_data(
    origin: object,
    *,
    parse_legacy: bool = False,
) -> SourceOrigin | str | None:
    """Decode structured origins and optionally recover legacy rendered text."""

    if origin is None:
        return None
    if isinstance(origin, dict):
        return SourceOrigin.from_data(origin)
    if isinstance(origin, str):
        return SourceOrigin.from_data(origin) if parse_legacy else origin
    raise ValueError("backend manifest source_origin must be an object, string, or null")


def binding_to_data(item: EquivalenceBinding) -> dict[str, object]:
    """Serialize one backend-independent source/RTL binding."""

    return {
        "map_version": item.map_version,
        "side": item.side.value,
        "semantic_signal_id": item.semantic_signal_id,
        "selected_ir_identity": item.selected_ir_identity,
        "rtl_module": item.rtl_module,
        "rtl_path": item.rtl_path,
        "width": item.width,
        "signedness": item.signedness,
        "role": item.role.value,
        "clock_domain": item.clock_domain,
        "reset_domain": item.reset_domain,
        "backend": item.backend,
        "artifact_hash": item.artifact_hash,
        "source_origin": origin_to_data(item.source_origin),
        "aggregate_endpoint_id": item.aggregate_endpoint_id,
        "protocol_specialization_id": item.protocol_specialization_id,
        "protocol_role": item.protocol_role,
        "member_path": list(item.member_path),
        "ownership": item.ownership,
        "signal_kind": item.signal_kind,
        "physical_available": item.physical_available,
        "canonical_type": item.canonical_type,
    }


def binding_from_data(data: object, *, manifest_version: int) -> EquivalenceBinding:
    """Strictly decode one backend-independent source/RTL binding."""

    if not isinstance(data, dict):
        raise ValueError("backend manifest binding must be an object")
    unknown = set(data) - _BINDING_FIELDS
    if unknown:
        raise ValueError(
            "backend manifest binding contains unsupported field(s): "
            + ", ".join(sorted(str(item) for item in unknown))
        )
    required = {
        "side",
        "semantic_signal_id",
        "selected_ir_identity",
        "rtl_module",
        "rtl_path",
        "width",
        "signedness",
        "role",
        "backend",
        "artifact_hash",
    }
    missing = required - set(data)
    if missing:
        raise ValueError(
            "backend manifest binding is missing required field(s): "
            + ", ".join(sorted(missing))
        )
    for field in (
        "semantic_signal_id",
        "selected_ir_identity",
        "rtl_module",
        "rtl_path",
        "signedness",
        "backend",
    ):
        if not isinstance(data[field], str):
            raise ValueError(f"backend manifest binding {field} must be a string")
    member_path = data.get("member_path", ())
    if not isinstance(member_path, (list, tuple)) or not all(
        isinstance(item, str) for item in member_path
    ):
        raise ValueError("backend manifest binding member_path must be an array of strings")
    physical_available = data.get("physical_available", True)
    if not isinstance(physical_available, bool):
        raise ValueError("backend manifest binding physical_available must be boolean")
    validate_sha256(data["artifact_hash"], "binding artifact_hash")
    return EquivalenceBinding(
        _strict_int(data.get("map_version", manifest_version), "binding map_version"),
        BindingSide(data["side"]),
        data["semantic_signal_id"],
        data["selected_ir_identity"],
        data["rtl_module"],
        data["rtl_path"],
        _strict_int(data["width"], "binding width"),
        data["signedness"],
        SignalRole(data["role"]),
        data.get("clock_domain"),
        data.get("reset_domain"),
        data["backend"],
        data["artifact_hash"],
        source_origin=origin_from_data(data.get("source_origin"), parse_legacy=True),
        aggregate_endpoint_id=data.get("aggregate_endpoint_id"),
        protocol_specialization_id=data.get("protocol_specialization_id"),
        protocol_role=data.get("protocol_role"),
        member_path=tuple(member_path),
        ownership=data.get("ownership"),
        signal_kind=data.get("signal_kind"),
        physical_available=physical_available,
        canonical_type=data.get("canonical_type"),
    )


def validate_naming_schema(value: object) -> None:
    """Old/manual manifests have no naming claim; new claims are exact."""

    if value is not None and value != RTL_NAMING_SCHEMA:
        raise ValueError("unsupported backend naming_schema")


def validate_artifact_links(artifact: Any) -> None:
    """Require every public binding to identify its exact containing artifact."""

    validate_sha256(artifact.artifact_hash, "artifact_hash")
    validate_naming_schema(artifact.naming_schema)
    if artifact.formal_artifact_hash is not None:
        validate_sha256(artifact.formal_artifact_hash, "formal_artifact_hash")
    for binding in artifact.bindings:
        if binding.backend != artifact.backend:
            raise ValueError(
                f"backend binding '{binding.semantic_signal_id}' backend does not "
                "match its artifact"
            )
        if binding.selected_ir_identity != artifact.selected_ir_identity:
            raise ValueError(
                f"backend binding '{binding.semantic_signal_id}' selected IR identity "
                "does not match its artifact"
            )
        if binding.artifact_hash != artifact.artifact_hash:
            raise ValueError(
                f"backend binding '{binding.semantic_signal_id}' hash does not match "
                "its artifact"
            )
        if binding.map_version > artifact.manifest_version:
            raise ValueError(
                f"backend binding '{binding.semantic_signal_id}' map version is newer "
                "than its artifact manifest"
            )
    accepted_recursive_hashes = {artifact.artifact_hash}
    if artifact.formal_artifact_hash is not None:
        # Recursive observations are first published against the generated
        # backend source, then rebound to validated formal RTL.  The production
        # bindings above remain tied to ``artifact_hash`` in both states.
        accepted_recursive_hashes.add(artifact.formal_artifact_hash)
    for binding in artifact.recursive_bindings:
        if binding.backend != artifact.backend:
            raise ValueError(
                f"recursive binding '{binding.semantic_binding_id}' backend does not "
                "match its artifact"
            )
        if binding.artifact_hash not in accepted_recursive_hashes:
            raise ValueError(
                f"recursive binding '{binding.semantic_binding_id}' hash does not "
                "match its artifact"
            )
    for observation in artifact.formal_observations:
        if observation.artifact_hash not in accepted_recursive_hashes:
            raise ValueError(
                f"formal observation '{observation.semantic_binding_id}' hash does "
                "not match its artifact"
            )

    physical_domains = tuple(getattr(artifact, "physical_domains", ()))
    if artifact.manifest_version < 10:
        if physical_domains:
            raise ValueError(
                "backend physical domains require manifest version 10 or newer"
            )
    else:
        if not physical_domains:
            raise ValueError(
                "backend manifest version 10 requires at least one physical domain"
            )
    domains_by_identity: dict[str, Any] = {}
    for domain in physical_domains:
        domain.validate()
        if domain.rtl_module != artifact.module:
            raise ValueError(
                f"backend physical domain '{domain.identity}' RTL module does not "
                "match its artifact"
            )
        if domain.identity in domains_by_identity:
            raise ValueError(
                f"duplicate backend physical domain identity '{domain.identity}'"
            )
        domains_by_identity[domain.identity] = domain
        clock_bindings = tuple(
            item for item in artifact.bindings
            if item.role is SignalRole.CLOCK
            and item.clock_domain == domain.clock
            and item.reset_domain == domain.reset
        )
        reset_bindings = tuple(
            item for item in artifact.bindings
            if item.role is SignalRole.RESET
            and item.clock_domain == domain.clock
            and item.reset_domain == domain.reset
        )
        if len(clock_bindings) != 1 or len(reset_bindings) != 1:
            raise ValueError(
                f"backend physical domain '{domain.identity}' does not resolve to "
                "exactly one public clock and reset binding"
            )
        for label, binding, expected_path in (
            ("clock", clock_bindings[0], domain.rtl_clock_path),
            ("reset", reset_bindings[0], domain.rtl_reset_path),
        ):
            if (
                not binding.physical_available
                or binding.width != 1
                or binding.rtl_module != domain.rtl_module
                or binding.rtl_path != expected_path
            ):
                raise ValueError(
                    f"backend physical domain {label} locator does not match its "
                    "validated public binding"
                )

    component_by_identity: dict[str, Any] = {}
    for component in artifact.components:
        if component.identity in component_by_identity:
            raise ValueError(
                f"duplicate backend component identity '{component.identity}'"
            )
        component_by_identity[component.identity] = component
        _validate_physical_domain_link(
            component,
            domains_by_identity,
            artifact.manifest_version,
            f"component '{component.identity}'",
        )
    for instance in artifact.instances:
        component = component_by_identity.get(instance.component_identity)
        if component is None and artifact.manifest_version >= 10:
            raise ValueError(
                f"backend instance '{instance.instance_identity}' references an "
                "unknown component"
            )
        _validate_physical_domain_link(
            instance,
            domains_by_identity,
            artifact.manifest_version,
            f"instance '{instance.instance_identity}'",
        )
        if component is not None and (
            instance.physical_domain_identity
            != component.physical_domain_identity
        ):
            raise ValueError(
                f"backend instance '{instance.instance_identity}' physical domain "
                "does not match its component"
            )


def _validate_physical_domain_link(
    item: Any,
    domains_by_identity: dict[str, Any],
    manifest_version: int,
    label: str,
) -> None:
    clock = item.clock_domain
    reset = item.reset_domain
    identity = getattr(item, "physical_domain_identity", None)
    if (clock is None) != (reset is None):
        raise ValueError(f"backend {label} has an incomplete clock/reset domain")
    sequential = clock is not None
    if manifest_version < 10:
        if identity is not None:
            raise ValueError(
                f"backend {label} physical domain link requires manifest version 10"
            )
        return
    if not sequential:
        if identity is not None:
            raise ValueError(
                f"backend combinational {label} cannot reference a physical domain"
            )
        return
    if not isinstance(identity, str) or not identity:
        raise ValueError(f"backend sequential {label} is missing its physical domain link")
    domain = domains_by_identity.get(identity)
    if domain is None:
        raise ValueError(f"backend {label} references an unknown physical domain")
    if domain.clock != clock or domain.reset != reset:
        raise ValueError(
            f"backend {label} physical domain link disagrees with its clock/reset"
        )


def validate_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"backend manifest {field} must be a SHA-256 digest")
    return value


def _strict_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"backend manifest {field} must be an integer")
    return value


__all__ = [
    "binding_from_data",
    "binding_to_data",
    "canonical_json",
    "decode_artifact_payload",
    "origin_from_data",
    "origin_to_data",
    "validate_artifact_links",
]
