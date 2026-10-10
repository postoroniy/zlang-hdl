"""Session-owned bounded caches for compiler-aware tooling."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
import hashlib
import os
from pathlib import Path

from zlang._version import __version__
from zlang.analysis_needs import AnalysisNeeds
from zlang.compiler import check_file_snapshot
from zlang.module_resolver import ModuleResolutionError
from zlang.intent_structural_exploration import IntentStructuralExplorationCache
from zlang.project import ProjectModelError, discover_project_manifest
from zlang.source import SourceOrigin
from zlang.source_identity import SOURCE_SUFFIX
from zlang.source_rebinding import TriviaRebinding
from zlang import workspace as workspace_api
from zlang import tooling_models as models
from zlang import tooling_symbol_cache as symbol_cache
from zlang import tooling_workspace as tooling_workspace

def _is_snapshot_race(error: BaseException) -> bool:
    message = str(error).lower()
    return "changed after" in message or " is dirty" in message


class ToolingSession:
    """Bounded request-driven cache for compiler semantic tooling snapshots.

    The cache is deliberately owned by a tooling/LSP session rather than by
    the compiler process.  Entries are immutable semantic products, keyed by
    the exact root text plus the locked physical workspace snapshot.  No
    background refresh or global index is kept; the compiler-owned process-local
    session retains exact products for compatible snapshots.
    """

    _MAX_ENTRIES = 8

    def __init__(self) -> None:
        from zlang.incremental_workspace import IncrementalWorkspaceSession

        self._incremental_workspace = IncrementalWorkspaceSession()
        self._intent_structural_cache = IntentStructuralExplorationCache(
            max_entries=128,
            max_total_enodes=524_288,
        )
        self._entries: OrderedDict[tuple[object, ...], models._ToolingSnapshotEntry] = (
            OrderedDict()
        )
        self._workspace_indexes: OrderedDict[Path, object] = OrderedDict()
        self._navigation_regions: OrderedDict[
            tuple[Path, str], tuple[tuple[tuple[int, int], str], ...]
        ] = OrderedDict()
        self._symbol_entries: OrderedDict[
            tuple[object, ...], models._ToolingSymbolEntry
        ] = OrderedDict()
        self._trivia_symbols: OrderedDict[
            Path, tuple[str, models._ToolingSymbolEntry]
        ] = OrderedDict()
        self._trivia_checks: OrderedDict[
            Path, tuple[str, models._ToolingSnapshotEntry]
        ] = OrderedDict()
        self._symbol_bytes = 0
        self._editor_workspace = models.EditorWorkspaceSnapshot(())
        requested_mode = os.environ.get(
            "ZLANG_LSP_SYMBOL_CACHE", "persistent"
        ).strip().lower()
        self._symbol_mode = (
            requested_mode
            if requested_mode in {"persistent", "memory", "off"}
            else "memory"
        )

    def _workspace_index_for(
        self,
        manifest: Path,
    ) -> object:
        """Return one session-local workspace projection for a manifest."""

        path = Path(manifest).expanduser().resolve()
        index = self._workspace_indexes.get(path)
        if index is None:
            # Resolve lazily so an unprojected standalone source never pays for
            # workspace indexing.  ``tooling_workspace.workspace_index`` is defined below in
            # this module and is available when the method is invoked.
            index = tooling_workspace.workspace_index(path)
            self._workspace_indexes[path] = index
            self._workspace_indexes.move_to_end(path)
            while len(self._workspace_indexes) > self._MAX_ENTRIES:
                self._workspace_indexes.popitem(last=False)
        else:
            self._workspace_indexes.move_to_end(path)
        return index

    @staticmethod
    def _context_key(
        source: Path,
        source_text: str,
        *,
        source_digest: str | None,
        project: Path | str | None,
        profile: str | None,
        top: str | None,
        editor_identity: tuple[tuple[str, str], ...] = (),
    ) -> tuple[object, ...]:
        # Always derive the content identity from the exact in-memory text.
        # ``source_digest`` is a caller-provided snapshot assertion (and is
        # checked by the compiler), not a substitute for the text identity in
        # the cache key.  A correct assertion is canonicalized to the same key
        # as an omitted assertion so an LSP didOpen check can be reused by a
        # subsequent definition/hover query.  A stale/mistyped assertion stays
        # distinct and is still rejected by the compiler on a cache miss.
        text_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        digest_assertion = (
            None
            if source_digest is None or source_digest == text_digest
            else source_digest
        )
        project_path = (
            None
            if project is None
            else Path(project).expanduser().resolve().as_posix()
        )
        return (
            source,
            text_digest,
            digest_assertion,
            len(source_text),
            project_path,
            profile,
            top,
            models.TOOLING_API_SCHEMA,
            __version__,
            editor_identity,
        )

    def set_editor_workspace(self, snapshot: models.EditorWorkspaceSnapshot) -> None:
        """Select one immutable open-document view for subsequent requests."""

        if not isinstance(snapshot, models.EditorWorkspaceSnapshot):
            raise TypeError("editor workspace must be an models.EditorWorkspaceSnapshot")
        self._editor_workspace = snapshot

    def editor_text_for(self, source: Path | str) -> str | None:
        """Return exact open-buffer text for ``source``, when available."""

        path = Path(source).expanduser().resolve()
        for document in self._editor_workspace.documents:
            if document.path == path:
                return document.text
        return None

    def source_path_for_unit(
        self,
        source: Path | str,
        source_unit: str,
    ) -> Path | None:
        """Resolve one compiler logical unit within the source's project."""

        try:
            # Prefer the atomic editor view.  This route deliberately does not
            # parse bytes from disk: an imported open buffer may currently be
            # malformed, or an autosave may have replaced its physical bytes,
            # while the compiler diagnostic still carries the exact logical
            # unit that must receive the marker.
            manifest = discover_project_manifest(Path(source))
            if manifest is not None:
                source_root = manifest.source_directory.resolve(strict=True)
                for document in self._editor_workspace.documents:
                    try:
                        relative = document.path.relative_to(source_root)
                    except ValueError:
                        continue
                    if relative.suffix != SOURCE_SUFFIX:
                        continue
                    logical = ".".join(
                        (manifest.package, *relative.with_suffix("").parts)
                    )
                    if logical == source_unit:
                        return document.path

            location = tooling_workspace.discover_project(source)
            if location is None:
                return None
            index = self._workspace_index_for(location.manifest_path)
            return index.source_path_for_unit(source_unit)
        except (models.ToolingError, OSError, ValueError):
            return None

    @staticmethod
    def _file_fingerprint(path: Path) -> tuple[object, ...]:
        try:
            payload = path.read_bytes()
        except OSError:
            return (path.as_posix(), None)
        return (path.as_posix(), hashlib.sha256(payload).hexdigest())

    @staticmethod
    def _file_signature(path: Path) -> tuple[object, ...]:
        try:
            stat = path.stat()
        except OSError:
            return (path.as_posix(), None, None, None, None, None)
        return (
            path.as_posix(),
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
            stat.st_dev,
            stat.st_ino,
        )

    @classmethod
    def _environment_signature(cls, physical_inputs: object) -> tuple[object, ...]:
        overlays = {
            Path(path).expanduser().resolve(): digest
            for path, digest in getattr(
                physical_inputs, "editor_source_overlays", ()
            )
        }
        paths = tuple(
            Path(path).expanduser().resolve()
            for path in getattr(physical_inputs, "all_paths", ())
            if Path(path).expanduser().resolve() not in overlays
        )
        return (
            *tuple(cls._file_signature(path) for path in paths),
            *(('editor', path.as_posix(), digest) for path, digest in sorted(
                overlays.items(), key=lambda item: item[0].as_posix()
            )),
        )

    @classmethod
    def _environment_fingerprint(
        cls,
        physical_inputs: object,
    ) -> tuple[object, ...]:
        overlays = {
            Path(path).expanduser().resolve(): digest
            for path, digest in getattr(
                physical_inputs, "editor_source_overlays", ()
            )
        }
        paths = tuple(
            Path(path).expanduser().resolve()
            for path in getattr(physical_inputs, "all_paths", ())
            if Path(path).expanduser().resolve() not in overlays
        )
        # ``PhysicalCompilationInputs`` is the compiler's authoritative locked
        # dependency/source closure.  Fingerprint those paths only: no
        # directory scans, polling, or speculative workspace discovery is
        # performed by the cache.
        return (
            *tuple(cls._file_fingerprint(path) for path in paths),
            *(('editor', path.as_posix(), digest) for path, digest in sorted(
                overlays.items(), key=lambda item: item[0].as_posix()
            )),
        )

    def semantic_snapshot(
        self,
        source: Path | str,
        source_text: str,
        *,
        analysis_needs: AnalysisNeeds = AnalysisNeeds.NONE,
        source_digest: str | None = None,
        project: Path | str | None = None,
        profile: str | None = None,
        top: str | None = None,
    ) -> object:
        """Return a compatible semantic product, upgrading needs monotonically."""

        requested = AnalysisNeeds(analysis_needs)
        path = Path(source).expanduser().resolve()
        editor_identity = self._editor_workspace.identity_for(path)
        context_key = self._context_key(
            path,
            source_text,
            source_digest=source_digest,
            project=project,
            profile=profile,
            top=top,
            editor_identity=editor_identity,
        )
        entry = self._entries.get(context_key)
        if entry is not None:
            current_signature = self._environment_signature(
                entry.result.physical_inputs
            )
            if (
                symbol_cache._inventory_matches(entry)
                and entry.environment_signature == current_signature
                and entry.needs & requested == requested
            ):
                if requested.wants(AnalysisNeeds.DEFINITIONS):
                    self._remember_symbol_snapshot(
                        path, source_text, entry.result, context_key
                    )
                self._entries.move_to_end(context_key)
                return entry.result
            # File metadata is a cheap request-time guard.  Only when a
            # dependency/source signature changed do we re-hash the locked
            # inputs to distinguish a touch/replace with identical bytes from
            # an actual semantic snapshot change.
            if entry.environment_fingerprint == self._environment_fingerprint(
                entry.result.physical_inputs
            ):
                refreshed = models._ToolingSnapshotEntry(
                    entry.context_key,
                    entry.needs,
                    entry.result,
                    current_signature,
                    entry.environment_fingerprint,
                    entry.root_inventory,
                )
                self._entries[context_key] = refreshed
                if symbol_cache._inventory_matches(entry) and entry.needs & requested == requested:
                    if requested.wants(AnalysisNeeds.DEFINITIONS):
                        self._remember_symbol_snapshot(
                            path, source_text, entry.result, context_key
                        )
                    self._entries.move_to_end(context_key)
                    return entry.result
            self._workspace_indexes.clear()
            requested |= entry.needs

        overlays = self._editor_workspace.overlays_for(path)
        for attempt in range(2):
            try:
                result = check_file_snapshot(
                    path,
                    source_text,
                    source_digest=source_digest,
                    project=project,
                    profile=profile,
                    top=top,
                    analysis_needs=requested,
                    allow_external_enum_inputs=True,
                    allow_unsaved_root=True,
                    source_overlays=overlays,
                    incremental_workspace=self._incremental_workspace,
                    intent_structural_cache=self._intent_structural_cache,
                )
                break
            except (ModuleResolutionError, workspace_api.WorkspaceError) as error:
                if attempt or not _is_snapshot_race(error):
                    raise
                # A physical input was replaced between discovery and locked
                # snapshot validation.  Rebuild the workspace once from the
                # same immutable editor overlay; a second race propagates as a
                # neutral LSP environment failure instead of stale results.
                self._workspace_indexes.clear()
        stored = models._ToolingSnapshotEntry(
            context_key,
            requested,
            result,
            self._environment_signature(result.physical_inputs),
            self._environment_fingerprint(result.physical_inputs),
            symbol_cache._symbol_root_inventory(result.physical_inputs),
        )
        self._entries[context_key] = stored
        self._entries.move_to_end(context_key)
        while len(self._entries) > self._MAX_ENTRIES:
            self._entries.popitem(last=False)
        if requested.wants(AnalysisNeeds.DEFINITIONS):
            self._remember_symbol_snapshot(path, source_text, result, context_key)
        return result

    def trivia_diagnostic_proof(self, source: Path, source_text: str) -> bool:
        """Reuse an earlier *clean* check without returning stale typed IR."""

        path = Path(source).expanduser().resolve()
        previous = self._trivia_checks.get(path)
        if previous is None:
            return False
        old_text, entry = previous
        if entry.context_key[6] is not None:
            # didChange clears a navigation-only selected top.  A proof for
            # that child is not a proof that the default public top is valid.
            return False
        inputs = entry.result.physical_inputs
        editor_identity = self._editor_workspace.identity_for(path)
        old_editor = tuple(item for item in entry.context_key[-1]
                           if item[0] != path.as_posix())
        new_editor = tuple(item for item in editor_identity
                           if item[0] != path.as_posix())
        if old_editor != new_editor:
            return False
        try:
            old_other = tuple(item for item in entry.environment_fingerprint
                              if item[0] != path.as_posix())
            new_other = tuple(item for item in self._environment_fingerprint(inputs)
                              if item[0] != path.as_posix())
            return (
                old_other == new_other
                and entry.root_inventory == symbol_cache._symbol_root_inventory(inputs)
                and TriviaRebinding.between(old_text, source_text) is not None
            )
        except (OSError, ProjectModelError, models.ToolingError, ValueError):
            return False

    def symbol_snapshot(
        self,
        source: Path | str,
        source_text: str,
        *,
        project: Path | str | None = None,
        profile: str | None = None,
        top: str | None = None,
    ) -> models._SymbolSnapshot | None:
        """Return a validated symbol-only memory or persistent cache hit."""

        if self._symbol_mode == "off":
            return None
        path = Path(source).expanduser().resolve()
        editor_identity = self._editor_workspace.identity_for(path)
        context_key = self._context_key(
            path,
            source_text,
            source_digest=None,
            project=project,
            profile=profile,
            top=top,
            editor_identity=editor_identity,
        )
        entry = self._symbol_entries.get(context_key)
        if entry is not None and not symbol_cache._inventory_matches(entry):
            self._drop_symbol_entry(context_key)
            entry = None
        if entry is not None:
            signature = self._environment_signature(entry.result.physical_inputs)
            if signature == entry.environment_signature:
                self._symbol_entries.move_to_end(context_key)
                return entry.result
            fingerprint = self._environment_fingerprint(
                entry.result.physical_inputs
            )
            if fingerprint == entry.environment_fingerprint:
                refreshed = models._ToolingSymbolEntry(
                    context_key,
                    entry.result,
                    signature,
                    fingerprint,
                    entry.root_inventory,
                )
                self._symbol_entries[context_key] = refreshed
                self._symbol_entries.move_to_end(context_key)
                return entry.result
            self._drop_symbol_entry(context_key)
        rebased = self._rebind_trivia_symbols(
            path, source_text, context_key, editor_identity
        )
        if rebased is not None:
            self._insert_symbol_entry(context_key, rebased)
            return rebased
        if self._symbol_mode != "persistent" or editor_identity:
            return None
        snapshot = symbol_cache._load_persistent_symbol_snapshot(
            path,
            source_text,
            project=project,
            profile=profile,
            top=top,
        )
        if snapshot is None:
            return None
        try:
            self._insert_symbol_entry(context_key, snapshot)
        except (OSError, ProjectModelError, models.ToolingError):
            return None
        return snapshot

    def _rebind_trivia_symbols(
        self,
        source: Path,
        source_text: str,
        context_key: tuple[object, ...],
        editor_identity: tuple[tuple[str, str], ...],
    ) -> models._SymbolSnapshot | None:
        previous = self._trivia_symbols.get(source)
        if previous is None:
            return None
        old_text, entry = previous
        old_key = entry.context_key
        if any(old_key[index] != context_key[index] for index in (0, 4, 5, 6, 7, 8)):
            return None
        remaining_old = tuple(item for item in old_key[-1] if item[0] != source.as_posix())
        remaining_new = tuple(item for item in editor_identity if item[0] != source.as_posix())
        if remaining_old != remaining_new:
            return None
        inputs = entry.result.physical_inputs
        try:
            current = self._environment_fingerprint(inputs)
            old_other = tuple(item for item in entry.environment_fingerprint if item[0] != source.as_posix())
            new_other = tuple(item for item in current if item[0] != source.as_posix())
            if old_other != new_other or entry.root_inventory != symbol_cache._symbol_root_inventory(inputs):
                return None
            mapping = TriviaRebinding.between(old_text, source_text)
            if mapping is None:
                return None
            root_units = {
                unit for unit, path in inputs.source_unit_paths if path == source
            }
            old_digest = hashlib.sha256(old_text.encode("utf-8")).hexdigest()

            def origin(value: SourceOrigin) -> SourceOrigin:
                if value.source_unit is None and inputs.project_manifest is not None:
                    raise ValueError("project symbol origin has no source unit")
                if value.source_unit not in root_units and value.source_unit is not None:
                    return value
                if value.digest != old_digest:
                    raise ValueError("symbol origin is not bound to the old root snapshot")
                updated = mapping.origin(value)
                if updated is None:
                    raise ValueError("symbol span is not anchored to a parser token")
                return updated

            resolutions = tuple(replace(item,
                occurrence=origin(item.occurrence), target=origin(item.target))
                for item in entry.result.definition_resolutions)
            declarations = tuple(replace(item, target=origin(item.target))
                                 for item in entry.result.definition_declarations)
            return models._SymbolSnapshot(inputs, resolutions, declarations,
                                   entry.result.serialized_size)
        except (OSError, ProjectModelError, models.ToolingError, ValueError):
            return None

    def symbol_snapshot_covering(
        self,
        source: Path | str,
        source_text: str,
        *,
        required_module: str,
    ) -> models._SymbolSnapshot | None:
        """Return a valid shard that semantically observed this exact source.

        A root hierarchy analysis already records declarations and occurrences
        in imported/instantiated child units.  Reuse that compiler evidence for
        an opened definition target instead of compiling the child again for
        semantic tokens.  Cross-root reuse is saved-file-only and requires the
        child digest plus the complete originating shard environment to match.
        """

        if self._symbol_mode == "off" or not required_module:
            return None
        path = Path(source).expanduser().resolve()
        try:
            digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            if models._digest_file(path) != digest:
                return None
        except OSError:
            return None

        for context_key in reversed(tuple(self._symbol_entries)):
            entry = self._symbol_entries.get(context_key)
            if entry is None:
                continue
            if not symbol_cache._inventory_matches(entry):
                self._drop_symbol_entry(context_key)
                continue
            signature = self._environment_signature(entry.result.physical_inputs)
            if signature != entry.environment_signature:
                fingerprint = self._environment_fingerprint(
                    entry.result.physical_inputs
                )
                if fingerprint != entry.environment_fingerprint:
                    self._drop_symbol_entry(context_key)
                    continue
                entry = models._ToolingSymbolEntry(
                    context_key,
                    entry.result,
                    signature,
                    fingerprint,
                    entry.root_inventory,
                )
                self._symbol_entries[context_key] = entry

            units = {
                unit
                for unit, candidate in entry.result.physical_inputs.source_unit_paths
                if candidate.resolve() == path
            }
            if entry.result.physical_inputs.root_source.resolve() == path:
                root_unit = tooling_workspace._root_logical_source(path, self)
                if root_unit is not None:
                    units.add(root_unit)
            if not units:
                continue
            observed = tuple(
                origin
                for origin in (
                    *(
                        declaration.target
                        for declaration in entry.result.definition_declarations
                    ),
                    *(
                        resolution.occurrence
                        for resolution in entry.result.definition_resolutions
                    ),
                )
                if origin.source_unit in units
            )
            if not observed or any(origin.digest != digest for origin in observed):
                continue
            if not any(
                declaration.kind == "module"
                and declaration.name == required_module
                and declaration.target.source_unit in units
                and declaration.target.digest == digest
                for declaration in entry.result.definition_declarations
            ):
                continue
            self._symbol_entries.move_to_end(context_key)
            return entry.result
        return None

    def _drop_symbol_entry(self, key: tuple[object, ...]) -> None:
        entry = self._symbol_entries.pop(key, None)
        if entry is not None:
            self._symbol_bytes -= entry.result.serialized_size

    def _insert_symbol_entry(
        self,
        context_key: tuple[object, ...],
        snapshot: models._SymbolSnapshot,
    ) -> None:
        self._drop_symbol_entry(context_key)
        entry = models._ToolingSymbolEntry(
            context_key,
            snapshot,
            self._environment_signature(snapshot.physical_inputs),
            self._environment_fingerprint(snapshot.physical_inputs),
            symbol_cache._symbol_root_inventory(snapshot.physical_inputs),
        )
        self._symbol_entries[context_key] = entry
        self._symbol_entries.move_to_end(context_key)
        self._symbol_bytes += snapshot.serialized_size
        while self._symbol_entries and (
            len(self._symbol_entries) > symbol_cache._SYMBOL_MEMORY_MAX_ENTRIES
            or self._symbol_bytes > symbol_cache._SYMBOL_MEMORY_MAX_BYTES
        ):
            oldest_key = next(iter(self._symbol_entries))
            self._drop_symbol_entry(oldest_key)

    def _remember_symbol_snapshot(
        self,
        source: Path,
        source_text: str,
        result: object,
        context_key: tuple[object, ...],
    ) -> None:
        if self._symbol_mode == "off":
            return
        existing = self._symbol_entries.get(context_key)
        if existing is not None:
            self._symbol_entries.move_to_end(context_key)
            return
        try:
            snapshot, payload = symbol_cache._normalized_symbol_payload(source, result, self)
            self._insert_symbol_entry(context_key, snapshot)
            if self._symbol_mode == "persistent":
                overlays = getattr(
                    result.physical_inputs, "editor_source_overlays", ()
                )
                saved = all(
                    models._digest_file(Path(path)) == digest
                    for path, digest in overlays
                )
                if saved:
                    symbol_cache._publish_persistent_symbol_snapshot(
                        source,
                        source_text,
                        payload,
                        project=context_key[4],
                        profile=context_key[5],
                        top=context_key[6],
                    )
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            # Navigation remains available from the just-produced semantic
            # result even when the optional cache cannot normalize or publish.
            return

    def invalidate(self, source: Path | str) -> None:
        """Drop all snapshots rooted at one document path."""

        path = Path(source).expanduser().resolve()
        old_text = self.editor_text_for(path)
        if old_text is not None:
            for key, entry in reversed(tuple(self._entries.items())):
                if key[0] == path and key[1] == hashlib.sha256(
                    old_text.encode("utf-8")
                ).hexdigest():
                    self._trivia_checks[path] = (old_text, entry)
                    self._trivia_checks.move_to_end(path)
                    while len(self._trivia_checks) > self._MAX_ENTRIES:
                        self._trivia_checks.popitem(last=False)
                    break
            for key, entry in reversed(tuple(self._symbol_entries.items())):
                if key[0] == path and key[1] == hashlib.sha256(
                    old_text.encode("utf-8")
                ).hexdigest():
                    self._trivia_symbols[path] = (old_text, entry)
                    self._trivia_symbols.move_to_end(path)
                    while len(self._trivia_symbols) > self._MAX_ENTRIES:
                        self._trivia_symbols.popitem(last=False)
                    break
        for key, entry in tuple(self._entries.items()):
            inputs = getattr(entry.result, "physical_inputs", None)
            paths = {
                Path(item).expanduser().resolve()
                for item in getattr(inputs, "all_paths", ())
            }
            if key[0] == path or path in paths:
                del self._entries[key]
        for key, entry in tuple(self._symbol_entries.items()):
            paths = {
                Path(item).expanduser().resolve()
                for item in getattr(entry.result.physical_inputs, "all_paths", ())
            }
            if key[0] == path or path in paths:
                self._drop_symbol_entry(key)
        for key in tuple(self._navigation_regions):
            if key[0] == path:
                del self._navigation_regions[key]
        self._workspace_indexes.clear()

    def clear(self) -> None:
        """Release all session-owned snapshots."""

        self._incremental_workspace.clear()
        self._intent_structural_cache.clear()
        self._entries.clear()
        self._symbol_entries.clear()
        self._trivia_symbols.clear()
        self._trivia_checks.clear()
        self._symbol_bytes = 0
        self._navigation_regions.clear()
        self._workspace_indexes.clear()
