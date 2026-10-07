"""Bounded, compiler-owned reuse of exact file compilation sessions.

Only an identical physical/editor snapshot may retain typed products.  The
workspace loader remains authoritative on every miss; this layer never tries
to infer semantic equivalence from source spelling.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
import hashlib
from pathlib import Path
from threading import RLock
from typing import Mapping

from zlang.analysis_needs import AnalysisNeeds
from zlang.compilation_session import CompilationSession
from zlang.project import ProjectModelError, discover_project_manifest


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _physical_fingerprint(session: CompilationSession) -> tuple[object, ...]:
    inputs = session.physical_inputs
    overlays = dict(inputs.editor_source_overlays)
    files = []
    for path in inputs.all_paths:
        try:
            digest = overlays.get(path) or _digest(path.read_bytes())
        except OSError:
            digest = None
        files.append((path.as_posix(), digest))
    # A new root-package module changes the resolver inventory even if none
    # of the previously known source files or the lock was modified.
    manifest = inputs.project_manifest
    try:
        model = (
            discover_project_manifest(manifest, explicit=manifest)
            if manifest is not None
            else discover_project_manifest(inputs.root_source)
            if inputs.root_source is not None
            else None
        )
        if manifest is None and model is not None:
            return (*files, ("project-discovery-changed", model.path.resolve()))
        if model is None:
            inventory: tuple[str, ...] = ()
        else:
            root = model.source_directory.resolve(strict=True)
            inventory = tuple(
                sorted(
                    path.relative_to(root).as_posix() for path in root.rglob("*.zhl")
                )
            )
    except (OSError, ValueError, ProjectModelError):
        inventory = ("<unavailable>",)
    return (*files, ("root-package-inventory", inventory))


def _same_affected_closure(
    previous: CompilationSession,
    current: CompilationSession,
    old_fingerprint: tuple[object, ...],
    current_fingerprint: tuple[object, ...],
    new_fingerprint: tuple[object, ...],
) -> bool:
    """An unrelated root-package file may change without changing this top.

    The new workspace has already validated the entire project inventory.
    Manifest, lock, external sources and the exact selected dependency
    closure remain strict; only root-package modules outside that closure may
    differ.  The previously discovered stdlib sources are checked against
    their own retained fingerprint by the caller.
    """

    old_inputs = previous.physical_inputs
    new_inputs = current.physical_inputs
    if (
        old_inputs.project_manifest is None
        or previous.root_module_identity != current.root_module_identity
        or previous.dependency_closure != current.dependency_closure
        or replace(old_inputs, stdlib_sources=()) != new_inputs
    ):
        return False
    old_files = dict(old_fingerprint[:-1])
    present_files = dict(current_fingerprint[:-1])
    new_files = dict(new_fingerprint[:-1])
    root_modules = {path.as_posix() for path in old_inputs.project_module_sources}
    stdlib = {path.as_posix() for path in old_inputs.stdlib_sources}
    if any(old_files.get(path) != present_files.get(path) for path in stdlib):
        return False
    for path in set(old_files) | set(new_files):
        if path in root_modules and path != old_inputs.root_source.as_posix():
            continue
        if path in stdlib:
            continue
        if old_files.get(path) != new_files.get(path):
            return False
    return old_fingerprint[-1] == new_fingerprint[-1]


class IncrementalWorkspaceSession:
    """An isolated LRU of exact compiler sessions for one consuming service.

    The caller owns serialization of semantic requests.  File contents and every
    locked physical input are checked before returning a retained product; source
    text and unsafe executable code are never serialized to disk here.
    """

    def __init__(self, *, max_entries: int = 8) -> None:
        if max_entries < 1:
            raise ValueError("incremental workspace requires a positive entry bound")
        self._entries: OrderedDict[
            tuple[object, ...], tuple[CompilationSession, tuple[object, ...]]
        ] = OrderedDict()
        self._max_entries = max_entries
        self._lock = RLock()

    def file_snapshot(
        self,
        source: Path | str,
        source_text: str,
        *,
        source_digest: str | None = None,
        project: Path | str | None = None,
        profile: str | None = None,
        top: str | None = None,
        analysis_needs: AnalysisNeeds = AnalysisNeeds.NONE,
        allow_external_enum_inputs: bool = False,
        allow_unsaved_root: bool = False,
        source_overlays: Mapping[Path | str, str] | None = None,
    ) -> CompilationSession:
        from zlang.compiler import create_file_compilation_session_snapshot

        path = Path(source).expanduser().resolve(strict=True)
        digest = _digest(source_text.encode("utf-8"))
        if source_digest is not None and source_digest != digest:
            raise ValueError(
                "source snapshot digest does not match its decoded UTF-8 bytes"
            )
        overlays = tuple(
            sorted(
                (
                    Path(name).expanduser().resolve(strict=True).as_posix(),
                    _digest(text.encode("utf-8")),
                )
                for name, text in (source_overlays or {}).items()
            )
        )
        key: tuple[object, ...] = (
            path,
            digest,
            None if project is None else Path(project).expanduser().resolve(),
            profile,
            top,
            AnalysisNeeds(analysis_needs),
            allow_external_enum_inputs,
            allow_unsaved_root,
            overlays,
        )
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                session, fingerprint = cached
                current_fingerprint = _physical_fingerprint(session)
                if fingerprint == current_fingerprint:
                    self._entries.move_to_end(key)
                    return session
                del self._entries[key]
            fresh = create_file_compilation_session_snapshot(
                path,
                source_text,
                source_digest=digest,
                project=project,
                profile=profile,
                top=top,
                analysis_needs=analysis_needs,
                allow_external_enum_inputs=allow_external_enum_inputs,
                allow_unsaved_root=allow_unsaved_root,
                source_overlays=source_overlays,
            )
            if cached is not None and _same_affected_closure(
                session,
                fresh,
                fingerprint,
                current_fingerprint,
                _physical_fingerprint(fresh),
            ):
                self._entries[key] = (session, current_fingerprint)
                return session
            session = fresh
            self._entries[key] = (session, _physical_fingerprint(session))
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            return session

    def refresh(self, session: CompilationSession) -> None:
        """Capture physical inputs discovered by a demanded product."""

        with self._lock:
            for key, (retained, _) in self._entries.items():
                if retained is session:
                    self._entries[key] = (session, _physical_fingerprint(session))
                    return

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
