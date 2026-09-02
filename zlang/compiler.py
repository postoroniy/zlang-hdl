"""Public orchestration API for the ZLang compilation pipeline."""

from __future__ import annotations

import hashlib
from pathlib import Path

from zlang.costs import SourcePolicy
from zlang.formal_exploration import FormalPolicy
from zlang.targets import ArchitectureSelectionMode
from zlang.workspace import WorkspaceError, load_project_workspace
from zlang.implementation_request import (
    BackendKind,
    ImplementationContribution,
    RequirementMode,
    parse_selected_profile,
)
from zlang.compilation_inputs import PhysicalCompilationInputs
from zlang.compilation_products import CompilationResult, SemanticCheckResult
from zlang.compilation_session import (
    CompilationSession,
    SessionTopSelectionError,
    inline_locals as _inline_locals,
)
from zlang.source_identity import validate_source_path


class TopSelectionError(ValueError):
    """A requested source top does not exist."""


def _materialize_session(session: CompilationSession) -> CompilationResult:
    try:
        return session.materialize()
    except SessionTopSelectionError as error:
        raise TopSelectionError(str(error)) from error


def compile_source(
    source: str,
    *,
    formal_policy: FormalPolicy | str | None = None,
    formal_depth: int = 32,
    formal_max_candidates: int = 8,
    formal_timeout: int = 120,
    formal_cache=None,
    formal_work_directory=None,
    formal_verifier=None,
    top: str | None = None,
    target: str | None = None,
    architecture: str | None = None,
    architecture_mode: ArchitectureSelectionMode | str | None = None,
    target_evidence_policy: SourcePolicy | str | None = None,
    target_evidence=None,
    target_evidence_path=None,
    target_tool: str = "Vivado",
    target_tool_version: str = "2024.2",
    target_clock_period_ns: float = 10.0,
    include_clash: bool = True,
    source_unit: str | None = None,
    module_resolver=None,
    root_module_identity=None,
    dependency_closure=None,
    implementation_backend: BackendKind | str | None = None,
    implementation_backend_mode: RequirementMode | str = RequirementMode.REQUIRED,
    implementation_contributions: tuple[ImplementationContribution, ...] = (),
) -> CompilationResult:
    """Run source through the backend-independent pipeline.

    Clash source remains part of the compatibility result by default.  A
    caller requesting only another backend may disable that final, independent
    emission step without changing semantic or selected IR.
    """

    # Keep this long-standing facade eager while making its orchestration
    # available independently through CompilationSession.
    return _materialize_session(
        CompilationSession(
            source,
            formal_policy=formal_policy,
            formal_depth=formal_depth,
            formal_max_candidates=formal_max_candidates,
            formal_timeout=formal_timeout,
            formal_cache=formal_cache,
            formal_work_directory=formal_work_directory,
            formal_verifier=formal_verifier,
            top=top,
            target=target,
            architecture=architecture,
            architecture_mode=architecture_mode,
            target_evidence_policy=target_evidence_policy,
            target_evidence=target_evidence,
            target_evidence_path=target_evidence_path,
            target_tool=target_tool,
            target_tool_version=target_tool_version,
            target_clock_period_ns=target_clock_period_ns,
            include_clash=include_clash,
            source_unit=source_unit,
            module_resolver=module_resolver,
            root_module_identity=root_module_identity,
            dependency_closure=dependency_closure,
            implementation_backend=implementation_backend,
            implementation_backend_mode=implementation_backend_mode,
            implementation_contributions=implementation_contributions,
        )
    )


def compile_file(
    source: Path | str,
    *,
    project: Path | str | None = None,
    profile: str | None = None,
    **options,
) -> CompilationResult:
    """Read one physical source snapshot and compile it with file resolution.

    This is the read-once convenience API.  Project loading remains read-only
    and offline; :func:`compile_file_snapshot` accepts an already decoded
    snapshot while performing the same filesystem-backed resolution.
    """

    return _materialize_session(
        create_file_compilation_session(
            source,
            project=project,
            profile=profile,
            **options,
        )
    )


def create_file_compilation_session(
    source: Path | str,
    *,
    project: Path | str | None = None,
    profile: str | None = None,
    **options,
) -> CompilationSession:
    """Read one source snapshot and return a lazy file-backed session."""

    source_path = validate_source_path(source)
    payload = source_path.read_bytes()
    return create_file_compilation_session_snapshot(
        source_path,
        payload.decode("utf-8"),
        source_digest=hashlib.sha256(payload).hexdigest(),
        project=project,
        profile=profile,
        **options,
    )


def create_file_compilation_session_snapshot(
    source: Path | str,
    source_text: str,
    *,
    source_digest: str | None = None,
    project: Path | str | None = None,
    profile: str | None = None,
    **options,
) -> CompilationSession:
    """Create a lazy session from one exact decoded file snapshot."""

    if not isinstance(source_text, str):
        raise TypeError("source snapshot must be decoded UTF-8 text")
    source_path = validate_source_path(source)
    snapshot_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    if source_digest is not None:
        if source_digest != snapshot_digest:
            raise ValueError(
                "source snapshot digest does not match its decoded UTF-8 bytes"
            )
    else:
        source_digest = snapshot_digest
    resolved_source = source_path.expanduser().resolve(strict=True)
    workspace = load_project_workspace(resolved_source, project=project)
    contributions = tuple(options.pop("implementation_contributions", ()))
    if workspace is None:
        if profile is not None:
            from zlang.implementation_request import ImplementationRequestError

            raise ImplementationRequestError(
                f"implementation profile '{profile}' requires a zlang.toml project"
            )
        return CompilationSession(
            source_text,
            source_unit=source_path.name,
            implementation_contributions=contributions,
            physical_inputs=PhysicalCompilationInputs(root_source=resolved_source),
            **options,
        )
    identity = workspace.root_identity_for(source_path)
    if identity.digest != source_digest:
        raise WorkspaceError(
            f"root source '{identity.logical_path}' changed after its compilation "
            f"snapshot: expected sha256:{source_digest}, found sha256:{identity.digest}"
        )
    if profile is not None:
        contributions = (
            *contributions,
            parse_selected_profile(workspace.manifest, profile),
        )
    return CompilationSession(
        source_text,
        source_unit=identity.logical_path,
        module_resolver=workspace.resolver,
        root_module_identity=identity,
        dependency_closure=workspace.compilation_closure_for(source_path),
        implementation_contributions=contributions,
        physical_inputs=PhysicalCompilationInputs(
            root_source=resolved_source,
            project_manifest=workspace.manifest.path,
            project_lock=workspace.lock_path,
            project_module_sources=tuple(
                item.source_path for item in workspace.root_modules
            ),
            dependency_manifests=workspace.dependency_manifests,
            dependency_module_sources=tuple(
                item.source_path for item in workspace.dependency_modules
            ),
            external_sources=workspace.external_source_paths,
        ),
        **options,
    )


def compile_file_snapshot(
    source: Path | str,
    source_text: str,
    *,
    source_digest: str | None = None,
    project: Path | str | None = None,
    profile: str | None = None,
    **options,
) -> CompilationResult:
    """Compile one exact decoded source snapshot with filesystem resolution.

    The root source is never read by this function for compilation.  The path
    remains necessary for project discovery and locked-module identity.  A
    project workspace may independently validate its indexed root bytes; that
    identity must match the supplied snapshot before semantic analysis starts.
    """

    return _materialize_session(
        create_file_compilation_session_snapshot(
            source,
            source_text,
            source_digest=source_digest,
            project=project,
            profile=profile,
            **options,
        )
    )


def check_file_snapshot(
    source: Path | str,
    source_text: str,
    *,
    source_digest: str | None = None,
    project: Path | str | None = None,
    profile: str | None = None,
    **options,
) -> SemanticCheckResult:
    """Check one exact file snapshot without planning or backend products.

    File/project resolution and digest validation intentionally match
    :func:`compile_file_snapshot`; only the demanded compilation product is
    different.
    """

    session = create_file_compilation_session_snapshot(
        source,
        source_text,
        source_digest=source_digest,
        project=project,
        profile=profile,
        **options,
    )
    try:
        module = session.check()
        syntax = session.syntax
    except SessionTopSelectionError as error:
        raise TopSelectionError(str(error)) from error
    return SemanticCheckResult(
        ast=syntax,
        ir=module,
        physical_inputs=session.physical_inputs,
    )
