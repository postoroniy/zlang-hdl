"""Deterministic whole-build records.

This module is deliberately independent of the compiler and backend emitters.  It
defines the public, versioned data model used to join their already-published
identities and files.  Runtime trivia (timestamps, wall time, cache hits and host
paths) has no place in this model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable, Mapping

from zlang.common.serialization import stable_digest, stable_json
from zlang.opt.identity import CANONICAL_IR_IDENTITY_SCHEMA
from zlang.source import SourceOrigin


WHOLE_BUILD_SCHEMA = "zlang-whole-build-manifest-v1"
WHOLE_BUILD_SCHEMA_VERSION = 1

_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_CANONICAL_IR_ID_RE = re.compile(r"(high-level|selected):[0-9a-f]{64}")
_EVIDENCE_STATUSES = frozenset({
    "typed_legal",
    "timing_validated",
    "bounded_pass",
    "proven",
    "failed",
    "witnessed",
    "bounded_unreached",
    "unknown",
    "skipped",
    "not_run",
})
_PROOF_MODES = frozenset({"bmc", "prove", "cover"})
_BACKEND_STATUSES = frozenset({
    "selected", "generic_fallback", "unsupported", "not_requested", "failed",
})
_BACKEND_REQUIREMENTS = frozenset({"preferred", "required", "not_requested"})
_TOOL_STATUSES = frozenset({"passed", "failed", "unknown", "skipped", "not_run"})
_NON_IDENTITY_EVIDENCE_DETAILS = frozenset({"cache_state"})


class BuildManifestError(ValueError):
    """A whole-build record is malformed, inconsistent, or tampered with."""


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BuildManifestError(f"{label} must be a non-empty string")
    return value


def _optional_nonempty(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _nonempty(value, label)


def _digest(value: object, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise BuildManifestError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _logical_path(value: object, label: str = "logical path") -> str:
    path = _nonempty(value, label)
    if "\\" in path or "\x00" in path:
        raise BuildManifestError(f"{label} must use safe POSIX path components")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or path.endswith("/"):
        raise BuildManifestError(f"{label} must be a relative file path")
    if any(part in {"", ".", ".."} for part in parsed.parts):
        raise BuildManifestError(f"{label} must not contain empty, '.' or '..' components")
    normalized = parsed.as_posix()
    if normalized != path:
        raise BuildManifestError(f"{label} must be normalized")
    return path


def _origin_to_data(origin: SourceOrigin | None) -> dict[str, object] | None:
    return origin.to_data() if origin is not None else None


def _origin_from_data(value: object, label: str) -> SourceOrigin | None:
    if value is None:
        return None
    try:
        return SourceOrigin.from_data(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise BuildManifestError(f"invalid {label}: {error}") from error


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise BuildManifestError(f"{label} must be an object with string keys")
    return value


def _keys(
    data: Mapping[str, object], *, required: Iterable[str], optional: Iterable[str] = (),
    label: str,
) -> None:
    required_set = frozenset(required)
    allowed = required_set | frozenset(optional)
    missing = sorted(required_set - data.keys())
    unknown = sorted(data.keys() - allowed)
    if missing:
        raise BuildManifestError(f"{label} is missing field(s): {', '.join(missing)}")
    if unknown:
        raise BuildManifestError(f"{label} has unknown field(s): {', '.join(unknown)}")


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise BuildManifestError(f"{label} must be an array")
    return value


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BuildManifestError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise BuildManifestError(f"{label} must be at least {minimum}")
    return value


def _pairs(value: object, label: str) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for index, item in enumerate(_list(value, label)):
        if not isinstance(item, list) or len(item) != 2:
            raise BuildManifestError(f"{label}[{index}] must be a two-element array")
        pairs.append((_nonempty(item[0], f"{label}[{index}] key"),
                      _nonempty(item[1], f"{label}[{index}] value")))
    return tuple(pairs)


def _normalize_pairs(
    pairs: Iterable[tuple[str, str]], label: str,
) -> tuple[tuple[str, str], ...]:
    values = tuple(sorted((_nonempty(key, f"{label} key"),
                           _nonempty(value, f"{label} value"))
                          for key, value in pairs))
    keys = tuple(key for key, _ in values)
    if len(keys) != len(set(keys)):
        raise BuildManifestError(f"{label} keys must be unique")
    return values


@dataclass(frozen=True)
class CanonicalIrRef:
    """One immutable high-level or selected canonical-IR identity."""

    phase: str
    identity: str
    schema: str
    content_hash: str | None = None
    source_origin: SourceOrigin | None = None

    def __post_init__(self) -> None:
        if self.phase not in {"high_level", "selected"}:
            raise BuildManifestError("canonical IR phase must be high_level or selected")
        _nonempty(self.identity, "canonical IR identity")
        match = _CANONICAL_IR_ID_RE.fullmatch(self.identity)
        expected_prefix = "high-level" if self.phase == "high_level" else "selected"
        if match is None or match.group(1) != expected_prefix:
            raise BuildManifestError(
                f"{self.phase} canonical IR identity must use the "
                f"'{expected_prefix}:<sha256>' form"
            )
        _nonempty(self.schema, "canonical IR schema")
        if self.schema != CANONICAL_IR_IDENTITY_SCHEMA:
            raise BuildManifestError(
                f"unsupported canonical IR identity schema '{self.schema}'"
            )
        _digest(self.content_hash, "canonical IR content hash", optional=True)

    def to_data(self) -> dict[str, object]:
        return {**self.identity_data(), "source_origin": _origin_to_data(self.source_origin)}

    def identity_data(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "identity": self.identity,
            "schema": self.schema,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_data(cls, value: object) -> "CanonicalIrRef":
        data = _object(value, "canonical IR reference")
        _keys(data, required=("phase", "identity", "schema", "content_hash", "source_origin"),
              label="canonical IR reference")
        return cls(
            _nonempty(data["phase"], "canonical IR phase"),
            _nonempty(data["identity"], "canonical IR identity"),
            _nonempty(data["schema"], "canonical IR schema"),
            _digest(data["content_hash"], "canonical IR content hash", optional=True),
            _origin_from_data(data["source_origin"], "canonical IR source origin"),
        )


@dataclass(frozen=True)
class PublishedFile:
    """A content-addressed file published at a logical, relocatable path."""

    logical_path: str
    content_hash: str
    kind: str
    size: int | None
    source_origin: SourceOrigin | None = None

    def __post_init__(self) -> None:
        _logical_path(self.logical_path)
        _digest(self.content_hash, "published-file content hash")
        _nonempty(self.kind, "published-file kind")
        if self.size is not None:
            _integer(self.size, "published-file size", minimum=0)

    @classmethod
    def from_bytes(
        cls, logical_path: str, content: bytes, *, kind: str,
        source_origin: SourceOrigin | None = None,
    ) -> "PublishedFile":
        if not isinstance(content, bytes):
            raise BuildManifestError("published file content must be bytes")
        return cls(
            logical_path,
            hashlib.sha256(content).hexdigest(),
            kind,
            len(content),
            source_origin,
        )

    @classmethod
    def from_content_identity(
        cls, logical_path: str, content_hash: str, *, kind: str,
        size: int | None = None, source_origin: SourceOrigin | None = None,
    ) -> "PublishedFile":
        """Create an entry when a dependency resolver exposes identity, not bytes."""
        return cls(logical_path, content_hash, kind, size, source_origin)

    def to_data(self) -> dict[str, object]:
        return {**self.identity_data(), "source_origin": _origin_to_data(self.source_origin)}

    def identity_data(self) -> dict[str, object]:
        return {
            "logical_path": self.logical_path,
            "content_hash": self.content_hash,
            "kind": self.kind,
            "size": self.size,
        }

    @classmethod
    def from_data(cls, value: object) -> "PublishedFile":
        data = _object(value, "published file")
        _keys(data, required=("logical_path", "content_hash", "kind", "size", "source_origin"),
              label="published file")
        return cls(
            _logical_path(data["logical_path"]),
            _digest(data["content_hash"], "published-file content hash"),  # type: ignore[arg-type]
            _nonempty(data["kind"], "published-file kind"),
            (
                None
                if data["size"] is None
                else _integer(data["size"], "published-file size", minimum=0)
            ),
            _origin_from_data(data["source_origin"], "published-file source origin"),
        )


@dataclass(frozen=True)
class BackendBuildRecord:
    """The plan and published products for one independently planned backend."""

    backend: str
    module: str
    requirement: str
    status: str
    plan_identity: str
    selected_ir_identity: str
    build_identity: str | None = None
    artifact_hash: str | None = None
    manifest_version: int | None = None
    source_map_hash: str | None = None
    implementation_graph_identity: str | None = None
    files: tuple[PublishedFile, ...] = ()
    companions: tuple[PublishedFile, ...] = ()
    source_origin: SourceOrigin | None = None

    def __post_init__(self) -> None:
        _nonempty(self.backend, "backend")
        _nonempty(self.module, "backend module")
        if self.requirement not in _BACKEND_REQUIREMENTS:
            raise BuildManifestError(f"unsupported backend requirement '{self.requirement}'")
        if self.status not in _BACKEND_STATUSES:
            raise BuildManifestError(f"unsupported backend status '{self.status}'")
        _nonempty(self.plan_identity, "backend plan identity")
        _nonempty(self.selected_ir_identity, "backend selected-IR identity")
        if re.fullmatch(r"selected:[0-9a-f]{64}", self.selected_ir_identity) is None:
            raise BuildManifestError(
                "backend selected-IR identity must use the 'selected:<sha256>' form"
            )
        _optional_nonempty(self.build_identity, "backend build identity")
        _digest(self.artifact_hash, "backend artifact hash", optional=True)
        _digest(self.source_map_hash, "backend source-map hash", optional=True)
        _optional_nonempty(self.implementation_graph_identity,
                           "implementation-graph identity")
        if self.manifest_version is not None:
            _integer(self.manifest_version, "backend manifest version", minimum=1)
        object.__setattr__(self, "files", _normalize_files(self.files, "backend files"))
        object.__setattr__(self, "companions", _normalize_files(self.companions, "companions"))
        built = self.status in {"selected", "generic_fallback"}
        if built and (self.build_identity is None or self.artifact_hash is None
                      or self.manifest_version is None or not self.files):
            raise BuildManifestError(
                f"backend status '{self.status}' requires build/artifact identity, "
                "manifest version, and at least one published file"
            )
        if built and any(item.size is None for item in (*self.files, *self.companions)):
            raise BuildManifestError("backend products require an exact published size")
        if not built and any((self.build_identity, self.artifact_hash,
                              self.manifest_version, self.files, self.companions)):
            raise BuildManifestError(
                f"backend status '{self.status}' cannot publish backend artifacts"
            )
        if self.requirement == "not_requested" and self.status != "not_requested":
            raise BuildManifestError("not_requested backend requirement requires matching status")
        _reject_duplicate_paths((*self.files, *self.companions), "backend products")
        product_hashes = {item.content_hash for item in (*self.files, *self.companions)}
        if self.artifact_hash is not None and self.artifact_hash not in product_hashes:
            raise BuildManifestError("backend artifact hash has no published file")
        if self.source_map_hash is not None and self.source_map_hash not in product_hashes:
            raise BuildManifestError("backend source-map hash has no published file")

    def to_data(self) -> dict[str, object]:
        data = self.identity_data()
        data["files"] = [item.to_data() for item in self.files]
        data["companions"] = [item.to_data() for item in self.companions]
        data["source_origin"] = _origin_to_data(self.source_origin)
        return data

    def identity_data(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "module": self.module,
            "requirement": self.requirement,
            "status": self.status,
            "plan_identity": self.plan_identity,
            "selected_ir_identity": self.selected_ir_identity,
            "build_identity": self.build_identity,
            "artifact_hash": self.artifact_hash,
            "manifest_version": self.manifest_version,
            "source_map_hash": self.source_map_hash,
            "implementation_graph_identity": self.implementation_graph_identity,
            "files": [item.identity_data() for item in self.files],
            "companions": [item.identity_data() for item in self.companions],
        }

    @classmethod
    def from_data(cls, value: object) -> "BackendBuildRecord":
        data = _object(value, "backend build record")
        required = (
            "backend", "module", "requirement", "status", "plan_identity",
            "selected_ir_identity", "build_identity", "artifact_hash", "manifest_version",
            "source_map_hash", "implementation_graph_identity", "files", "companions",
            "source_origin",
        )
        _keys(data, required=required, label="backend build record")
        version = data["manifest_version"]
        if version is not None:
            version = _integer(version, "backend manifest version", minimum=1)
        return cls(
            _nonempty(data["backend"], "backend"),
            _nonempty(data["module"], "backend module"),
            _nonempty(data["requirement"], "backend requirement"),
            _nonempty(data["status"], "backend status"),
            _nonempty(data["plan_identity"], "backend plan identity"),
            _nonempty(data["selected_ir_identity"], "backend selected-IR identity"),
            _optional_nonempty(data["build_identity"], "backend build identity"),
            _digest(data["artifact_hash"], "backend artifact hash", optional=True),
            version,
            _digest(data["source_map_hash"], "backend source-map hash", optional=True),
            _optional_nonempty(data["implementation_graph_identity"],
                               "implementation-graph identity"),
            tuple(PublishedFile.from_data(item) for item in _list(data["files"], "backend files")),
            tuple(
                PublishedFile.from_data(item)
                for item in _list(data["companions"], "companions")
            ),
            _origin_from_data(data["source_origin"], "backend source origin"),
        )


@dataclass(frozen=True)
class EvidenceRecord:
    """One truthful semantic, timing, formal, or equivalence evidence claim."""

    evidence_id: str
    claim: str
    status: str
    mode: str | None = None
    depth: int | None = None
    property_id: str | None = None
    candidate_identity: str | None = None
    backend: str | None = None
    artifact_hash: str | None = None
    reference_hash: str | None = None
    engine: str | None = None
    solver: str | None = None
    relation: str | None = None
    route: str | None = None
    counterexample_digest: str | None = None
    details: tuple[tuple[str, str], ...] = ()
    source_origin: SourceOrigin | None = None

    def __post_init__(self) -> None:
        _nonempty(self.evidence_id, "evidence ID")
        _nonempty(self.claim, "evidence claim")
        if self.status not in _EVIDENCE_STATUSES:
            raise BuildManifestError(f"unsupported evidence status '{self.status}'")
        if self.mode is not None and self.mode not in _PROOF_MODES:
            raise BuildManifestError(f"unsupported evidence proof mode '{self.mode}'")
        if self.depth is not None:
            _integer(self.depth, "evidence depth", minimum=1)
        if self.status == "bounded_pass" and (self.mode != "bmc" or self.depth is None):
            raise BuildManifestError("bounded_pass requires BMC mode and a positive depth")
        if self.status == "proven" and self.mode != "prove":
            raise BuildManifestError("proven requires unbounded PROVE mode")
        if self.status in {"witnessed", "bounded_unreached"} and (
            self.mode != "cover" or self.depth is None
        ):
            raise BuildManifestError(
                f"{self.status} requires cover mode and a positive depth"
            )
        if self.mode == "cover" and self.status not in {
            "witnessed", "bounded_unreached", "unknown", "skipped",
        }:
            raise BuildManifestError(
                f"status '{self.status}' is not valid for cover evidence"
            )
        if self.status in {"typed_legal", "timing_validated"}:
            if self.mode is not None or self.depth is not None:
                raise BuildManifestError(
                    f"{self.status} is not a proof result and cannot carry proof mode/depth"
                )
        if self.status == "not_run":
            words = set(re.findall(r"[a-z]+", self.claim.lower()))
            if words & {"proof", "proved", "proven"}:
                raise BuildManifestError("harness/property generation must not claim proof")
        _optional_nonempty(self.property_id, "evidence property ID")
        _optional_nonempty(self.candidate_identity, "evidence candidate identity")
        _optional_nonempty(self.backend, "evidence backend")
        _digest(self.artifact_hash, "evidence artifact hash", optional=True)
        _digest(self.reference_hash, "evidence reference hash", optional=True)
        _optional_nonempty(self.engine, "evidence engine")
        _optional_nonempty(self.solver, "evidence solver")
        _optional_nonempty(self.relation, "evidence relation")
        _optional_nonempty(self.route, "evidence route")
        _digest(self.counterexample_digest, "counterexample digest", optional=True)
        if (self.counterexample_digest is not None) != (self.status == "failed"):
            raise BuildManifestError(
                "failed evidence requires exactly one counterexample digest"
            )
        object.__setattr__(self, "details", _normalize_pairs(self.details, "evidence details"))

    def to_data(self) -> dict[str, object]:
        data = self.identity_data()
        data["details"] = [list(item) for item in self.details]
        data["source_origin"] = _origin_to_data(self.source_origin)
        return data

    def identity_data(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "claim": self.claim,
            "status": self.status,
            "mode": self.mode,
            "depth": self.depth,
            "property_id": self.property_id,
            "candidate_identity": self.candidate_identity,
            "backend": self.backend,
            "artifact_hash": self.artifact_hash,
            "reference_hash": self.reference_hash,
            "engine": self.engine,
            "solver": self.solver,
            "relation": self.relation,
            "route": self.route,
            "counterexample_digest": self.counterexample_digest,
            "details": [
                list(item) for item in self.details
                if item[0] not in _NON_IDENTITY_EVIDENCE_DETAILS
            ],
        }

    @classmethod
    def from_data(cls, value: object) -> "EvidenceRecord":
        data = _object(value, "evidence record")
        required = (
            "evidence_id", "claim", "status", "mode", "depth", "property_id",
            "candidate_identity", "backend", "artifact_hash", "reference_hash", "engine",
            "solver", "relation", "route", "counterexample_digest", "details",
            "source_origin",
        )
        _keys(data, required=required, label="evidence record")
        depth = data["depth"]
        if depth is not None:
            depth = _integer(depth, "evidence depth", minimum=1)
        return cls(
            _nonempty(data["evidence_id"], "evidence ID"),
            _nonempty(data["claim"], "evidence claim"),
            _nonempty(data["status"], "evidence status"),
            _optional_nonempty(data["mode"], "evidence proof mode"),
            depth,
            _optional_nonempty(data["property_id"], "evidence property ID"),
            _optional_nonempty(data["candidate_identity"], "evidence candidate identity"),
            _optional_nonempty(data["backend"], "evidence backend"),
            _digest(data["artifact_hash"], "evidence artifact hash", optional=True),
            _digest(data["reference_hash"], "evidence reference hash", optional=True),
            _optional_nonempty(data["engine"], "evidence engine"),
            _optional_nonempty(data["solver"], "evidence solver"),
            _optional_nonempty(data["relation"], "evidence relation"),
            _optional_nonempty(data["route"], "evidence route"),
            _digest(data["counterexample_digest"], "counterexample digest", optional=True),
            _pairs(data["details"], "evidence details"),
            _origin_from_data(data["source_origin"], "evidence source origin"),
        )


@dataclass(frozen=True)
class ToolExecutionRecord:
    """A normalized, reproducible tool invocation and its logical outputs."""

    execution_id: str
    role: str
    tool: str
    version: str
    argv_shape: tuple[str, ...]
    status: str
    exit_code: int | None = None
    outputs: tuple[str, ...] = ()
    mode: str | None = None
    depth: int | None = None
    source_origin: SourceOrigin | None = None

    def __post_init__(self) -> None:
        _nonempty(self.execution_id, "tool execution ID")
        _nonempty(self.role, "tool role")
        _nonempty(self.tool, "tool name")
        _nonempty(self.version, "tool version")
        if not self.argv_shape or any(
            not isinstance(item, str) or not item for item in self.argv_shape
        ):
            raise BuildManifestError("tool argv_shape must contain non-empty normalized tokens")
        if any(_argv_has_absolute_path(item) for item in self.argv_shape):
            raise BuildManifestError("tool argv_shape must not contain absolute host paths")
        if self.status not in _TOOL_STATUSES:
            raise BuildManifestError(f"unsupported tool status '{self.status}'")
        if self.exit_code is not None:
            _integer(self.exit_code, "tool exit code")
        if self.status == "passed" and self.exit_code not in {None, 0}:
            raise BuildManifestError("passed tool execution cannot have a nonzero exit code")
        if self.mode is not None and self.mode not in _PROOF_MODES:
            raise BuildManifestError(f"unsupported tool proof mode '{self.mode}'")
        if self.depth is not None:
            _integer(self.depth, "tool depth", minimum=1)
        object.__setattr__(self, "outputs", tuple(sorted(_logical_path(item, "tool output")
                                                         for item in self.outputs)))
        if len(self.outputs) != len(set(self.outputs)):
            raise BuildManifestError("tool outputs must be unique")

    def to_data(self) -> dict[str, object]:
        return {**self.identity_data(), "source_origin": _origin_to_data(self.source_origin)}

    def identity_data(self) -> dict[str, object]:
        return {
            "execution_id": self.execution_id,
            "role": self.role,
            "tool": self.tool,
            "version": self.version,
            "argv_shape": list(self.argv_shape),
            "status": self.status,
            "exit_code": self.exit_code,
            "outputs": list(self.outputs),
            "mode": self.mode,
            "depth": self.depth,
        }

    @classmethod
    def from_data(cls, value: object) -> "ToolExecutionRecord":
        data = _object(value, "tool execution record")
        required = (
            "execution_id", "role", "tool", "version", "argv_shape", "status",
            "exit_code", "outputs", "mode", "depth", "source_origin",
        )
        _keys(data, required=required, label="tool execution record")
        argv = tuple(_nonempty(item, "tool argv token")
                     for item in _list(data["argv_shape"], "tool argv_shape"))
        outputs = tuple(_logical_path(item, "tool output")
                        for item in _list(data["outputs"], "tool outputs"))
        exit_code = data["exit_code"]
        if exit_code is not None:
            exit_code = _integer(exit_code, "tool exit code")
        depth = data["depth"]
        if depth is not None:
            depth = _integer(depth, "tool depth", minimum=1)
        return cls(
            _nonempty(data["execution_id"], "tool execution ID"),
            _nonempty(data["role"], "tool role"),
            _nonempty(data["tool"], "tool name"),
            _nonempty(data["version"], "tool version"),
            argv,
            _nonempty(data["status"], "tool status"),
            exit_code,
            outputs,
            _optional_nonempty(data["mode"], "tool proof mode"),
            depth,
            _origin_from_data(data["source_origin"], "tool source origin"),
        )


@dataclass(frozen=True)
class ReportRecord:
    """A content-addressed generated report and its evidence joins."""

    report_id: str
    kind: str
    format: str
    content_hash: str
    logical_path: str | None = None
    evidence_ids: tuple[str, ...] = ()
    source_origin: SourceOrigin | None = None
    identity_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _nonempty(self.report_id, "report ID")
        _nonempty(self.kind, "report kind")
        _nonempty(self.format, "report format")
        _digest(self.content_hash, "report content hash")
        if self.logical_path is not None:
            _logical_path(self.logical_path, "report logical path")
        object.__setattr__(self, "evidence_ids", tuple(sorted(
            _nonempty(item, "report evidence ID") for item in self.evidence_ids
        )))
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise BuildManifestError("report evidence IDs must be unique")
        expected_identity = stable_digest({
            "schema": "zlang-semantic-report-identity-v1",
            "report_id": self.report_id,
            "kind": self.kind,
            "evidence_ids": list(self.evidence_ids),
        })
        object.__setattr__(self, "identity_hash", expected_identity)

    def to_data(self) -> dict[str, object]:
        return {
            "report_id": self.report_id,
            "kind": self.kind,
            "format": self.format,
            "content_hash": self.content_hash,
            "logical_path": self.logical_path,
            "evidence_ids": list(self.evidence_ids),
            "identity_hash": self.identity_hash,
            "source_origin": _origin_to_data(self.source_origin),
        }

    def identity_data(self) -> dict[str, object]:
        return {
            "identity_hash": self.identity_hash,
        }

    @classmethod
    def from_data(cls, value: object) -> "ReportRecord":
        data = _object(value, "report record")
        required = (
            "report_id", "kind", "format", "content_hash", "logical_path",
            "evidence_ids", "identity_hash", "source_origin",
        )
        _keys(data, required=required, label="report record")
        path = data["logical_path"]
        if path is not None:
            path = _logical_path(path, "report logical path")
        restored = cls(
            _nonempty(data["report_id"], "report ID"),
            _nonempty(data["kind"], "report kind"),
            _nonempty(data["format"], "report format"),
            _digest(data["content_hash"], "report content hash"),  # type: ignore[arg-type]
            path,  # type: ignore[arg-type]
            tuple(_nonempty(item, "report evidence ID")
                  for item in _list(data["evidence_ids"], "report evidence IDs")),
            _origin_from_data(data["source_origin"], "report source origin"),
        )
        supplied_identity = _digest(data["identity_hash"], "report identity hash")
        if supplied_identity != restored.identity_hash:
            raise BuildManifestError(
                "report identity hash does not match its semantic report inputs"
            )
        return restored


@dataclass(frozen=True)
class WholeBuildManifest:
    """Complete, deterministic linkage for one selected ZLang build."""

    root_source: PublishedFile
    dependency_closure: tuple[PublishedFile, ...]
    high_level_ir: CanonicalIrRef
    selected_ir: CanonicalIrRef
    implementation_request_identity: str
    implementation_policy_identity: str
    profile_identity: str | None = None
    backend_builds: tuple[BackendBuildRecord, ...] = ()
    tool_executions: tuple[ToolExecutionRecord, ...] = ()
    reports: tuple[ReportRecord, ...] = ()
    evidence: tuple[EvidenceRecord, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()
    schema_version: int = field(default=WHOLE_BUILD_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.root_source, PublishedFile):
            raise BuildManifestError("root_source must be a PublishedFile record")
        if not isinstance(self.high_level_ir, CanonicalIrRef):
            raise BuildManifestError("high_level_ir must be a CanonicalIrRef")
        if not isinstance(self.selected_ir, CanonicalIrRef):
            raise BuildManifestError("selected_ir must be a CanonicalIrRef")
        if self.root_source.size is None:
            raise BuildManifestError("root_source requires an exact published size")
        if any(not isinstance(item, BackendBuildRecord) for item in self.backend_builds):
            raise BuildManifestError("backend_builds must contain BackendBuildRecord values")
        if any(not isinstance(item, ToolExecutionRecord) for item in self.tool_executions):
            raise BuildManifestError("tool_executions must contain ToolExecutionRecord values")
        if any(not isinstance(item, ReportRecord) for item in self.reports):
            raise BuildManifestError("reports must contain ReportRecord values")
        if any(not isinstance(item, EvidenceRecord) for item in self.evidence):
            raise BuildManifestError("evidence must contain EvidenceRecord values")
        if self.high_level_ir.phase != "high_level":
            raise BuildManifestError("high_level_ir must carry a high_level reference")
        if self.selected_ir.phase != "selected":
            raise BuildManifestError("selected_ir must carry a selected reference")
        _nonempty(self.implementation_request_identity, "implementation-request identity")
        _nonempty(self.implementation_policy_identity, "implementation-policy identity")
        _optional_nonempty(self.profile_identity, "profile identity")
        dependencies = _normalize_files(self.dependency_closure, "dependency closure")
        _reject_duplicate_paths((self.root_source, *dependencies), "source/dependency closure")
        object.__setattr__(self, "dependency_closure", dependencies)
        object.__setattr__(self, "backend_builds", tuple(sorted(
            self.backend_builds,
            key=lambda item: (item.backend, item.module, item.plan_identity),
        )))
        object.__setattr__(self, "tool_executions", tuple(sorted(
            self.tool_executions, key=lambda item: item.execution_id,
        )))
        object.__setattr__(self, "reports", tuple(sorted(
            self.reports, key=lambda item: item.report_id,
        )))
        object.__setattr__(self, "evidence", tuple(sorted(
            self.evidence, key=lambda item: item.evidence_id,
        )))
        object.__setattr__(self, "metadata", _normalize_pairs(self.metadata, "manifest metadata"))
        _unique(tuple((item.backend, item.module) for item in self.backend_builds),
                "backend/module build")
        _unique(tuple(item.execution_id for item in self.tool_executions), "tool execution ID")
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        _unique(evidence_ids, "evidence ID")
        _unique(tuple(item.report_id for item in self.reports), "report ID")
        _unique(
            tuple(
                item.logical_path
                for item in self.reports
                if item.logical_path is not None
            ),
            "report logical path",
        )
        all_files = [self.root_source, *dependencies]
        for backend in self.backend_builds:
            all_files.extend(backend.files)
            all_files.extend(backend.companions)
        _reject_duplicate_paths(all_files, "whole-build published file")
        evidence_set = set(evidence_ids)
        for report in self.reports:
            missing = sorted(set(report.evidence_ids) - evidence_set)
            if missing:
                raise BuildManifestError(
                    f"report '{report.report_id}' references unknown evidence: {', '.join(missing)}"
                )
        published = _published_paths(self)
        published_records = {item.logical_path: item for item in all_files}
        for execution in self.tool_executions:
            missing = sorted(set(execution.outputs) - published)
            if missing:
                raise BuildManifestError(
                    f"tool execution '{execution.execution_id}' references unpublished outputs: "
                    + ", ".join(missing)
                )
        for report in self.reports:
            if (
                report.logical_path is not None
                and report.logical_path in published_records
                and published_records[report.logical_path].content_hash != report.content_hash
            ):
                raise BuildManifestError(
                    f"report '{report.report_id}' hash differs from its published file"
                )

    @property
    def build_identity(self) -> str:
        return stable_digest(self.identity_data())

    def identity_data(self) -> dict[str, object]:
        """Return the complete semantic/product identity without provenance."""
        return {
            "schema": WHOLE_BUILD_SCHEMA,
            "schema_version": self.schema_version,
            "root_source": self.root_source.identity_data(),
            "dependency_closure": [item.identity_data() for item in self.dependency_closure],
            "high_level_ir": self.high_level_ir.identity_data(),
            "selected_ir": self.selected_ir.identity_data(),
            "implementation_request_identity": self.implementation_request_identity,
            "implementation_policy_identity": self.implementation_policy_identity,
            "profile_identity": self.profile_identity,
            "backend_builds": [item.identity_data() for item in self.backend_builds],
            "tool_executions": [item.identity_data() for item in self.tool_executions],
            "reports": [item.identity_data() for item in self.reports],
            "evidence": [item.identity_data() for item in self.evidence],
            "metadata": [list(item) for item in self.metadata],
        }

    def to_data(self) -> dict[str, object]:
        return {
            "schema": WHOLE_BUILD_SCHEMA,
            "schema_version": self.schema_version,
            "build_identity": self.build_identity,
            "root_source": self.root_source.to_data(),
            "dependency_closure": [item.to_data() for item in self.dependency_closure],
            "high_level_ir": self.high_level_ir.to_data(),
            "selected_ir": self.selected_ir.to_data(),
            "implementation_request_identity": self.implementation_request_identity,
            "implementation_policy_identity": self.implementation_policy_identity,
            "profile_identity": self.profile_identity,
            "backend_builds": [item.to_data() for item in self.backend_builds],
            "tool_executions": [item.to_data() for item in self.tool_executions],
            "reports": [item.to_data() for item in self.reports],
            "evidence": [item.to_data() for item in self.evidence],
            "metadata": [list(item) for item in self.metadata],
        }

    def to_json(self) -> str:
        return stable_json(self.to_data(), indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "WholeBuildManifest":
        if not isinstance(text, str):
            raise BuildManifestError("whole-build manifest JSON must be text")
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise BuildManifestError(f"invalid whole-build manifest JSON: {error.msg}") from error
        data = _object(value, "whole-build manifest")
        required = (
            "schema", "schema_version", "build_identity", "root_source",
            "dependency_closure", "high_level_ir", "selected_ir",
            "implementation_request_identity", "implementation_policy_identity",
            "profile_identity", "backend_builds", "tool_executions", "reports",
            "evidence", "metadata",
        )
        _keys(data, required=required, label="whole-build manifest")
        if data["schema"] != WHOLE_BUILD_SCHEMA:
            raise BuildManifestError(f"unsupported whole-build schema '{data['schema']}'")
        restored_version = _integer(
            data["schema_version"], "whole-build schema version", minimum=1
        )
        if restored_version != WHOLE_BUILD_SCHEMA_VERSION:
            raise BuildManifestError(
                f"unsupported whole-build schema version {data['schema_version']}"
            )
        expected_identity = _digest(data["build_identity"], "whole-build identity")
        manifest = cls(
            PublishedFile.from_data(data["root_source"]),
            tuple(PublishedFile.from_data(item)
                  for item in _list(data["dependency_closure"], "dependency closure")),
            CanonicalIrRef.from_data(data["high_level_ir"]),
            CanonicalIrRef.from_data(data["selected_ir"]),
            _nonempty(data["implementation_request_identity"],
                      "implementation-request identity"),
            _nonempty(data["implementation_policy_identity"],
                      "implementation-policy identity"),
            _optional_nonempty(data["profile_identity"], "profile identity"),
            tuple(BackendBuildRecord.from_data(item)
                  for item in _list(data["backend_builds"], "backend builds")),
            tuple(ToolExecutionRecord.from_data(item)
                  for item in _list(data["tool_executions"], "tool executions")),
            tuple(ReportRecord.from_data(item)
                  for item in _list(data["reports"], "reports")),
            tuple(EvidenceRecord.from_data(item)
                  for item in _list(data["evidence"], "evidence")),
            _pairs(data["metadata"], "manifest metadata"),
        )
        if manifest.build_identity != expected_identity:
            raise BuildManifestError(
                "whole-build manifest identity does not match its deterministic contents"
            )
        return manifest


def validate_published_files(files: Iterable[PublishedFile], root: Path) -> None:
    """Validate logical files below ``root`` without serializing the host path."""
    root_path = Path(root).resolve()
    for published in _normalize_files(files, "published files"):
        candidate = root_path.joinpath(*PurePosixPath(published.logical_path).parts)
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as error:
            raise BuildManifestError(
                f"published file is missing: {published.logical_path}"
            ) from error
        if not resolved.is_relative_to(root_path):
            raise BuildManifestError(
                f"published file escapes publication root: {published.logical_path}"
            )
        if not resolved.is_file():
            raise BuildManifestError(
                f"published path is not a regular file: {published.logical_path}"
            )
        content = resolved.read_bytes()
        actual_hash = hashlib.sha256(content).hexdigest()
        if (
            (published.size is not None and len(content) != published.size)
            or actual_hash != published.content_hash
        ):
            raise BuildManifestError(
                f"published file hash/size mismatch: {published.logical_path}"
            )


def validate_manifest_files(manifest: WholeBuildManifest, root: Path) -> None:
    """Validate every source, dependency, backend product, companion and report."""
    files: list[PublishedFile] = [manifest.root_source, *manifest.dependency_closure]
    for backend in manifest.backend_builds:
        files.extend(backend.files)
        files.extend(backend.companions)
    by_path = {item.logical_path: item for item in files}
    for report in manifest.reports:
        if report.logical_path is not None:
            published = by_path.get(report.logical_path)
            if published is not None and published.content_hash != report.content_hash:
                raise BuildManifestError(
                    f"report hash differs from published file: {report.logical_path}"
                )
    validate_published_files(files, root)
    for report in manifest.reports:
        if report.logical_path is not None and report.logical_path not in by_path:
            _validate_staged_hash(
                Path(root), report.logical_path, report.content_hash, label="report"
            )


def validate_published_file_map(
    files: Iterable[PublishedFile], paths: Mapping[str, Path],
) -> None:
    """Validate content identities against an explicit logical-to-physical map.

    This is the input-side counterpart of :func:`validate_published_files`.
    Physical source/dependency paths may live anywhere; only logical paths enter
    the manifest and its identity.
    """
    normalized = _normalize_files(files, "published files")
    if any(not isinstance(key, str) for key in paths):
        raise BuildManifestError("physical path map keys must be logical path strings")
    expected = {item.logical_path for item in normalized}
    actual = set(paths)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise BuildManifestError("physical path map is missing: " + ", ".join(missing))
    if unknown:
        raise BuildManifestError("physical path map has unknown entries: " + ", ".join(unknown))
    for published in normalized:
        path = Path(paths[published.logical_path])
        if not path.exists():
            raise BuildManifestError(f"published file is missing: {published.logical_path}")
        if not path.is_file():
            raise BuildManifestError(
                f"published path is not a regular file: {published.logical_path}"
            )
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != published.content_hash or (
            published.size is not None and len(content) != published.size
        ):
            raise BuildManifestError(
                f"published file hash/size mismatch: {published.logical_path}"
            )


def validate_manifest_file_map(
    manifest: WholeBuildManifest, paths: Mapping[str, Path],
) -> None:
    """Validate every whole-build path against an explicit physical map."""
    files: list[PublishedFile] = [manifest.root_source, *manifest.dependency_closure]
    for backend in manifest.backend_builds:
        files.extend(backend.files)
        files.extend(backend.companions)
    by_path = {item.logical_path: item for item in files}
    expected = set(by_path)
    expected.update(
        report.logical_path for report in manifest.reports
        if report.logical_path is not None
    )
    actual = set(paths)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise BuildManifestError("physical path map is missing: " + ", ".join(missing))
    if unknown:
        raise BuildManifestError("physical path map has unknown entries: " + ", ".join(unknown))
    validate_published_file_map(files, {path: paths[path] for path in by_path})
    for report in manifest.reports:
        if report.logical_path is None:
            continue
        content_hash = hashlib.sha256(Path(paths[report.logical_path]).read_bytes()).hexdigest()
        if content_hash != report.content_hash:
            raise BuildManifestError(
                f"report hash differs from physical file: {report.logical_path}"
            )


def _normalize_files(files: Iterable[PublishedFile], label: str) -> tuple[PublishedFile, ...]:
    values = tuple(files)
    if any(not isinstance(item, PublishedFile) for item in values):
        raise BuildManifestError(f"{label} must contain PublishedFile records")
    result = tuple(sorted(
        values,
        key=lambda item: (item.logical_path, item.content_hash, item.kind),
    ))
    _reject_duplicate_paths(result, label)
    return result


def _reject_duplicate_paths(files: Iterable[PublishedFile], label: str) -> None:
    paths = tuple(item.logical_path for item in files)
    if len(paths) != len(set(paths)):
        duplicate = next(path for path in paths if paths.count(path) > 1)
        raise BuildManifestError(f"duplicate {label} logical path: {duplicate}")


def _unique(values: tuple[object, ...], label: str) -> None:
    if len(values) != len(set(values)):
        duplicate = next(value for value in values if values.count(value) > 1)
        raise BuildManifestError(f"duplicate {label}: {duplicate}")


def _published_paths(manifest: WholeBuildManifest) -> set[str]:
    paths = {manifest.root_source.logical_path}
    paths.update(item.logical_path for item in manifest.dependency_closure)
    for backend in manifest.backend_builds:
        paths.update(item.logical_path for item in backend.files)
        paths.update(item.logical_path for item in backend.companions)
    paths.update(
        item.logical_path for item in manifest.reports
        if item.logical_path is not None
    )
    return paths


def _validate_staged_hash(root: Path, logical_path: str, expected_hash: str, *, label: str) -> None:
    root_path = root.resolve()
    candidate = root_path.joinpath(*PurePosixPath(logical_path).parts)
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise BuildManifestError(f"{label} is missing: {logical_path}") from error
    if not resolved.is_relative_to(root_path):
        raise BuildManifestError(f"{label} escapes publication root: {logical_path}")
    if not resolved.is_file():
        raise BuildManifestError(f"{label} path is not a regular file: {logical_path}")
    if hashlib.sha256(resolved.read_bytes()).hexdigest() != expected_hash:
        raise BuildManifestError(f"{label} hash mismatch: {logical_path}")


def _argv_has_absolute_path(token: str) -> bool:
    if Path(token).is_absolute() or token.startswith("file://"):
        return True
    if "=" in token:
        _, value = token.split("=", 1)
        return Path(value).is_absolute() or value.startswith("file://")
    return re.match(r"^[A-Za-z]:[/\\]", token) is not None


__all__ = [
    "BackendBuildRecord",
    "BuildManifestError",
    "CanonicalIrRef",
    "EvidenceRecord",
    "PublishedFile",
    "ReportRecord",
    "ToolExecutionRecord",
    "WHOLE_BUILD_SCHEMA",
    "WHOLE_BUILD_SCHEMA_VERSION",
    "WholeBuildManifest",
    "validate_manifest_files",
    "validate_manifest_file_map",
    "validate_published_file_map",
    "validate_published_files",
]
