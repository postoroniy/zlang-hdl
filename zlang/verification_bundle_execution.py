# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Execute immutable verification bundles and own result caching."""

from __future__ import annotations

from dataclasses import dataclass, replace
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

from zlang.backend.source_map import GeneratedSourceMap
from zlang.common import stable_digest
from zlang.common.content_cache import load_json_object, publish_json_atomically
from zlang.formal import (
    FormalToolchainContext,
    run_verilog_cover,
    run_verilog_formal,
)
from zlang import formal_trace as formal_trace
from zlang.ir.comparison_window import ComparisonWindow
from zlang.ir import formal as ir_formal
from zlang import source as source
from zlang.toolchain import GeneratedDiagnosticContext


from zlang import verification_bundle_codec as bundle_codec
from zlang import verification_codec_support as codec_support
from zlang import verification_bundle_io as bundle_io
from zlang import verification_bundle_report as bundle_report

class _ExecutionRootLease:
    """Process-wide exclusive lease for one deterministic solver workspace.

    Job paths intentionally remain content-derived and inspectable.  The lease
    prevents two concurrent replays of the same bundle/run configuration from
    corrupting those shared status, log, or trace files.  Independent bundles
    and configurations still execute concurrently because they use different
    roots and lock files.
    """

    def __init__(self, root: Path) -> None:
        self._stream = (root / ".zlang-formal.lock").open("a+", encoding="ascii")
        fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX)

    def close(self) -> None:
        if self._stream.closed:
            return
        try:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()

    def __del__(self) -> None:
        self.close()


def _execution_inputs(
    loaded: bundle_codec.LoadedVerificationBundle,
    job: bundle_codec.VerificationJob,
) -> tuple[str, Mapping[str, bytes]]:
    """Return Verilog text and flat auxiliary files for one immutable job."""

    records = {item.logical_path: item for item in loaded.manifest.files}
    source_parts: list[str] = []
    auxiliary: dict[str, bytes] = {}
    for logical_path in job.source_files:
        record = records.get(logical_path)
        if record is None:
            raise codec_support.VerificationBundleError(
                f"verification job '{job.property_id}' references an unknown source"
            )
        content = loaded.read_bytes(logical_path)
        if record.kind == "companion":
            name = PurePosixPath(logical_path).name
            previous = auxiliary.get(name)
            if previous is not None and previous != content:
                raise codec_support.VerificationBundleError(
                    f"verification job '{job.property_id}' has conflicting "
                    f"auxiliary file '{name}'"
                )
            auxiliary[name] = content
            continue
        if record.kind not in {"implementation", "harness"}:
            raise codec_support.VerificationBundleError(
                f"verification job '{job.property_id}' cannot execute bundle "
                f"file kind '{record.kind}' as Verilog"
            )
        try:
            source_parts.append(content.decode("utf-8"))
        except UnicodeDecodeError as error:
            raise codec_support.VerificationBundleError(
                f"verification source for '{job.property_id}' is not UTF-8"
            ) from error
    return "\n".join(source_parts), auxiliary


def _execution_diagnostic_sources(
    loaded: bundle_codec.LoadedVerificationBundle,
    job: bundle_codec.VerificationJob,
) -> tuple[GeneratedDiagnosticContext, ...]:
    """Bind validated published source maps to exact implementation slices."""

    records = {item.logical_path: item for item in loaded.manifest.files}
    maps: list[GeneratedSourceMap] = []
    for path in job.source_map_files:
        try:
            maps.append(GeneratedSourceMap.from_json(loaded.read_bytes(path)))
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise codec_support.VerificationBundleError(
                f"verification source map '{path}' is invalid: {error}"
            ) from error
    if not maps:
        return ()
    contexts: list[GeneratedDiagnosticContext] = []
    line_offset = 0
    for logical_path in job.source_files:
        record = records.get(logical_path)
        if record is None or record.kind == "companion":
            continue
        if record.kind not in {"implementation", "harness"}:
            continue
        try:
            text = loaded.read_bytes(logical_path).decode("utf-8")
        except UnicodeDecodeError:
            continue
        if record.kind == "implementation":
            digest = hashlib.sha256(text.encode()).hexdigest()
            for source_map in maps:
                if (
                    source_map.artifact_hash == digest
                    and (job.backend is None or source_map.backend == job.backend)
                    and (
                        job.selected_ir_identity is None
                        or source_map.selected_ir_identity
                        == job.selected_ir_identity
                    )
                ):
                    contexts.append(GeneratedDiagnosticContext(
                        source_map,
                        text,
                        line_offset,
                    ))
        # _execution_inputs joins each non-companion text with one newline.
        line_offset += text.count("\n") + 1
    return tuple(contexts)


def _trace_binding_records(
    loaded: bundle_codec.LoadedVerificationBundle,
    *,
    route: str | None = None,
) -> tuple[formal_trace.TraceBinding, ...]:
    payload = loaded.verification_ir.get("payload", {})
    raw: object = ()
    if isinstance(payload, Mapping):
        binding_sets = payload.get("binding_sets")
        if isinstance(binding_sets, list):
            matches = tuple(
                item for item in binding_sets
                if isinstance(item, Mapping) and item.get("route") == route
            )
            if route is not None and len(matches) == 1:
                raw = matches[0].get("bindings", ())
            elif route is None and len(binding_sets) == 1:
                only = binding_sets[0]
                raw = only.get("bindings", ()) if isinstance(only, Mapping) else ()
    records: list[formal_trace.TraceBinding] = []
    if not isinstance(raw, list):
        return ()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        semantic_id = item.get("semantic_signal_id")
        rtl_name = item.get("rtl_name")
        width = item.get("width")
        if (
            isinstance(semantic_id, str)
            and isinstance(rtl_name, str)
            and isinstance(width, int)
            and not isinstance(width, bool)
        ):
            canonical_type = (
                item.get("canonical_type")
                if isinstance(item.get("canonical_type"), str)
                else None
            )
            signedness = (
                item.get("signedness")
                if isinstance(item.get("signedness"), str)
                else None
            )
            if semantic_id in {
                "clock", "reset", "trace:reset", "physical_reset",
            } and width == 1:
                canonical_type = "bit"
                signedness = "bit"
            records.append(formal_trace.TraceBinding(
                semantic_id,
                rtl_name,
                width,
                canonical_type=canonical_type,
                signedness=signedness,
            ))
    return tuple(sorted(records, key=lambda item: item.semantic_signal_id))


def _semantic_trace_values(
    work_directory: Path | None,
    *,
    cycle: int | None,
    bindings: tuple[formal_trace.TraceBinding, ...],
) -> tuple[tuple[str, str], ...]:
    if work_directory is None:
        return ()
    traces = tuple(sorted(work_directory.glob("**/trace*.vcd")))
    if not traces:
        return ()
    return formal_trace.decode_vcd_trace(
        traces[-1], cycle=cycle, bindings=bindings
    ).values


def _semantic_trace_snapshot(
    work_directory: Path | None,
    *,
    cycle: int | None,
    bindings: tuple[formal_trace.TraceBinding, ...],
) -> formal_trace.FormalTraceSnapshot:
    """Decode one same-cycle safety verification frame through the common trace utility."""

    if work_directory is None:
        return formal_trace.FormalTraceSnapshot(cycle, cycle, None, None, ())
    traces = tuple(sorted(work_directory.glob("**/trace*.vcd")))
    if not traces:
        return formal_trace.FormalTraceSnapshot(cycle, cycle, None, None, ())
    return formal_trace.decode_vcd_trace(
        traces[-1],
        cycle=cycle,
        bindings=bindings,
        comparison_window=ComparisonWindow.same_cycle(),
    )


def _relevant_verification_tool_versions(
    config: bundle_report.VerificationRunConfig,
    versions: Iterable[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    """Return only tools which can affect the configured execution route."""

    required = (
        {"yosys", "sby", "yosys-smtbmc", config.solver}
        if config.engine == "sby"
        else {config.engine, config.solver}
    )
    selected: dict[str, str] = {}
    for name, version in versions:
        codec_support.require_string(name, "verification cache tool name")
        codec_support.require_string(version, "verification cache tool version")
        if name in selected:
            raise codec_support.VerificationBundleError(
                f"duplicate verification cache tool version '{name}'"
            )
        if name in required:
            selected[name] = version
    return tuple(sorted(selected.items()))


def _verification_result_cache_identity(
    loaded: bundle_codec.LoadedVerificationBundle,
    job: bundle_codec.VerificationJob,
    *,
    config: bundle_report.VerificationRunConfig,
    tool_versions: Iterable[tuple[str, str]],
) -> dict[str, object]:
    job_data = job.to_data()
    route = {
        "artifact_hash": job.artifact_hash,
        "backend": job.backend,
        "binding_identity": job.binding_identity,
        "route": job.route,
        "selected_ir_identity": job.selected_ir_identity,
    }
    # ``jobs`` controls host scheduling only.  It cannot change one solver
    # invocation and therefore deliberately does not fragment result reuse.
    execution_config = {
        "depth": config.depth,
        "engine": config.engine,
        "mode": "cover" if job.kind == "cover" else config.mode.value,
        "solver": config.solver,
        "timeout_seconds": config.timeout_seconds,
    }
    return {
        "bundle_identity": (
            loaded.manifest.bundle_identity or loaded.manifest.computed_identity
        ),
        "config": execution_config,
        "job": job_data,
        "job_identity": stable_digest(job_data),
        "route": route,
        "run_report_schema": bundle_codec.VERIFICATION_RUN_REPORT_SCHEMA,
        "run_report_schema_version": bundle_codec.VERIFICATION_RUN_REPORT_SCHEMA_VERSION,
        "schema": bundle_codec.VERIFICATION_RESULT_CACHE_SCHEMA,
        "schema_version": bundle_codec.VERIFICATION_RESULT_CACHE_SCHEMA_VERSION,
        "tool_versions": [
            list(item)
            for item in _relevant_verification_tool_versions(config, tool_versions)
        ],
    }


def verification_result_cache_key(
    bundle: bundle_codec.LoadedVerificationBundle | Path,
    job: bundle_codec.VerificationJob,
    *,
    config: bundle_report.VerificationRunConfig,
    tool_versions: Iterable[tuple[str, str]],
) -> str:
    """Return the exact content key for one verification-job execution."""

    loaded = bundle_io.load_verification_bundle(bundle) if isinstance(bundle, Path) else bundle
    return stable_digest(_verification_result_cache_identity(
        loaded,
        job,
        config=config,
        tool_versions=tool_versions,
    ))


def _decisive_verification_result(result: bundle_report.VerificationJobResult) -> bool:
    if result.kind == "safety":
        return result.status in {
            ir_formal.FormalStatus.FAILED.value,
            ir_formal.FormalStatus.BOUNDED_PASS.value,
            ir_formal.FormalStatus.PROVEN.value,
        }
    if result.kind == "cover":
        return result.status in {
            ir_formal.CoverStatus.WITNESSED.value,
            ir_formal.CoverStatus.BOUNDED_UNREACHED.value,
        }
    return False


def _validate_cached_verification_result(
    result: bundle_report.VerificationJobResult,
    job: bundle_codec.VerificationJob,
    *,
    config: bundle_report.VerificationRunConfig,
    relevant_tool_versions: tuple[tuple[str, str], ...],
) -> None:
    expected = {
        "property_id": job.property_id,
        "kind": job.kind,
        "mode": "cover" if job.kind == "cover" else config.mode.value,
        "engine": config.engine,
        "solver": config.solver,
        "depth": config.depth,
        "source_origin": job.source_origin,
        "route": job.route,
        "backend": job.backend,
        "artifact_hash": job.artifact_hash,
        "binding_identity": job.binding_identity,
        "selected_ir_identity": job.selected_ir_identity,
        "scope_id": job.scope_id,
        "assumption_ids": job.assumption_ids,
        "clock_domain": job.clock_domain,
        "reset_domain": job.reset_domain,
        "physical_instance_path": job.physical_instance_path,
    }
    for field_name, expected_value in expected.items():
        if getattr(result, field_name) != expected_value:
            raise codec_support.VerificationBundleError(
                f"cached verification result {field_name.replace('_', ' ')} "
                "does not match the requested job"
            )
    if result.work_directory is not None:
        raise codec_support.VerificationBundleError(
            "cached verification results must not retain a mutable work directory"
        )
    if result.tool_versions != relevant_tool_versions:
        raise codec_support.VerificationBundleError(
            "cached verification result tool versions do not match the execution route"
        )
    if not _decisive_verification_result(result):
        raise codec_support.VerificationBundleError(
            "cached verification result is not a decisive solver outcome"
        )


def _load_verification_result_cache(
    cache_directory: Path | None,
    loaded: bundle_codec.LoadedVerificationBundle,
    job: bundle_codec.VerificationJob,
    *,
    config: bundle_report.VerificationRunConfig,
    tool_versions: tuple[tuple[str, str], ...],
) -> tuple[bundle_report.VerificationJobResult | None, str | None, dict[str, object] | None]:
    if cache_directory is None:
        return None, None, None
    identity = _verification_result_cache_identity(
        loaded, job, config=config, tool_versions=tool_versions
    )
    key = stable_digest(identity)
    canonical = cache_directory / "safety verification" / "results" / f"{key}.json"
    payload, diagnostic = load_json_object(canonical)
    if payload is None or diagnostic is not None:
        return None, key, identity
    try:
        codec_support.require_exact_keys(
            payload,
            required=(
                "identity", "identity_hash", "key", "result", "result_hash",
                "schema", "schema_version",
            ),
            description="verification result cache entry",
        )
        if (
            payload["schema"] != bundle_codec.VERIFICATION_RESULT_CACHE_SCHEMA
            or payload["schema_version"] != bundle_codec.VERIFICATION_RESULT_CACHE_SCHEMA_VERSION
        ):
            raise codec_support.VerificationBundleError(
                "verification result cache schema does not match"
            )
        stored_key = payload["key"]
        identity_hash = payload["identity_hash"]
        result_hash = payload["result_hash"]
        for value, description in (
            (stored_key, "verification result cache key"),
            (identity_hash, "verification result cache identity hash"),
            (result_hash, "verification result cache result hash"),
        ):
            if not isinstance(value, str) or codec_support.HASH_PATTERN.fullmatch(value) is None:
                raise codec_support.VerificationBundleError(
                    f"{description} must be lowercase SHA-256"
                )
        stored_identity = payload["identity"]
        result_data = payload["result"]
        if not isinstance(stored_identity, Mapping):
            raise codec_support.VerificationBundleError(
                "verification result cache identity must be an object"
            )
        if not isinstance(result_data, Mapping):
            raise codec_support.VerificationBundleError(
                "verification result cache payload must be an object"
            )
        if stored_identity != identity:
            raise codec_support.VerificationBundleError(
                "verification result cache identity does not match the request"
            )
        if stable_digest(stored_identity) != identity_hash or identity_hash != key:
            raise codec_support.VerificationBundleError(
                "verification result cache identity hash does not match"
            )
        if stored_key != key:
            raise codec_support.VerificationBundleError(
                "verification result cache key does not match"
            )
        if stable_digest(result_data) != result_hash:
            raise codec_support.VerificationBundleError(
                "verification result cache payload hash does not match"
            )
        result = bundle_report.VerificationJobResult.from_data(result_data)
        relevant_versions = _relevant_verification_tool_versions(
            config, tool_versions
        )
        _validate_cached_verification_result(
            result,
            job,
            config=config,
            relevant_tool_versions=relevant_versions,
        )
        # Reports describe the current discovery snapshot.  The entry itself
        # stores only route-relevant versions, so an unrelated installed tool
        # neither invalidates the cache nor leaks stale inventory into a report.
        return replace(result, tool_versions=tool_versions), key, identity
    except (KeyError, TypeError, codec_support.VerificationBundleError):
        # A malformed, partial, stale, or tampered entry is never evidence.
        # Treat it as a miss so real execution can repair it atomically.
        return None, key, identity


def _publish_verification_result_cache(
    cache_directory: Path | None,
    key: str | None,
    identity: Mapping[str, object] | None,
    job: bundle_codec.VerificationJob,
    result: bundle_report.VerificationJobResult,
    *,
    config: bundle_report.VerificationRunConfig,
    tool_versions: tuple[tuple[str, str], ...],
) -> None:
    if cache_directory is None or key is None or identity is None:
        return
    if not _decisive_verification_result(result):
        return
    relevant_versions = _relevant_verification_tool_versions(config, tool_versions)
    stored_result = replace(
        result,
        tool_versions=relevant_versions,
        work_directory=None,
    )
    _validate_cached_verification_result(
        stored_result,
        job,
        config=config,
        relevant_tool_versions=relevant_versions,
    )
    # Defend the internal caller boundary too: the path key must bind the exact
    # identity published inside the cache envelope.
    if identity.get("job") != job.to_data():
        raise codec_support.VerificationBundleError(
            "verification result cache publication job does not match its identity"
        )
    if codec_support.HASH_PATTERN.fullmatch(key) is None or stable_digest(identity) != key:
        raise codec_support.VerificationBundleError(
            "verification result cache publication key does not match its identity"
        )
    result_data = stored_result.to_data()
    publish_json_atomically(
        cache_directory / "safety verification" / "results" / f"{key}.json",
        {
            "identity": dict(identity),
            "identity_hash": key,
            "key": key,
            "result": result_data,
            "result_hash": stable_digest(result_data),
            "schema": bundle_codec.VERIFICATION_RESULT_CACHE_SCHEMA,
            "schema_version": bundle_codec.VERIFICATION_RESULT_CACHE_SCHEMA_VERSION,
        },
    )


@dataclass(frozen=True)
class _VerificationExecution:
    result: bundle_report.VerificationJobResult
    job: bundle_codec.VerificationJob
    cache_key: str | None = None
    cache_identity: Mapping[str, object] | None = None
    cache_hit: bool = False


def _run_verification_bundle_unlocked(
    bundle: bundle_codec.LoadedVerificationBundle | Path,
    *,
    config: bundle_report.VerificationRunConfig = bundle_report.VerificationRunConfig(),
    work_directory: Path | None = None,
    cache_directory: Path | None = None,
    job_kinds: frozenset[str] | None = None,
    toolchain: FormalToolchainContext | None = None,
) -> bundle_report.VerificationRunReport:
    """Replay executable safety and bounded-cover jobs."""

    loaded = (
        bundle_io.load_verification_bundle(bundle)
        if isinstance(bundle, Path)
        else bundle
    )
    bundle_root = loaded.directory.resolve(strict=False)
    execution_root: Path | None = None
    if work_directory is not None:
        candidate = Path(work_directory).resolve(strict=False)
        try:
            candidate.relative_to(bundle_root)
        except ValueError:
            pass
        else:
            raise codec_support.VerificationBundleError(
                "verification work directory must be outside the immutable bundle"
            )
        bundle_token = stable_digest(
            loaded.manifest.bundle_identity or loaded.manifest.computed_identity
        )[:16]
        run_token = stable_digest(config.to_data())[:16]
        execution_root = candidate / (
            f"{bundle_token}-{config.mode.value}-{run_token}"
        )
        execution_root.mkdir(parents=True, exist_ok=True)
    cache_root: Path | None = None
    if cache_directory is not None:
        candidate = Path(cache_directory).resolve(strict=False)
        try:
            candidate.relative_to(bundle_root)
        except ValueError:
            pass
        else:
            raise codec_support.VerificationBundleError(
                "verification cache directory must be outside the immutable bundle"
            )
        cache_root = candidate
    selected_jobs = tuple(
        (ordinal, job)
        for ordinal, job in enumerate(loaded.manifest.jobs)
        if job_kinds is None or job.kind in job_kinds
    )
    if not selected_jobs:
        raise codec_support.VerificationBundleError("verification execution selected no jobs")

    # Structured skips are reports, not solver requests.  In particular, a
    # bundle whose assumptions or observations could not be connected must be
    # replayable without probing the host for Yosys/SBY/a solver.  Discover one
    # immutable snapshot only when at least one selected job can actually use
    # the supported executor.
    needs_toolchain = config.engine == "sby" and any(
        job.executable and job.kind in {"safety", "cover"}
        for _, job in selected_jobs
    )
    if needs_toolchain:
        toolchain = toolchain or FormalToolchainContext.discover(
            engine=config.engine, solver=config.solver
        )
        if toolchain.engine != config.engine or toolchain.solver != config.solver:
            raise codec_support.VerificationBundleError(
                "verification toolchain context does not match the run configuration"
            )
        versions = toolchain.versions
    else:
        toolchain = None
        versions = ()

    def result_mode(job: bundle_codec.VerificationJob) -> str:
        return "cover" if job.kind == "cover" else config.mode.value

    def validate_attribution(
        job: bundle_codec.VerificationJob,
        *,
        mode: str,
        engine: str | None,
        solver: str | None,
        depth: int | None,
        source_origin: source.SourceOrigin | None,
    ) -> None:
        if mode != result_mode(job):
            raise codec_support.VerificationBundleError(
                f"verification runner returned mode '{mode}' for job '{job.property_id}'"
            )
        if engine != config.engine or solver != config.solver or depth != config.depth:
            raise codec_support.VerificationBundleError(
                f"verification runner returned mismatched execution metadata for "
                f"job '{job.property_id}'"
            )
        if source_origin != job.source_origin:
            raise codec_support.VerificationBundleError(
                f"verification runner returned mismatched source origin for "
                f"job '{job.property_id}'"
            )

    def skipped(
        job: bundle_codec.VerificationJob,
        reason: str,
    ) -> bundle_report.VerificationJobResult:
        return bundle_report.VerificationJobResult(
            property_id=job.property_id,
            kind=job.kind,
            status=ir_formal.FormalStatus.SKIPPED.value,
            mode=result_mode(job),
            engine=config.engine,
            solver=config.solver,
            depth=config.depth,
            reason=reason,
            source_origin=job.source_origin,
            tool_versions=versions,
            route=job.route,
            backend=job.backend,
            artifact_hash=job.artifact_hash,
            binding_identity=job.binding_identity,
            selected_ir_identity=job.selected_ir_identity,
            scope_id=job.scope_id,
            assumption_ids=job.assumption_ids,
            clock_domain=job.clock_domain,
            reset_domain=job.reset_domain,
            physical_instance_path=job.physical_instance_path,
            clock_domain_contract=job.clock_domain_contract,
            physical_domain_identity=job.physical_domain_identity,
        )

    def execute(entry: tuple[int, bundle_codec.VerificationJob]) -> _VerificationExecution:
        ordinal, job = entry
        trace_bindings = _trace_binding_records(loaded, route=job.route)
        diagnostic_sources = _execution_diagnostic_sources(loaded, job)
        job_work_directory = (
            None
            if execution_root is None
            else execution_root
            / f"job-{ordinal:04d}-{stable_digest(job.property_id)[:16]}"
        )
        if job.kind not in {"safety", "cover"}:
            return _VerificationExecution(
                skipped(
                    job,
                    f"verification executor for job kind '{job.kind}' is unavailable",
                ),
                job,
            )
        if not job.executable:
            return _VerificationExecution(
                skipped(job, job.reason or "verification job is not executable"),
                job,
            )
        if config.engine != "sby":
            return _VerificationExecution(
                skipped(
                    job, f"verification engine '{config.engine}' is unsupported"
                ),
                job,
            )
        cached, cache_key, cache_identity = _load_verification_result_cache(
            cache_root,
            loaded,
            job,
            config=config,
            tool_versions=versions,
        )
        if cached is not None:
            return _VerificationExecution(
                cached, job, cache_key, cache_identity, True
            )
        source, auxiliary_files = _execution_inputs(loaded, job)
        if job.kind == "cover":
            cover_result = run_verilog_cover(
                source,
                top=job.top,
                property_id=job.property_id,
                depth=config.depth,
                solver=config.solver,
                engine=config.engine,
                source_origin=job.source_origin,
                systemverilog=job.systemverilog,
                timeout_seconds=config.timeout_seconds,
                work_directory=job_work_directory,
                auxiliary_files=auxiliary_files,
                toolchain=toolchain,
                diagnostic_sources=diagnostic_sources,
            )
            if cover_result.property_id != job.property_id:
                raise codec_support.VerificationBundleError(
                    f"verification runner returned property '{cover_result.property_id}' "
                    f"for job '{job.property_id}'"
                )
            if (
                cover_result.witness is not None
                and cover_result.witness.property_id != job.property_id
            ):
                raise codec_support.VerificationBundleError(
                    f"verification witness property does not match job '{job.property_id}'"
                )
            validate_attribution(
                job,
                mode="cover",
                engine=cover_result.engine,
                solver=cover_result.solver,
                depth=cover_result.depth,
                source_origin=cover_result.source_origin,
            )
            if cover_result.tool_versions != versions:
                raise codec_support.VerificationBundleError(
                    f"verification job '{job.property_id}' changed tool inventory"
                )
            if cover_result.witness is not None:
                values = _semantic_trace_values(
                    job_work_directory,
                    cycle=cover_result.witness.cycle,
                    bindings=trace_bindings,
                )
                if values:
                    cover_result = replace(
                        cover_result,
                        witness=replace(cover_result.witness, values=values),
                    )
            return _VerificationExecution(
                bundle_report.VerificationJobResult.from_cover_result(
                    job, cover_result, work_directory=job_work_directory
                ),
                job,
                cache_key,
                cache_identity,
            )
        formal_result = run_verilog_formal(
            source,
            top=job.top,
            property_id=job.property_id,
            mode=config.mode,
            depth=config.depth,
            solver=config.solver,
            engine=config.engine,
            source_origin=job.source_origin,
            systemverilog=job.systemverilog,
            timeout_seconds=config.timeout_seconds,
            work_directory=job_work_directory,
            auxiliary_files=auxiliary_files,
            toolchain=toolchain,
            counterexample_pre_edge=True,
            diagnostic_sources=diagnostic_sources,
        )
        if formal_result.property_id != job.property_id:
            raise codec_support.VerificationBundleError(
                f"verification runner returned property '{formal_result.property_id}' "
                f"for job '{job.property_id}'"
            )
        if (
            formal_result.counterexample is not None
            and formal_result.counterexample.property_id != job.property_id
        ):
            raise codec_support.VerificationBundleError(
                f"verification counterexample property does not match job '{job.property_id}'"
            )
        validate_attribution(
            job,
            mode=formal_result.mode.value,
            engine=formal_result.engine,
            solver=formal_result.solver,
            depth=formal_result.depth,
            source_origin=formal_result.source_origin,
        )
        if formal_result.tool_versions != versions:
            raise codec_support.VerificationBundleError(
                f"verification job '{job.property_id}' changed tool inventory"
            )
        trace_snapshot = None
        if (
            formal_result.counterexample is not None
            and formal_result.counterexample.cycle is not None
        ):
            trace_snapshot = _semantic_trace_snapshot(
                job_work_directory,
                cycle=formal_result.counterexample.cycle,
                bindings=trace_bindings,
            )
            formal_result = replace(
                formal_result,
                counterexample=replace(
                    formal_result.counterexample,
                    cycle=trace_snapshot.failure_cycle,
                    values=(
                        trace_snapshot.values
                        if trace_snapshot.values
                        else formal_result.counterexample.values
                    ),
                ),
            )
        return _VerificationExecution(
            bundle_report.VerificationJobResult.from_formal_result(
                job,
                formal_result,
                work_directory=job_work_directory,
                trace_snapshot=trace_snapshot,
            ),
            job,
            cache_key,
            cache_identity,
        )

    if config.jobs == 1 or len(selected_jobs) == 1:
        executions = [execute(item) for item in selected_jobs]
    else:
        with ThreadPoolExecutor(max_workers=min(config.jobs, len(selected_jobs))) as pool:
            # ``executor.map`` retains input order even though tools complete
            # independently, keeping reports and identities deterministic.
            executions = list(pool.map(execute, selected_jobs))
    results = [item.result for item in executions]
    payload = loaded.verification_ir.get("payload", {})
    dependencies = (
        payload.get("vacuity_dependencies", {})
        if isinstance(payload, Mapping) else {}
    )
    if isinstance(dependencies, Mapping) and job_kinds is None:
        by_id = {item.property_id: item for item in results}
        revised: list[bundle_report.VerificationJobResult] = []
        for item in results:
            feasibility_id = dependencies.get(item.property_id)
            feasibility = by_id.get(feasibility_id) if isinstance(feasibility_id, str) else None
            if feasibility_id is not None and (
                feasibility is None or feasibility.kind != "cover"
            ):
                raise codec_support.VerificationBundleError(
                    f"verification result '{item.property_id}' has an invalid "
                    "vacuity dependency"
                )
            if (
                feasibility is not None
                and feasibility.status in {
                    ir_formal.CoverStatus.BOUNDED_UNREACHED.value,
                    ir_formal.CoverStatus.UNKNOWN.value,
                    ir_formal.CoverStatus.SKIPPED.value,
                }
                and item.status in {
                    ir_formal.FormalStatus.BOUNDED_PASS.value,
                    ir_formal.FormalStatus.PROVEN.value,
                }
            ):
                revised.append(replace(
                    item,
                    status=ir_formal.FormalStatus.UNKNOWN.value,
                    reason=(
                        f"verification scope is vacuous: feasibility cover "
                        f"'{feasibility.property_id}' has status "
                        f"'{feasibility.status}'"
                    ),
                ))
            else:
                revised.append(item)
        results = revised
    for execution, result in zip(executions, results, strict=True):
        if execution.cache_hit:
            continue
        _publish_verification_result_cache(
            cache_root,
            execution.cache_key,
            execution.cache_identity,
            execution.job,
            result,
            config=config,
            tool_versions=versions,
        )
    report = bundle_report.VerificationRunReport(
        loaded.manifest.bundle_identity or loaded.manifest.computed_identity,
        loaded.manifest.top,
        config,
        tuple(results),
        versions,
    )
    return report


def run_verification_bundle(
    bundle: bundle_codec.LoadedVerificationBundle | Path,
    *,
    config: bundle_report.VerificationRunConfig = bundle_report.VerificationRunConfig(),
    work_directory: Path | None = None,
    cache_directory: Path | None = None,
    job_kinds: frozenset[str] | None = None,
    toolchain: FormalToolchainContext | None = None,
) -> bundle_report.VerificationRunReport:
    """Replay jobs under one exclusive deterministic execution-root lease."""

    loaded = bundle_io.load_verification_bundle(bundle) if isinstance(bundle, Path) else bundle
    lease: _ExecutionRootLease | None = None
    if work_directory is not None:
        bundle_root = loaded.directory.resolve(strict=False)
        candidate = Path(work_directory).resolve(strict=False)
        try:
            candidate.relative_to(bundle_root)
        except ValueError:
            pass
        else:
            raise codec_support.VerificationBundleError(
                "verification work directory must be outside the immutable bundle"
            )
        bundle_token = stable_digest(
            loaded.manifest.bundle_identity or loaded.manifest.computed_identity
        )[:16]
        run_token = stable_digest(config.to_data())[:16]
        execution_root = candidate / (
            f"{bundle_token}-{config.mode.value}-{run_token}"
        )
        execution_root.mkdir(parents=True, exist_ok=True)
        lease = _ExecutionRootLease(execution_root)
    try:
        return _run_verification_bundle_unlocked(
            loaded,
            config=config,
            work_directory=work_directory,
            cache_directory=cache_directory,
            job_kinds=job_kinds,
            toolchain=toolchain,
        )
    finally:
        if lease is not None:
            lease.close()


def run_verification_bundle_staged(
    bundle: bundle_codec.LoadedVerificationBundle | Path,
    *,
    config: bundle_report.VerificationRunConfig = bundle_report.VerificationRunConfig(),
    work_directory: Path | None = None,
    cache_directory: Path | None = None,
    tool_resolver: object | None = None,
) -> bundle_report.VerificationRunReport:
    """Run BMC before an optional unbounded proof attempt.

    A prove request never bypasses the bounded mutation-catching pass.  The
    returned evidence is the BMC report when that prerequisite is not clean,
    otherwise it is the proof report.  The two executions use distinct
    configuration-derived subdirectories below ``work_directory``.
    """

    loaded = bundle_io.load_verification_bundle(bundle) if isinstance(bundle, Path) else bundle
    needs_toolchain = config.engine == "sby" and any(
        job.executable and job.kind in {"safety", "cover"}
        for job in loaded.manifest.jobs
    )
    toolchain = None
    if needs_toolchain:
        resolve = getattr(tool_resolver, "formal_context", None)
        toolchain = (
            resolve(engine=config.engine, solver=config.solver)
            if callable(resolve)
            else FormalToolchainContext.discover(
                engine=config.engine, solver=config.solver
            )
        )
    if config.mode is ir_formal.ProofMode.BMC:
        return run_verification_bundle(
            loaded,
            config=config,
            work_directory=work_directory,
            cache_directory=cache_directory,
            toolchain=toolchain,
        )
    bounded = run_verification_bundle(
        loaded,
        config=replace(config, mode=ir_formal.ProofMode.BMC),
        work_directory=work_directory,
        cache_directory=cache_directory,
        toolchain=toolchain,
    )
    # Covers are advisory unless they are the explicit feasibility dependency
    # of a safety goal.  Vacuity processing above has already converted such
    # dependent safety results to UNKNOWN.  An unrelated unavailable cover
    # must therefore remain visible in the final report without preventing an
    # otherwise clean safety job from advancing to PROVE.
    bounded_safety = tuple(
        item for item in bounded.results if item.kind == "safety"
    )
    if any(
        item.status != ir_formal.FormalStatus.BOUNDED_PASS.value
        for item in bounded_safety
    ):
        return bounded
    if not bounded_safety:
        return bounded
    proof = run_verification_bundle(
        loaded,
        config=config,
        work_directory=work_directory,
        cache_directory=cache_directory,
        job_kinds=frozenset({"safety"}),
        toolchain=toolchain,
    )
    proof_by_id = {item.property_id: item for item in proof.results}
    merged = tuple(
        proof_by_id.get(item.property_id, item)
        for item in bounded.results
    )
    return bundle_report.VerificationRunReport(
        proof.bundle_identity,
        proof.top,
        config,
        merged,
        proof.tool_versions,
        bounded.results,
    )
