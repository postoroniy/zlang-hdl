"""Filesystem publication helpers for deterministic whole-build manifests.

The public data model lives in :mod:`zlang.build_manifest`.  This module is the
small, compiler-facing adapter which hashes files that were *actually* written,
assigns relocatable logical roles, and publishes the final manifest atomically.
It deliberately does not run a backend or a proof engine.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable, Mapping

from zlang.backend.manifest import BackendArtifact
from zlang.build_manifest import (
    BackendBuildRecord,
    BuildManifestError,
    PublishedFile,
    ReportRecord,
    WholeBuildManifest,
    validate_manifest_file_map,
)
from zlang.common import stable_digest
from zlang.dependencies import DependencyClosure
from zlang.implementation_plans import BackendImplementationPlan


_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class PhysicalPublication:
    """One relocatable manifest file record and its host-only location."""

    record: PublishedFile
    physical_path: Path


def published_file_from_path(
    logical_path: str,
    physical_path: Path,
    *,
    kind: str,
) -> PhysicalPublication:
    """Hash one existing regular file without serializing its physical path."""

    path = Path(physical_path)
    if not path.is_file():
        raise BuildManifestError(f"published file is missing: {logical_path}")
    return PhysicalPublication(
        PublishedFile.from_bytes(logical_path, path.read_bytes(), kind=kind),
        path,
    )


def source_publication(
    source_path: Path, *, compiled_bytes: bytes | None = None,
) -> PhysicalPublication:
    """Publish the bytes that were compiled, then revalidate the input path.

    Using a fixed logical root name makes checkout relocation neutral.  Passing
    ``compiled_bytes`` closes the mutation window between the compiler's input
    read and final manifest publication: a concurrently changed source fails
    the physical-map validation instead of being attributed to the old IR.
    """

    if compiled_bytes is None:
        return published_file_from_path(
            "sources/root.zl", source_path, kind="zlang_source"
        )
    return PhysicalPublication(
        PublishedFile.from_bytes(
            "sources/root.zl", compiled_bytes, kind="zlang_source"
        ),
        Path(source_path),
    )


def dependency_records(
    closure: DependencyClosure | None,
    *,
    library_dependencies: Iterable[tuple[str, str]] = (),
) -> tuple[PublishedFile, ...]:
    """Convert validated project and compiler-library inputs into identities.

    The workspace resolver validates the bytes before semantic analysis.  Its
    portable IR deliberately exposes hashes rather than cache/check-out paths,
    so dependency sizes remain unknown here.  Compiler-shipped ``std.*``
    modules are resolved and hashed by the standard-library resolver and are
    recorded alongside locked project dependencies without exposing their
    installation paths.
    """

    records: dict[str, PublishedFile] = {}

    def add(logical_module: str, digest: str, *, kind: str) -> None:
        logical_path = (
            "dependencies/" + logical_module.replace(".", "/") + ".zl"
        )
        record = PublishedFile.from_content_identity(
            logical_path, digest, kind=kind,
        )
        previous = records.get(logical_path)
        if previous is not None:
            if previous.content_hash != record.content_hash:
                raise BuildManifestError(
                    f"conflicting dependency publication '{logical_module}'"
                )
            # Resolved-import metadata includes project imports as well as
            # compiler-shipped stdlib modules.  The locked closure is the more
            # specific classification for the same project module/hash.
            return
        records[logical_path] = record

    if closure is not None:
        for item in closure.modules:
            add(item.logical_path, item.digest, kind="zlang_dependency")
    for logical_module, digest in library_dependencies:
        add(logical_module, digest, kind="zlang_stdlib_dependency")
    return tuple(records[path] for path in sorted(records))


def backend_logical_path(backend: str, module: str, suffix: str) -> str:
    safe_module = _SAFE_COMPONENT.sub("_", module).strip("._") or "Top"
    safe_suffix = _SAFE_COMPONENT.sub("_", suffix).strip("._")
    if not safe_suffix:
        raise BuildManifestError("backend publication suffix must not be empty")
    return f"backends/{backend}/{safe_module}.{safe_suffix}"


def backend_build_record(
    *,
    artifact: BackendArtifact,
    plan: BackendImplementationPlan,
    publications: Iterable[PhysicalPublication],
    companions: Iterable[PhysicalPublication] = (),
    selected_ir_identity: str,
    implementation_graph_identity: str | None = None,
    source_map_hash: str | None = None,
) -> BackendBuildRecord:
    """Join a typed plan and exact backend artifact to files already written."""

    files = tuple(item.record for item in publications)
    companion_files = tuple(item.record for item in companions)
    if artifact.selected_ir_identity != selected_ir_identity:
        raise BuildManifestError(
            f"{artifact.backend} artifact selected-IR identity does not match the build"
        )
    if not files:
        raise BuildManifestError(
            f"{artifact.backend} backend build requires a published source or RTL file"
        )
    status = plan.status.value
    if status not in {"selected", "generic_fallback"}:
        # A bare CLI output is an explicit publication request even when no
        # profile selected a backend.  Keep the pre-existing planning result as
        # identity input, but state truthfully that its generic route was used.
        status = "generic_fallback" if plan.graph is not None and plan.graph.is_generic else "selected"
    requirement = plan.requirement.value
    if requirement == "not_requested":
        requirement = "preferred"
    return BackendBuildRecord(
        backend=artifact.backend,
        module=artifact.module,
        requirement=requirement,
        status=status,
        plan_identity=stable_digest({
            "schema": "zlang-backend-publication-plan-v1",
            "plan": plan.to_data(),
            "published_backend": artifact.backend,
        }),
        selected_ir_identity=selected_ir_identity,
        build_identity=artifact.build_identity,
        artifact_hash=artifact.artifact_hash,
        manifest_version=artifact.manifest_version,
        source_map_hash=source_map_hash,
        implementation_graph_identity=implementation_graph_identity,
        files=files,
        companions=companion_files,
    )


def report_from_path(
    report_id: str,
    kind: str,
    format: str,
    logical_path: str,
    physical_path: Path,
    *,
    evidence_ids: Iterable[str] = (),
) -> tuple[ReportRecord, PhysicalPublication]:
    """Create matching report and physical-publication records."""

    publication = published_file_from_path(
        logical_path, physical_path, kind=f"report:{kind}"
    )
    return (
        ReportRecord(
            report_id,
            kind,
            format,
            publication.record.content_hash,
            logical_path,
            tuple(evidence_ids),
        ),
        publication,
    )


def physical_path_map(
    publications: Iterable[PhysicalPublication],
) -> dict[str, Path]:
    """Reject conflicting logical paths and return the validation map."""

    result: dict[str, Path] = {}
    for item in publications:
        logical = item.record.logical_path
        previous = result.get(logical)
        if previous is not None and previous != item.physical_path:
            raise BuildManifestError(
                f"logical publication '{logical}' maps to multiple physical files"
            )
        result[logical] = item.physical_path
    return result


def publish_manifest_atomically(
    manifest: WholeBuildManifest,
    output_path: Path,
    *,
    publications: Iterable[PhysicalPublication],
) -> None:
    """Validate every mapped file, then replace the manifest in one rename.

    A second validation after the rename closes the meaningful output-mutation
    window: if a producer changes an artifact between the first validation and
    publication, the newly written manifest is removed rather than left as an
    apparently valid build record.  Consumers must still validate content when
    reading a manifest, as files may of course be changed later.
    """

    path = Path(output_path)
    mapping = physical_path_map(publications)
    # Dependency entries are already validated by the immutable project
    # workspace and intentionally have no physical cache paths in public IR.
    expected_dependencies = {
        item.logical_path for item in manifest.dependency_closure
    }
    validation_manifest = manifest
    if expected_dependencies:
        from dataclasses import replace

        validation_manifest = replace(manifest, dependency_closure=())
    validate_manifest_file_map(validation_manifest, mapping)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(manifest.to_json())
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        Path(temporary_name).replace(path)
        temporary_name = None
        try:
            validate_manifest_file_map(validation_manifest, mapping)
        except Exception:
            path.unlink(missing_ok=True)
            _fsync_directory(path.parent)
            raise
        _fsync_directory(path.parent)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    """Make a completed same-directory rename durable on POSIX filesystems."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "PhysicalPublication",
    "backend_build_record",
    "backend_logical_path",
    "dependency_records",
    "physical_path_map",
    "publish_manifest_atomically",
    "published_file_from_path",
    "report_from_path",
    "source_publication",
]
