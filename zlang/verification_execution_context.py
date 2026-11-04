"""Validated immutable context for one verification-bundle replay."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from zlang.common import stable_digest
from zlang.formal import FormalToolchainContext
from zlang import verification_bundle_codec as bundle_codec
from zlang import verification_bundle_io as bundle_io
from zlang import verification_bundle_report as bundle_report
from zlang import verification_codec_support as codec_support


@dataclass(frozen=True)
class VerificationExecutionContext:
    loaded: bundle_codec.LoadedVerificationBundle
    config: bundle_report.VerificationRunConfig
    execution_root: Path | None
    cache_root: Path | None
    selected_jobs: tuple[tuple[int, bundle_codec.VerificationJob], ...]
    toolchain: FormalToolchainContext | None
    tool_versions: tuple[tuple[str, str], ...]


def _external_directory(
    selected: Path | None,
    bundle_root: Path,
    *,
    purpose: str,
) -> Path | None:
    if selected is None:
        return None
    candidate = Path(selected).resolve(strict=False)
    try:
        candidate.relative_to(bundle_root)
    except ValueError:
        return candidate
    raise codec_support.VerificationBundleError(
        f"verification {purpose} directory must be outside the immutable bundle"
    )


def prepare_execution_context(
    loaded: bundle_codec.LoadedVerificationBundle,
    *,
    config: bundle_report.VerificationRunConfig,
    work_directory: Path | None,
    cache_directory: Path | None,
    job_kinds: frozenset[str] | None,
    toolchain: FormalToolchainContext | None,
) -> VerificationExecutionContext:
    """Validate replay roots, selected jobs, and exact toolchain attribution."""

    # Loaded bundles retain identities, not ownership of immutable filesystem
    # bytes. Recheck the full inventory before cache admission, independently
    # of optional diagnostic maps or whether a solver will be invoked.
    current = bundle_io.load_verification_bundle(loaded.directory)
    if current.manifest != loaded.manifest or current.verification_ir != loaded.verification_ir:
        raise codec_support.VerificationBundleError(
            "verification bundle changed since it was loaded"
        )
    loaded = current
    bundle_root = loaded.directory.resolve(strict=False)
    execution_parent = _external_directory(
        work_directory, bundle_root, purpose="work"
    )
    execution_root = None
    if execution_parent is not None:
        bundle_token = stable_digest(
            loaded.manifest.bundle_identity or loaded.manifest.computed_identity
        )[:16]
        run_token = stable_digest(config.to_data())[:16]
        execution_root = execution_parent / (
            f"{bundle_token}-{config.mode.value}-{run_token}"
        )
        execution_root.mkdir(parents=True, exist_ok=True)
    cache_root = _external_directory(
        cache_directory, bundle_root, purpose="cache"
    )
    selected_jobs = tuple(
        (ordinal, job)
        for ordinal, job in enumerate(loaded.manifest.jobs)
        if job_kinds is None or job.kind in job_kinds
    )
    if not selected_jobs:
        raise codec_support.VerificationBundleError(
            "verification execution selected no jobs"
        )

    needs_toolchain = config.engine == "sby" and any(
        job.executable and job.kind in {"safety", "cover"}
        for _, job in selected_jobs
    )
    if needs_toolchain:
        selected_toolchain = toolchain or FormalToolchainContext.discover(
            engine=config.engine,
            solver=config.solver,
            route=config.route,
        )
        if (
            selected_toolchain.engine != config.engine
            or selected_toolchain.solver != config.solver
            or selected_toolchain.route != config.route
        ):
            raise codec_support.VerificationBundleError(
                "verification toolchain context does not match the run configuration"
            )
        versions = selected_toolchain.versions
    else:
        selected_toolchain = None
        versions = ()
    return VerificationExecutionContext(
        loaded,
        config,
        execution_root,
        cache_root,
        selected_jobs,
        selected_toolchain,
        versions,
    )


__all__ = ["VerificationExecutionContext", "prepare_execution_context"]
