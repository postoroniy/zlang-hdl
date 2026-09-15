"""Validated, relocatable publication bundles for generated RTL provenance.

The bundle is deliberately smaller than a whole-build manifest.  It packages
one already-emitted :class:`BackendArtifact`, its exact generated text, its
exact :class:`GeneratedSourceMap`, and the complete compiler-owned source
snapshot identities needed to reject stale navigation.  Loading never invokes
the compiler or a backend.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import stat
from typing import Iterable, Mapping

from zlang.backend.manifest import BackendArtifact
from zlang.backend.publication import (
    SafePublicationError,
    publish_relative_files,
    validate_relative_hashes,
)
from zlang.backend.source_map import GeneratedSourceMap
from zlang.build_manifest import BuildManifestError, PublishedFile
from zlang.common import stable_digest, stable_json


GENERATED_NAVIGATION_BUNDLE_SCHEMA = "zlang-generated-navigation-bundle-v1"
GENERATED_NAVIGATION_BUNDLE_SCHEMA_VERSION = 1

_DIGEST = re.compile(r"[0-9a-f]{64}")
_GENERATED_PATH = "generated/design.sv"
_SOURCE_MAP_PATH = "generated/source-map.json"
_BACKEND_MANIFEST_PATH = "manifest/backend-artifact.json"
_MANIFEST_PATH = "manifest.json"


class GeneratedNavigationBundleError(ValueError):
    """A generated-navigation publication is missing, malformed, or stale."""


class SourceSnapshotStatus(str, Enum):
    """Result of comparing a current source snapshot with bundle provenance."""

    MATCH = "match"
    STALE = "stale"
    UNKNOWN_SOURCE = "unknown_source"


def _require_string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise GeneratedNavigationBundleError(
            f"{description} must be a non-empty string"
        )
    if any(ord(character) < 32 for character in value):
        raise GeneratedNavigationBundleError(
            f"{description} must not contain control characters"
        )
    return value


def _require_digest(value: object, description: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise GeneratedNavigationBundleError(
            f"{description} must be a lowercase SHA-256 digest"
        )
    return value


def _require_mapping(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise GeneratedNavigationBundleError(
            f"{description} must be an object with string keys"
        )
    return value


def _require_keys(
    data: Mapping[str, object], *, required: Iterable[str], description: str
) -> None:
    expected = frozenset(required)
    missing = sorted(expected - data.keys())
    unknown = sorted(data.keys() - expected)
    if missing:
        raise GeneratedNavigationBundleError(
            f"{description} is missing field(s): {', '.join(missing)}"
        )
    if unknown:
        raise GeneratedNavigationBundleError(
            f"{description} has unknown field(s): {', '.join(unknown)}"
        )


@dataclass(frozen=True, order=True)
class GeneratedSourceSnapshot:
    """One logical source unit and the exact UTF-8 snapshot used to build."""

    role: str
    source_unit: str
    digest: str

    def __post_init__(self) -> None:
        if self.role not in {"root", "dependency"}:
            raise GeneratedNavigationBundleError(
                "generated source snapshot role must be root or dependency"
            )
        _require_string(self.source_unit, "generated source unit")
        _require_digest(self.digest, "generated source digest")

    def to_data(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "role": self.role,
            "source_unit": self.source_unit,
        }

    @classmethod
    def from_data(cls, value: object) -> "GeneratedSourceSnapshot":
        data = _require_mapping(value, "generated source snapshot")
        _require_keys(
            data,
            required=("digest", "role", "source_unit"),
            description="generated source snapshot",
        )
        return cls(
            _require_string(data["role"], "generated source snapshot role"),
            _require_string(data["source_unit"], "generated source unit"),
            _require_digest(data["digest"], "generated source digest"),
        )


@dataclass(frozen=True)
class GeneratedNavigationBundleManifest:
    """Versioned manifest joining explicit relative files and source inputs."""

    generated_artifact: PublishedFile
    source_map: PublishedFile
    backend_manifest: PublishedFile
    sources: tuple[GeneratedSourceSnapshot, ...]
    schema_version: int = GENERATED_NAVIGATION_BUNDLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != GENERATED_NAVIGATION_BUNDLE_SCHEMA_VERSION:
            raise GeneratedNavigationBundleError(
                "unsupported generated-navigation bundle schema version: "
                f"{self.schema_version}"
            )
        expected_kinds = (
            (self.generated_artifact, "direct_systemverilog"),
            (self.source_map, "generated_source_map"),
            (self.backend_manifest, "backend_artifact_manifest"),
        )
        for record, kind in expected_kinds:
            if not isinstance(record, PublishedFile) or record.kind != kind:
                raise GeneratedNavigationBundleError(
                    f"generated-navigation {kind} record is invalid"
                )
            if record.size is None:
                raise GeneratedNavigationBundleError(
                    f"generated-navigation {kind} size is unavailable"
                )
            if record.source_origin is not None:
                raise GeneratedNavigationBundleError(
                    f"generated-navigation {kind} file record cannot carry a source origin"
                )
        paths = tuple(record.logical_path for record, _ in expected_kinds)
        if len(paths) != len(set(paths)):
            raise GeneratedNavigationBundleError(
                "generated-navigation bundle file paths must be unique"
            )
        if any(not isinstance(item, GeneratedSourceSnapshot) for item in self.sources):
            raise GeneratedNavigationBundleError(
                "generated source snapshots must contain source snapshot records"
            )
        ordered = tuple(
            sorted(
                self.sources,
                key=lambda item: (item.role != "root", item.source_unit, item.digest),
            )
        )
        if ordered != self.sources:
            raise GeneratedNavigationBundleError(
                "generated source snapshots are not canonical"
            )
        if len(tuple(item for item in ordered if item.role == "root")) != 1:
            raise GeneratedNavigationBundleError(
                "generated-navigation bundle requires exactly one root source snapshot"
            )
        units = tuple(item.source_unit for item in ordered)
        if len(units) != len(set(units)):
            raise GeneratedNavigationBundleError(
                "generated source snapshot units must be unique"
            )

    @property
    def bundle_identity(self) -> str:
        return stable_digest(self.identity_data())

    def identity_data(self) -> dict[str, object]:
        return {
            "schema": GENERATED_NAVIGATION_BUNDLE_SCHEMA,
            "schema_version": self.schema_version,
            "backend_manifest": self.backend_manifest.identity_data(),
            "generated_artifact": self.generated_artifact.identity_data(),
            "source_map": self.source_map.identity_data(),
            "sources": [item.to_data() for item in self.sources],
        }

    def to_data(self) -> dict[str, object]:
        return {
            **self.identity_data(),
            "bundle_identity": self.bundle_identity,
            "backend_manifest": self.backend_manifest.to_data(),
            "generated_artifact": self.generated_artifact.to_data(),
            "source_map": self.source_map.to_data(),
        }

    def to_json(self) -> str:
        return stable_json(self.to_data(), indent=2) + "\n"

    @classmethod
    def from_json(cls, payload: str | bytes) -> "GeneratedNavigationBundleManifest":
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise GeneratedNavigationBundleError(
                "invalid generated-navigation bundle manifest JSON"
            ) from error
        data = _require_mapping(decoded, "generated-navigation bundle manifest")
        _require_keys(
            data,
            required=(
                "schema",
                "schema_version",
                "bundle_identity",
                "backend_manifest",
                "generated_artifact",
                "source_map",
                "sources",
            ),
            description="generated-navigation bundle manifest",
        )
        if data["schema"] != GENERATED_NAVIGATION_BUNDLE_SCHEMA:
            raise GeneratedNavigationBundleError(
                f"unsupported generated-navigation bundle schema: {data['schema']!r}"
            )
        version = data["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise GeneratedNavigationBundleError(
                "generated-navigation bundle schema version must be an integer"
            )
        raw_sources = data["sources"]
        if not isinstance(raw_sources, list):
            raise GeneratedNavigationBundleError(
                "generated source snapshots must be an array"
            )
        try:
            manifest = cls(
                PublishedFile.from_data(data["generated_artifact"]),
                PublishedFile.from_data(data["source_map"]),
                PublishedFile.from_data(data["backend_manifest"]),
                tuple(GeneratedSourceSnapshot.from_data(item) for item in raw_sources),
                version,
            )
        except BuildManifestError as error:
            raise GeneratedNavigationBundleError(str(error)) from error
        encoded_identity = _require_digest(
            data["bundle_identity"], "generated-navigation bundle identity"
        )
        if encoded_identity != manifest.bundle_identity:
            raise GeneratedNavigationBundleError(
                "generated-navigation bundle identity does not match its contents"
            )
        return manifest


@dataclass(frozen=True)
class ValidatedGeneratedNavigationBundle:
    """A fully validated immutable artifact bundle."""

    publication_root: Path
    manifest: GeneratedNavigationBundleManifest
    artifact: BackendArtifact
    source_map: GeneratedSourceMap
    generated_path: Path
    generated_text: str

    def source_digest_status(
        self, source_unit: str, current_digest: str
    ) -> SourceSnapshotStatus:
        """Compare one current digest without treating an unknown source as valid."""

        _require_string(source_unit, "source unit")
        _require_digest(current_digest, "current source digest")
        expected = next(
            (
                item.digest
                for item in self.manifest.sources
                if item.source_unit == source_unit
            ),
            None,
        )
        if expected is None:
            return SourceSnapshotStatus.UNKNOWN_SOURCE
        return (
            SourceSnapshotStatus.MATCH
            if current_digest == expected
            else SourceSnapshotStatus.STALE
        )

    def source_status(
        self, source_unit: str, current_text: str
    ) -> SourceSnapshotStatus:
        """Compare current UTF-8 source text with its producing snapshot."""

        if not isinstance(current_text, str):
            raise GeneratedNavigationBundleError("current source snapshot must be text")
        return self.source_digest_status(
            source_unit,
            hashlib.sha256(current_text.encode("utf-8")).hexdigest(),
        )


def source_snapshots_for_artifact(
    artifact: BackendArtifact,
    *,
    root_source_unit: str,
    root_source_digest: str,
) -> tuple[GeneratedSourceSnapshot, ...]:
    """Project the complete compiler-owned root/dependency snapshot identities."""

    root_unit = _require_string(root_source_unit, "root source unit")
    root_digest = _require_digest(root_source_digest, "root source digest")
    if artifact.root_module_identity is not None and (
        artifact.root_module_identity.logical_path != root_unit
        or artifact.root_module_identity.digest != root_digest
    ):
        raise GeneratedNavigationBundleError(
            "root source snapshot does not match the backend artifact"
        )

    dependencies: dict[str, str] = {}

    def add(source_unit: str, digest: str) -> None:
        unit = _require_string(source_unit, "dependency source unit")
        value = _require_digest(digest, "dependency source digest")
        if unit == root_unit:
            if value != root_digest:
                raise GeneratedNavigationBundleError(
                    "dependency metadata conflicts with the root source snapshot"
                )
            return
        previous = dependencies.get(unit)
        if previous is not None and previous != value:
            raise GeneratedNavigationBundleError(
                f"conflicting source snapshot identity for '{unit}'"
            )
        dependencies[unit] = value

    if artifact.dependency_closure is not None:
        for item in artifact.dependency_closure.modules:
            add(item.logical_path, item.digest)
    for source_unit, digest in artifact.library_dependencies:
        add(source_unit, digest)

    return (
        GeneratedSourceSnapshot("root", root_unit, root_digest),
        *(
            GeneratedSourceSnapshot("dependency", source_unit, digest)
            for source_unit, digest in sorted(dependencies.items())
        ),
    )


def _validate_lineage(
    manifest: GeneratedNavigationBundleManifest,
    artifact: BackendArtifact,
    source_map: GeneratedSourceMap,
) -> None:
    if artifact.backend != "direct_systemverilog":
        raise GeneratedNavigationBundleError(
            "generated-navigation bundle requires the direct SystemVerilog backend"
        )
    if manifest.generated_artifact.content_hash != artifact.artifact_hash:
        raise GeneratedNavigationBundleError(
            "generated artifact hash does not match the backend manifest"
        )
    if (
        source_map.backend != artifact.backend
        or source_map.module != artifact.module
        or source_map.selected_ir_identity != artifact.selected_ir_identity
        or source_map.artifact_hash != artifact.artifact_hash
    ):
        raise GeneratedNavigationBundleError(
            "generated source map does not match backend/module/selected-IR artifact lineage"
        )

    snapshots = {item.source_unit: item.digest for item in manifest.sources}
    expected = source_snapshots_for_artifact(
        artifact,
        root_source_unit=manifest.sources[0].source_unit,
        root_source_digest=manifest.sources[0].digest,
    )
    if expected != manifest.sources:
        raise GeneratedNavigationBundleError(
            "generated source snapshots do not match backend dependency provenance"
        )
    for entry in source_map.entries:
        origin = entry.source_origin
        if origin.source_unit is None or origin.digest is None:
            raise GeneratedNavigationBundleError(
                "generated source-map entry has incomplete source snapshot identity"
            )
        if snapshots.get(origin.source_unit) != origin.digest:
            raise GeneratedNavigationBundleError(
                "generated source-map entry does not match producing source snapshots"
            )
    for source_identity, source_hash in (
        *((item.source_identity, item.source_hash) for item in artifact.components),
        *((item.source_identity, item.source_hash) for item in artifact.instances),
    ):
        if snapshots.get(source_identity) != source_hash:
            raise GeneratedNavigationBundleError(
                "recursive backend provenance does not match producing source snapshots"
            )


def publish_generated_navigation_bundle(
    directory: Path,
    *,
    artifact: BackendArtifact,
    source_map: GeneratedSourceMap,
    generated_path: Path,
    root_source_unit: str,
    root_source_digest: str,
) -> GeneratedNavigationBundleManifest:
    """Publish one immutable bundle from an already-written generated file."""

    try:
        generated_content = Path(generated_path).read_bytes()
    except OSError as error:
        raise GeneratedNavigationBundleError(
            f"cannot read published generated artifact: {error}"
        ) from error
    if hashlib.sha256(generated_content).hexdigest() != artifact.artifact_hash:
        raise GeneratedNavigationBundleError(
            "published generated artifact does not match the backend artifact hash"
        )
    try:
        generated_content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GeneratedNavigationBundleError(
            "published generated artifact is not UTF-8"
        ) from error

    source_map_content = source_map.to_json().encode("utf-8")
    backend_manifest_content = artifact.to_json().encode("utf-8")
    manifest = GeneratedNavigationBundleManifest(
        PublishedFile.from_bytes(
            _GENERATED_PATH, generated_content, kind="direct_systemverilog"
        ),
        PublishedFile.from_bytes(
            _SOURCE_MAP_PATH, source_map_content, kind="generated_source_map"
        ),
        PublishedFile.from_bytes(
            _BACKEND_MANIFEST_PATH,
            backend_manifest_content,
            kind="backend_artifact_manifest",
        ),
        source_snapshots_for_artifact(
            artifact,
            root_source_unit=root_source_unit,
            root_source_digest=root_source_digest,
        ),
    )
    _validate_lineage(manifest, artifact, source_map)
    files = (
        (Path(manifest.generated_artifact.logical_path), generated_content),
        (Path(manifest.source_map.logical_path), source_map_content),
        (Path(manifest.backend_manifest.logical_path), backend_manifest_content),
        (Path(_MANIFEST_PATH), manifest.to_json().encode("utf-8")),
    )
    try:
        publish_relative_files(Path(directory), files, existing="identical")
    except SafePublicationError as error:
        raise GeneratedNavigationBundleError(str(error)) from error
    return manifest


def _read_record(root: Path, record: PublishedFile) -> bytes:
    candidate = root.joinpath(*Path(record.logical_path).parts)
    try:
        metadata = candidate.lstat()
        content = candidate.read_bytes()
    except FileNotFoundError as error:
        raise GeneratedNavigationBundleError(
            f"generated-navigation bundle file is missing: {record.logical_path}"
        ) from error
    except OSError as error:
        raise GeneratedNavigationBundleError(
            f"cannot read generated-navigation bundle file '{record.logical_path}': {error}"
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise GeneratedNavigationBundleError(
            f"generated-navigation bundle path is not a regular file: {record.logical_path}"
        )
    if len(content) != record.size:
        raise GeneratedNavigationBundleError(
            f"generated-navigation bundle file has the wrong size: {record.logical_path}"
        )
    if hashlib.sha256(content).hexdigest() != record.content_hash:
        raise GeneratedNavigationBundleError(
            f"generated-navigation bundle file hash mismatch: {record.logical_path}"
        )
    return content


def load_generated_navigation_bundle(
    directory: Path,
) -> ValidatedGeneratedNavigationBundle:
    """Load and fully validate one explicit bundle without compiling source."""

    root = Path(directory)
    manifest_path = root / _MANIFEST_PATH
    try:
        metadata = manifest_path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise GeneratedNavigationBundleError(
                "generated-navigation manifest must be a regular file"
            )
        manifest = GeneratedNavigationBundleManifest.from_json(
            manifest_path.read_bytes()
        )
    except FileNotFoundError as error:
        raise GeneratedNavigationBundleError(
            "generated-navigation bundle is missing manifest.json"
        ) from error
    except OSError as error:
        raise GeneratedNavigationBundleError(
            f"cannot read generated-navigation manifest: {error}"
        ) from error

    records = (
        manifest.generated_artifact,
        manifest.source_map,
        manifest.backend_manifest,
    )
    try:
        validate_relative_hashes(
            root,
            (
                (Path(record.logical_path), record.content_hash)
                for record in records
            ),
        )
    except SafePublicationError as error:
        raise GeneratedNavigationBundleError(str(error)) from error

    generated_content = _read_record(root, manifest.generated_artifact)
    source_map_content = _read_record(root, manifest.source_map)
    backend_manifest_content = _read_record(root, manifest.backend_manifest)
    try:
        generated_text = generated_content.decode("utf-8")
        source_map_text = source_map_content.decode("utf-8")
        backend_manifest_text = backend_manifest_content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GeneratedNavigationBundleError(
            "generated-navigation bundle contains non-UTF-8 content"
        ) from error
    try:
        artifact = BackendArtifact.from_json(backend_manifest_text)
        source_map = GeneratedSourceMap.from_json(source_map_text)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise GeneratedNavigationBundleError(
            f"invalid generated-navigation child metadata: {error}"
        ) from error
    if artifact.to_json().encode("utf-8") != backend_manifest_content:
        raise GeneratedNavigationBundleError(
            "backend artifact manifest is not in canonical form"
        )
    if source_map.to_json().encode("utf-8") != source_map_content:
        raise GeneratedNavigationBundleError(
            "generated source map is not in canonical form"
        )
    artifact = replace(artifact, text=generated_text)
    _validate_lineage(manifest, artifact, source_map)
    return ValidatedGeneratedNavigationBundle(
        root,
        manifest,
        artifact,
        source_map,
        root / manifest.generated_artifact.logical_path,
        generated_text,
    )


__all__ = [
    "GENERATED_NAVIGATION_BUNDLE_SCHEMA",
    "GENERATED_NAVIGATION_BUNDLE_SCHEMA_VERSION",
    "GeneratedNavigationBundleError",
    "GeneratedNavigationBundleManifest",
    "GeneratedSourceSnapshot",
    "SourceSnapshotStatus",
    "ValidatedGeneratedNavigationBundle",
    "load_generated_navigation_bundle",
    "publish_generated_navigation_bundle",
    "source_snapshots_for_artifact",
]
