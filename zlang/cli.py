"""Command-line interface for the prototype compiler."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from zlang._version import __version__
from zlang.source_identity import (
    CLI_NAME,
    PUBLIC_LANGUAGE_NAME,
    SOURCE_SUFFIX,
    SourceExtensionError,
    validate_source_path,
)
from zlang.compiler import (
    TopSelectionError,
    check_file_snapshot,
    compile_file_snapshot,
)
from zlang.build_manifest import (
    BuildManifestError,
    CanonicalIrRef,
    ReportRecord,
    ToolExecutionRecord,
    WholeBuildManifest,
)
from zlang.build_publication import (
    PhysicalPublication,
    backend_build_record,
    backend_logical_path,
    dependency_records,
    publish_manifest_atomically,
    published_file_from_path,
    report_from_path,
    source_publication,
)
from zlang.common import stable_digest
from zlang.parser import ParseError, parse
from zlang.semantic import SemanticError
from zlang.costs import CostExtractionError
from zlang.backend.clash import emit, emit_artifact as emit_clash_artifact
from zlang.backend.clash.public_wrapper import (
    ClashPublicTopWrapper,
    ClashPublicWrapperError,
    bind_artifact_to_public_wrapper,
)
from zlang.backend.companions import (
    CompanionArtifact,
    CompanionArtifactError,
    collect_rom_companions,
    publish_companion_bundle,
)
from zlang.backend.source_map import build_generated_source_map
from zlang.backend.systemverilog import (
    ExternalMappingError,
    SystemVerilogEmissionError,
    emit_artifact as emit_systemverilog_artifact,
    emit_formal_artifact as emit_systemverilog_formal_artifact,
    emit_target_artifact,
    build_systemverilog_simulation_state_bundle,
)
from zlang.simulation_state import SimulationStateError
from zlang.backend.external import load_profile_external_mappings
from zlang.implementations import render_implementation_report
from zlang.opt import (
    OptimizationStage,
    SaturationError,
    render as render_optimization_ir,
    render_saturation,
    saturate,
    lower,
    restore,
)
from zlang.opt.identity import CANONICAL_IR_IDENTITY_SCHEMA
from zlang.synthesis import (
    SynthesisFeedbackError,
    YosysTarget,
    characterize_with_yosys,
    render_synthesis_report,
)
from zlang.toolchain import (
    ToolchainError,
    find_clash_executable,
    generate_verilog,
    lint_with_verilator,
)
from zlang.formal import connect_formal_design, emit_harness, emit_sby
from zlang.ir.formal import FormalError, ProofMode
from zlang.verification_bundle import (
    VerificationBundleError,
    VerificationRunConfig,
    load_verification_bundle,
    run_verification_bundle_staged,
)
from zlang.verification_publication import publish_compilation_verification_bundle
from zlang.formal_orchestration import (
    CandidateEquivalenceExecutionReport,
    CompilerFormalExecutionPlan,
    FormalOrchestrationError,
    collect_m39_evidence,
    validate_legacy_formal_view,
)
from zlang.formal_exploration import FormalExplorationConfig, FormalPolicy
from zlang.candidate_equivalence import (
    PreparedCandidateEquivalenceSite,
    execute_prepared_candidate_equivalence,
    prepare_selected_candidate_equivalence,
)
from zlang.compiler_verification_report import CompilerVerificationReport
from zlang.targets import ArchitectureSelectionMode, TargetArchitectureError
from zlang.costs import SourcePolicy
from zlang.diagnostics import Diagnostic, DiagnosticError, diagnostic_from_exception
from zlang.workspace import WorkspaceError
from zlang.project import ProjectLock, ProjectManifest
from zlang.platform_constraints import (
    ConstraintArtifact,
    ConstraintFormat,
    PlatformConstraintError,
    build_constraint_artifact,
    parse_platform_profile,
    publish_constraint_artifact,
)
from zlang.evidence_report import (
    EvidenceReportError,
    build_evidence_report,
    evidence_for_typed_module,
    evidence_for_unexecuted_property,
    evidence_for_validated_timing,
    evidence_from_verification_report,
)


_ARTIFACT_SINK_ATTRIBUTES = (
    "output",
    "systemverilog",
    "experimental_systemverilog",
    "simulation_state_bundle",
    "verilog_dir",
    "implementation_manifest",
    "csr_markdown",
    "csr_json",
    "contracts_sva",
    "high_level_ir",
    "optimization_ir",
    "saturation_report",
    "implementation_report",
    "cost_report",
    "pipeline_report",
    "architecture_report",
    "exploration_report",
    "implementation_policy_report",
    "backend_implementation_report",
    "formal_harness",
    "formal_sby",
    "verification_bundle",
    "verification_report",
    "synthesis_report",
    "source_map",
    "constraints_xdc",
    "constraints_sdc",
    "evidence_report",
    "build_manifest",
)

_OWNED_OUTPUT_DIRECTORY_ATTRIBUTES = (
    "verilog_dir",
    "verification_bundle",
    "simulation_state_bundle",
    "formal_cache",
    "synthesis_cache",
)

_EXPLICIT_FILE_SINK_ATTRIBUTES = tuple(
    attribute
    for attribute in _ARTIFACT_SINK_ATTRIBUTES
    if attribute not in _OWNED_OUTPUT_DIRECTORY_ATTRIBUTES
)

_SYSTEMVERILOG_ALIAS_ATTRIBUTES = frozenset({
    "systemverilog",
    "experimental_systemverilog",
})


def has_explicit_artifact_sink(arguments: argparse.Namespace) -> bool:
    """Return whether the invocation explicitly requests a file artifact.

    Selection and tool configuration flags are deliberately absent from this
    list.  Keeping the decision in one helper preserves the legacy Clash
    stdout default while making every explicit publication path suppress
    implicit output.
    """
    return any(
        getattr(arguments, attribute, None) is not None
        for attribute in _ARTIFACT_SINK_ATTRIBUTES
    )


def _resolved_cli_path(path: Path) -> Path:
    """Resolve aliases without requiring a not-yet-created output to exist."""

    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"cannot resolve publication path '{path}': {error}") from error


def _option_for_attribute(attribute: str) -> str:
    return "--" + attribute.replace("_", "-")


def _paths_overlap_file_and_directory(file_path: Path, directory_path: Path) -> bool:
    """Return whether a file sink and an owned directory cannot coexist safely."""

    return (
        file_path == directory_path
        or file_path.is_relative_to(directory_path)
        or directory_path.is_relative_to(file_path)
    )


def _preflight_explicit_output_paths(arguments: argparse.Namespace) -> None:
    """Reject resolved input/output aliases before compilation writes anything.

    Every explicit file sink must be physically distinct from the root source
    and every other sink.  The two direct-SystemVerilog spellings are the sole
    exception because they are compatibility aliases for one product.  Tool-
    owned output/cache directories must not contain the source or a file sink,
    and cannot be nested below a path which is itself intended to be a file.
    Comparing resolved paths prevents ``..`` spellings and existing symlinks
    from bypassing the checks.
    """

    source_path = _resolved_cli_path(arguments.source)
    file_sinks: list[tuple[str, Path]] = []
    for attribute in _EXPLICIT_FILE_SINK_ATTRIBUTES:
        sink = getattr(arguments, attribute, None)
        if sink is None:
            continue
        sink_path = _resolved_cli_path(sink)
        if sink_path == source_path:
            if attribute == "build_manifest":
                raise ValueError(
                    "--build-manifest output collides with the source file"
                )
            raise ValueError(
                f"explicit sink {_option_for_attribute(attribute)} "
                "collides with the source file"
            )
        file_sinks.append((attribute, sink_path))

    for index, (left_attribute, left_path) in enumerate(file_sinks):
        for right_attribute, right_path in file_sinks[index + 1:]:
            attributes = frozenset({left_attribute, right_attribute})
            if attributes == _SYSTEMVERILOG_ALIAS_ATTRIBUTES:
                if left_path != right_path:
                    raise ValueError(
                        "--systemverilog and --experimental-systemverilog "
                        "select different outputs"
                    )
                continue
            if left_path != right_path:
                continue
            if "build_manifest" in attributes:
                other = (
                    right_attribute
                    if left_attribute == "build_manifest"
                    else left_attribute
                )
                raise ValueError(
                    "--build-manifest output collides with explicit sink "
                    f"{_option_for_attribute(other)}"
                )
            raise ValueError(
                f"explicit sinks {_option_for_attribute(left_attribute)} and "
                f"{_option_for_attribute(right_attribute)} resolve to the same path"
            )

    owned_directories: list[tuple[str, Path]] = []
    for attribute in _OWNED_OUTPUT_DIRECTORY_ATTRIBUTES:
        directory = getattr(arguments, attribute, None)
        if directory is None:
            continue
        directory_path = _resolved_cli_path(directory)
        owned_directories.append((attribute, directory_path))
        option = _option_for_attribute(attribute)
        if _paths_overlap_file_and_directory(source_path, directory_path):
            raise ValueError(
                f"source file collides with explicit output directory {option}"
            )
        for sink_attribute, sink_path in file_sinks:
            if not _paths_overlap_file_and_directory(sink_path, directory_path):
                continue
            if sink_attribute == "build_manifest" and (
                sink_path == directory_path
                or sink_path.is_relative_to(directory_path)
            ):
                raise ValueError(
                    "--build-manifest output is inside explicit output directory "
                    f"{option}"
                )
            raise ValueError(
                f"explicit sink {_option_for_attribute(sink_attribute)} collides "
                f"with explicit output directory {option}"
            )

    for index, (left_attribute, left_path) in enumerate(owned_directories):
        for right_attribute, right_path in owned_directories[index + 1:]:
            if not _paths_overlap_file_and_directory(left_path, right_path):
                continue
            raise ValueError(
                f"explicit output directories {_option_for_attribute(left_attribute)} "
                f"and {_option_for_attribute(right_attribute)} overlap"
            )


def _preflight_compilation_input_paths(arguments: argparse.Namespace, result) -> None:
    """Protect every physical compiler input discovered during compilation.

    File-backed project and stdlib resolution is complete when this runs, but
    no synthesis, backend, report, or companion output has yet been written.
    Physical paths remain local overwrite guards and never enter compiler or
    artifact identities.
    """

    inputs = result.physical_inputs.all_paths
    for attribute in _EXPLICIT_FILE_SINK_ATTRIBUTES:
        sink = getattr(arguments, attribute, None)
        if sink is None:
            continue
        sink_path = _resolved_cli_path(sink)
        collision = next((path for path in inputs if path == sink_path), None)
        if collision is not None:
            raise ValueError(
                f"explicit sink {_option_for_attribute(attribute)} collides "
                f"with compilation input '{collision}'"
            )

    for attribute in _OWNED_OUTPUT_DIRECTORY_ATTRIBUTES:
        directory = getattr(arguments, attribute, None)
        if directory is None:
            continue
        directory_path = _resolved_cli_path(directory)
        collision = next(
            (
                path for path in inputs
                if _paths_overlap_file_and_directory(path, directory_path)
            ),
            None,
        )
        if collision is not None:
            raise ValueError(
                f"explicit output directory {_option_for_attribute(attribute)} "
                f"collides with compilation input '{collision}'"
            )


def _preflight_companion_paths(
    arguments: argparse.Namespace,
    *,
    systemverilog_output: Path | None,
    clash_companions: Sequence[CompanionArtifact],
    direct_companions: Sequence[CompanionArtifact],
    compilation_inputs: Sequence[Path],
) -> None:
    """Reject deterministic companion aliases before publishing any product."""

    destinations: list[tuple[str, Path, CompanionArtifact]] = []
    if arguments.output is not None:
        destinations.extend(
            (
                "Clash companion",
                arguments.output.parent / companion.logical_path,
                companion,
            )
            for companion in clash_companions
        )
    if arguments.verilog_dir is not None:
        destinations.extend(
            (
                "Clash RTL companion",
                arguments.verilog_dir / companion.logical_path,
                companion,
            )
            for companion in clash_companions
        )
    if systemverilog_output is not None:
        destinations.extend(
            (
                "direct-SystemVerilog companion",
                systemverilog_output.parent / companion.logical_path,
                companion,
            )
            for companion in direct_companions
        )

    by_destination: dict[Path, tuple[str, CompanionArtifact]] = {}
    for description, destination, companion in destinations:
        destination_path = _resolved_cli_path(destination)
        previous = by_destination.get(destination_path)
        if previous is not None:
            previous_description, previous_companion = previous
            if previous_companion.file_hash != companion.file_hash:
                raise ValueError(
                    f"deterministic {description} conflicts with deterministic "
                    f"{previous_description} at '{destination_path}'"
                )
            continue
        by_destination[destination_path] = (description, companion)

    file_sinks = tuple(
        (attribute, _resolved_cli_path(sink))
        for attribute in _EXPLICIT_FILE_SINK_ATTRIBUTES
        if (sink := getattr(arguments, attribute, None)) is not None
    )
    input_paths = frozenset(compilation_inputs)
    for destination_path, (description, _) in by_destination.items():
        for sink_attribute, sink_path in file_sinks:
            if destination_path != sink_path:
                continue
            if sink_attribute == "build_manifest":
                raise ValueError(
                    "--build-manifest output collides with deterministic "
                    f"{description}"
                )
            raise ValueError(
                f"deterministic {description} collides with explicit sink "
                f"{_option_for_attribute(sink_attribute)}"
            )
        if destination_path in input_paths:
            raise ValueError(
                f"deterministic {description} collides with compilation input "
                f"'{destination_path}'"
            )


def _fallback_diagnostic(error: BaseException) -> Diagnostic:
    """Attach stable codes to public exceptions not migrated at their source."""

    if isinstance(error, SourceExtensionError):
        return Diagnostic(
            "ZL-SOURCE-EXTENSION",
            str(error),
            fixes=(f"rename the source file to use '{SOURCE_SUFFIX}'",),
        )
    if isinstance(error, TopSelectionError):
        code = "ZL-TOP-001"
    elif isinstance(error, WorkspaceError):
        code = "ZL-PROJECT-001"
    elif isinstance(error, OSError):
        code = "ZL-IO-001"
    else:
        code = "ZL-ERROR-001"
    return diagnostic_from_exception(error, code=code)


def _print_cli_diagnostic(
    error: BaseException,
    diagnostic_format: str,
    *,
    legacy_message: str | None = None,
) -> None:
    """Write one diagnostic without changing legacy text presentation."""

    if diagnostic_format == "json":
        diagnostic = _fallback_diagnostic(error)
        if legacy_message is not None:
            diagnostic = Diagnostic(
                diagnostic.code,
                legacy_message,
                diagnostic.primary,
                diagnostic.notes,
                diagnostic.fixes,
            )
        print(diagnostic.to_json(), file=sys.stderr)
    else:
        print(
            f"{CLI_NAME}: error: {legacy_message if legacy_message is not None else error}",
            file=sys.stderr,
        )


def _query_tool_version(executable: str, argument: str) -> str:
    """Query a tool only after that exact tool has executed successfully."""

    completed = subprocess.run(
        [executable, argument],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        return "version-unreported"
    output = (completed.stdout.strip() or completed.stderr.strip()).splitlines()
    return output[0].strip() if output else "version-unreported"


def _tool_execution(
    *,
    role: str,
    tool: str,
    version: str,
    argv_shape: tuple[str, ...],
    outputs: tuple[str, ...] = (),
) -> ToolExecutionRecord:
    identity = stable_digest({
        "schema": "zlang-tool-execution-v1",
        "role": role,
        "tool": tool,
        "version": version,
        "argv_shape": list(argv_shape),
        "outputs": list(outputs),
    })
    return ToolExecutionRecord(
        execution_id=f"{tool}.{identity}",
        role=role,
        tool=tool,
        version=version,
        argv_shape=argv_shape,
        status="passed",
        exit_code=0,
        outputs=outputs,
    )


def _report_format(path: Path, *, fallback: str = "text") -> str:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix in {".sv", ".sva"}:
        return "systemverilog"
    if suffix in {".sby"}:
        return "sby"
    if suffix in {".md"}:
        return "markdown"
    return fallback


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=CLI_NAME,
        description=f"Compile {PUBLIC_LANGUAGE_NAME} and emit selected backend artifacts"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument("--top", help="select a top module from a multi-module source")
    parser.add_argument(
        "--project",
        type=Path,
        help="use this zlang.toml (or containing directory) instead of parent discovery",
    )
    parser.add_argument(
        "--profile",
        help="select one strict implementation profile from zlang.toml",
    )
    parser.add_argument(
        "--diagnostic-format",
        choices=("text", "json"),
        default="text",
        help="render compiler diagnostics as compatible text or stable JSON",
    )
    parser.add_argument("source", type=Path, help=f"input {SOURCE_SUFFIX} file")
    parser.add_argument(
        "--check",
        action="store_true",
        help="check syntax and semantics without emitting an artifact",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="report successful compilation and explicitly written artifacts",
    )
    parser.add_argument("-o", "--output", type=Path, help="output Clash .hs file")
    parser.add_argument(
        "--systemverilog",
        type=Path,
        help="write direct SystemVerilog for the supported backend subset",
    )
    parser.add_argument(
        "--experimental-systemverilog",
        type=Path,
        help="compatibility alias for --systemverilog",
    )
    parser.add_argument(
        "--simulation-state-bundle",
        type=Path,
        help=(
            "publish simulation-only Verilator VPI state bindings "
            "(requires --systemverilog)"
        ),
    )
    parser.add_argument(
        "--verilog-dir",
        type=Path,
        help="run Clash and retain generated Verilog in this directory",
    )
    parser.add_argument(
        "--constraints-xdc",
        type=Path,
        help="publish a typed single-domain XDC create_clock constraint",
    )
    parser.add_argument(
        "--constraints-sdc",
        type=Path,
        help="publish a typed single-domain SDC create_clock constraint",
    )
    parser.add_argument(
        "--verilator-lint",
        action="store_true",
        help="lint generated Verilog with Verilator (requires --verilog-dir)",
    )
    parser.add_argument("--clash", help="explicit Clash executable")
    parser.add_argument("--verilator", help="explicit Verilator executable")
    parser.add_argument("--yosys", help="explicit Yosys executable")
    parser.add_argument(
        "--target",
        help="select a compiler-shipped target instance (for example xc7z030ffg676-1)",
    )
    parser.add_argument(
        "--target-architecture",
        help="manually select one source-described architecture template",
    )
    parser.add_argument(
        "--target-architecture-mode",
        choices=tuple(item.value for item in ArchitectureSelectionMode),
        default=None,
        help="generic, preferred fallback, or required manual target architecture",
    )
    parser.add_argument(
        "--target-evidence-policy",
        choices=tuple(item.value for item in SourcePolicy),
        default=None,
        help="estimate_only, measured_preferred, or routed-Fmax measured_required",
    )
    parser.add_argument(
        "--implementation-manifest",
        type=Path,
        help="write the versioned selected-resource BackendArtifact manifest",
    )
    parser.add_argument(
        "--source-map",
        type=Path,
        help=(
            "write a generated-line to ZLang-origin sidecar for exactly one "
            "explicit Clash or direct-SystemVerilog output"
        ),
    )
    parser.add_argument(
        "--evidence-report",
        type=Path,
        help="write deterministic typed build evidence",
    )
    parser.add_argument(
        "--evidence-format",
        choices=("text", "json"),
        default="json",
        help="format selected by --evidence-report (default: json)",
    )
    parser.add_argument(
        "--build-manifest",
        type=Path,
        help="write a deterministic whole-build manifest after validating outputs",
    )
    parser.add_argument(
        "--csr-markdown",
        type=Path,
        help="write CSR Markdown documentation (requires a CSR block)",
    )
    parser.add_argument(
        "--csr-json",
        type=Path,
        help="write the software-readable CSR JSON map (requires a CSR block)",
    )
    parser.add_argument(
        "--contracts-sva",
        type=Path,
        help="write bindable SystemVerilog assume/assert contracts",
    )
    parser.add_argument(
        "--high-level-ir",
        type=Path,
        help="write canonical IR before implementation-cost extraction",
    )
    parser.add_argument(
        "--optimization-ir",
        type=Path,
        help="write the normalized canonical optimization IR",
    )
    parser.add_argument(
        "--saturation-report",
        type=Path,
        help="write bounded equality-saturation alternatives for one output",
    )
    parser.add_argument(
        "--saturate-output",
        help="wire output to inspect (requires --saturation-report)",
    )
    parser.add_argument(
        "--implementation-report",
        type=Path,
        help="write explicit implementation applicability and timing metadata",
    )
    parser.add_argument(
        "--cost-report",
        type=Path,
        help="write estimated candidate costs, constraints, and extraction choice",
    )
    parser.add_argument(
        "--pipeline-report",
        type=Path,
        help="write automatic pipeline candidates, constraints, and selection",
    )
    parser.add_argument(
        "--architecture-report",
        type=Path,
        help="write bounded FIR architecture candidates, pruning, and selection",
    )
    parser.add_argument(
        "--exploration-report",
        type=Path,
        help="write unified explore candidate, rejection, and selection details",
    )
    parser.add_argument(
        "--implementation-policy-report",
        type=Path,
        help="write the normalized profile/source/CLI implementation policy",
    )
    parser.add_argument(
        "--backend-implementation-report",
        type=Path,
        help="write independent Clash and direct-SystemVerilog planning results",
    )
    parser.add_argument("--formal-harness", type=Path, help="write the M35 formal checker harness")
    parser.add_argument("--formal-sby", type=Path, help="write the M35 SymbiYosys configuration")
    parser.add_argument("--formal-depth", type=int, default=32, help="bounded formal depth")
    parser.add_argument("--formal-policy", choices=tuple(item.value for item in FormalPolicy),
                        default=None,
                        help="M39 formal exploration eligibility policy")
    parser.add_argument("--formal-max-candidates", type=int, default=8,
                        help="maximum candidates to execute formal proofs for")
    parser.add_argument("--formal-timeout", type=int, default=120,
                        help="per-candidate formal timeout in seconds")
    parser.add_argument(
        "--formal-jobs",
        type=int,
        default=1,
        help=(
            "maximum independent verification-bundle jobs and selected-candidate "
            "sites to run concurrently"
        ),
    )
    parser.add_argument(
        "--formal-cache",
        type=Path,
        help="versioned formal proof and verification-result cache directory",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="execute connected safety checks and bounded cover goals",
    )
    parser.add_argument(
        "--verification-bundle",
        type=Path,
        help="publish an immutable replayable verification bundle",
    )
    parser.add_argument(
        "--verification-report",
        type=Path,
        help="write the --verify result report",
    )
    parser.add_argument(
        "--verification-work-dir",
        type=Path,
        help="retain solver configurations, logs, and traces for --verify",
    )
    parser.add_argument(
        "--verification-format",
        choices=("text", "json"),
        default=None,
        help="verification report format (default: text)",
    )
    parser.add_argument(
        "--verify-require",
        choices=("checked", "proven"),
        default=None,
        help="required safety evidence: bounded checked or unbounded proven",
    )
    parser.add_argument(
        "--synthesis-report",
        type=Path,
        help="write cached Yosys measurements and feedback selection",
    )
    parser.add_argument(
        "--synthesis-cache",
        type=Path,
        help="cache directory for normalized-candidate Yosys measurements",
    )
    parser.add_argument(
        "--synthesis-target",
        choices=("generic-lut6",),
        default="generic-lut6",
        help="Yosys characterization target",
    )
    arguments = parser.parse_args(argv)
    if arguments.check and has_explicit_artifact_sink(arguments):
        parser.error("--check cannot be combined with artifact output options")
    if arguments.check and arguments.verify:
        parser.error("--check cannot be combined with --verify")
    if arguments.verification_report is not None and not arguments.verify:
        parser.error("--verification-report requires --verify")
    if arguments.verification_work_dir is not None and not arguments.verify:
        parser.error("--verification-work-dir requires --verify")
    if arguments.verification_format is not None and not arguments.verify:
        parser.error("--verification-format requires --verify")
    if arguments.verify_require is not None and not arguments.verify:
        parser.error("--verify-require requires --verify")
    if arguments.formal_jobs < 1:
        parser.error("--formal-jobs must be positive")
    if arguments.verilator_lint and arguments.verilog_dir is None:
        parser.error("--verilator-lint requires --verilog-dir")
    if arguments.simulation_state_bundle is not None and not (
        arguments.systemverilog is not None
        or arguments.experimental_systemverilog is not None
    ):
        parser.error("--simulation-state-bundle requires --systemverilog")
    if arguments.saturation_report is not None and arguments.saturate_output is None:
        parser.error("--saturation-report requires --saturate-output")
    if arguments.saturate_output is not None and arguments.saturation_report is None:
        parser.error("--saturate-output requires --saturation-report")
    if arguments.synthesis_report is not None and arguments.synthesis_cache is None:
        parser.error("--synthesis-report requires --synthesis-cache")
    if arguments.synthesis_cache is not None and arguments.synthesis_report is None:
        parser.error("--synthesis-cache requires --synthesis-report")
    constraint_requested = (
        arguments.constraints_xdc is not None
        or arguments.constraints_sdc is not None
    )
    if constraint_requested and arguments.profile is None:
        parser.error("constraint publication requires --profile")
    if constraint_requested:
        requested_backends = sum((
            arguments.output is not None or arguments.verilog_dir is not None,
            arguments.systemverilog is not None
            or arguments.experimental_systemverilog is not None,
        ))
        if requested_backends != 1:
            parser.error(
                "constraint publication requires exactly one Clash or "
                "direct-SystemVerilog backend output"
            )
    try:
        _preflight_explicit_output_paths(arguments)
    except ValueError as error:
        parser.error(str(error))
    if arguments.build_manifest is not None and not any(
        path is not None
        for path in (
            arguments.output,
            arguments.systemverilog,
            arguments.experimental_systemverilog,
            arguments.verilog_dir,
        )
    ):
        parser.error(
            "--build-manifest requires at least one published Clash or "
            "direct-SystemVerilog backend output"
        )

    checked_module_names: tuple[str, ...] = ()
    verification_work_directory = arguments.verification_work_dir
    if arguments.verify and verification_work_directory is None:
        if arguments.verification_bundle is not None:
            verification_work_directory = (
                arguments.verification_bundle.parent
                / f"{arguments.verification_bundle.name}.work"
            )
        else:
            verification_work_directory = Path(tempfile.mkdtemp(
                prefix="zlang-verification-work-"
            ))
    try:
        # This is the sole root-source snapshot used by the CLI.  Decoding
        # bytes directly preserves CRLF and makes the compiled text, semantic
        # digest, and eventual whole-build publication describe the same input.
        validate_source_path(arguments.source)
        source_bytes = arguments.source.read_bytes()
        source_text = source_bytes.decode("utf-8")
        source_digest = hashlib.sha256(source_bytes).hexdigest()
        explicit_sink = has_explicit_artifact_sink(arguments) or arguments.verify
        include_clash = (
            not arguments.check
            and (
                not explicit_sink
                or arguments.output is not None
                or arguments.verilog_dir is not None
            )
        )
        if arguments.check and arguments.top is None:
            syntax = parse(source_text)
            checked_module_names = tuple(
                (*[module.name for module in syntax.submodules], syntax.name)
            )
        else:
            checked_module_names = (
                (arguments.top,) if arguments.top is not None else ()
            )
        compile_tops: tuple[str | None, ...] = (
            checked_module_names if checked_module_names else (arguments.top,)
        )
        result = None
        for compile_top in compile_tops:
            try:
                compile_snapshot = (
                    check_file_snapshot if arguments.check else compile_file_snapshot
                )
                result = compile_snapshot(
                    arguments.source,
                    source_text,
                    source_digest=source_digest,
                    project=arguments.project,
                    profile=arguments.profile,
                    formal_policy=arguments.formal_policy,
                    formal_depth=arguments.formal_depth,
                    formal_max_candidates=arguments.formal_max_candidates,
                    formal_timeout=arguments.formal_timeout,
                    formal_cache=arguments.formal_cache,
                    formal_work_directory=verification_work_directory,
                    top=compile_top,
                    target=arguments.target,
                    architecture=arguments.target_architecture,
                    architecture_mode=arguments.target_architecture_mode,
                    target_evidence_policy=arguments.target_evidence_policy,
                    include_clash=include_clash,
                )
            except (ParseError, SemanticError, TopSelectionError) as error:
                if arguments.check and len(compile_tops) > 1:
                    raise SemanticError(
                        f"while checking module '{compile_top}': {error}"
                    ) from error
                raise
        assert result is not None
    except UnicodeDecodeError as error:
        _print_cli_diagnostic(
            error,
            arguments.diagnostic_format,
            legacy_message=(
                f"cannot read source file '{arguments.source.name}': invalid UTF-8"
            ),
        )
        return 1
    except OSError as error:
        detail = error.strerror or "unable to read source file"
        _print_cli_diagnostic(
            error,
            arguments.diagnostic_format,
            legacy_message=(
                f"cannot read source file '{arguments.source.name}': {detail}"
            ),
        )
        return 1
    except (
        DiagnosticError,
        SourceExtensionError,
        TopSelectionError,
        WorkspaceError,
    ) as error:
        # Keep the Python API typed, but make source failures concise and
        # artifact-safe at the CLI boundary.  argparse usage errors below
        # intentionally retain exit status 2.
        _print_cli_diagnostic(error, arguments.diagnostic_format)
        return 1
    try:
        _preflight_compilation_input_paths(arguments, result)
    except ValueError as error:
        parser.error(str(error))
    selected_platform_profile = None
    if arguments.profile is not None and result.physical_inputs.project_manifest is not None:
        try:
            selected_platform_profile = parse_platform_profile(
                ProjectManifest.load(result.physical_inputs.project_manifest),
                arguments.profile,
            )
        except (PlatformConstraintError, OSError) as error:
            parser.error(str(error))
    if arguments.check:
        checked = (
            f"top {result.ir.name}"
            if arguments.top is not None
            else f"{len(checked_module_names)} module"
            f"{'s' if len(checked_module_names) != 1 else ''}"
        )
        print(
            f"{CLI_NAME}: ok: {arguments.source} "
            f"({checked}; syntax and semantics valid)"
        )
        return 0
    if (arguments.csr_markdown or arguments.csr_json) and not result.ir.csr_blocks:
        parser.error("CSR artifact options require a source CSR block")
    if arguments.contracts_sva is not None and not result.contracts_sva:
        parser.error(
            "--contracts-sva requires at least one source verification declaration"
        )
    if arguments.implementation_report is not None and not result.implementation_report:
        parser.error("--implementation-report requires a source implementation choice")
    if arguments.cost_report is not None and not result.cost_report:
        parser.error("--cost-report requires an automatic implementation choice")
    if arguments.pipeline_report is not None and not result.pipeline_report:
        parser.error("--pipeline-report requires a pipeline(auto) expression")
    if arguments.architecture_report is not None and not result.architecture_report:
        parser.error(
            "--architecture-report requires an architecture(auto) expression"
        )
    if arguments.exploration_report is not None and not result.exploration_report:
        parser.error("--exploration-report requires an explore expression")
    synthesis_report = ""
    synthesis_feedback = None
    if arguments.synthesis_report is not None:
        assert arguments.synthesis_cache is not None
        try:
            feedback = characterize_with_yosys(
                result.ir,
                arguments.synthesis_cache,
                clash_executable=arguments.clash,
                yosys_executable=arguments.yosys,
                target=YosysTarget(arguments.synthesis_target),
            )
        except SynthesisFeedbackError as error:
            parser.error(str(error))
        measured_optimization_ir = lower(
            feedback.module,
            stage=OptimizationStage.SELECTED_ARCHITECTURE,
        )
        measured_ir = restore(measured_optimization_ir)
        if measured_ir != feedback.module:
            raise RuntimeError("measured optimization IR did not restore typed IR")
        result = replace(
            result,
            ir=measured_ir,
            optimization_ir=measured_optimization_ir,
            clash=emit(measured_ir),
            implementation_report=render_implementation_report(measured_ir),
        )
        synthesis_report = render_synthesis_report(feedback)
        synthesis_feedback = feedback
    saturation_report = ""
    if arguments.saturation_report is not None:
        assignments = tuple(
            assignment
            for assignment in result.optimization_ir.assignments
            if assignment.target_name == arguments.saturate_output
            and assignment.signal is None
            and assignment.channel is None
        )
        if len(assignments) != 1:
            parser.error(
                f"--saturate-output '{arguments.saturate_output}' must name "
                "one assigned wire output"
            )
        try:
            saturation_report = render_saturation(
                saturate(
                    result.optimization_ir,
                    assignments[0].expression,
                )
            )
        except SaturationError as error:
            parser.error(str(error))
    systemverilog_output = arguments.systemverilog or arguments.experimental_systemverilog
    rom_companions = collect_rom_companions(result.ir)
    if arguments.source_map is not None:
        selected_backend_outputs = sum(
            path is not None for path in (arguments.output, systemverilog_output)
        )
        if selected_backend_outputs != 1:
            parser.error(
                "--source-map requires exactly one of --output or --systemverilog"
            )
    direct_systemverilog = ""
    direct_artifact = None
    simulation_state_bundle = None
    external_mappings = ()
    direct_companion_paths: tuple[Path, ...] = ()
    clash_companion_paths: tuple[Path, ...] = ()
    clash_rtl_companion_paths: tuple[Path, ...] = ()
    generated_verilog_files: tuple[Path, ...] = ()
    clash_manifest_source_path: Path | None = None
    source_map = None
    tool_executions: list[ToolExecutionRecord] = []
    if systemverilog_output is not None:
        try:
            if result.physical_inputs.project_manifest is not None:
                manifest = ProjectManifest.load(result.physical_inputs.project_manifest)
                assert result.physical_inputs.project_lock is not None
                lock = ProjectLock.load(result.physical_inputs.project_lock)
                external_mappings = load_profile_external_mappings(
                    manifest, lock, arguments.profile, result.ir
                )
            if result.implementation_graph is not None and not result.implementation_graph.is_generic:
                direct_artifact = emit_target_artifact(
                    result.ir,
                    result.implementation_graph,
                    selected_ir_identity=result.selected_ir_identity,
                )
            else:
                direct_artifact = emit_systemverilog_artifact(
                    result.ir,
                    selected_ir_identity=result.selected_ir_identity,
                    external_mappings=external_mappings,
                )
            # A BackendArtifact is the authoritative direct-SV product.  Its
            # text, hash, source-map inputs, and manifest were all derived from
            # the same single renderer invocation above; never render the
            # module a second time merely to obtain the file payload.
            direct_systemverilog = direct_artifact.text
            if arguments.simulation_state_bundle is not None:
                simulation_state_bundle = build_systemverilog_simulation_state_bundle(
                    result.ir, direct_artifact
                )
        except (ExternalMappingError, SystemVerilogEmissionError) as error:
            if arguments.diagnostic_format == "json":
                _print_cli_diagnostic(error, "json")
                return 2
            parser.error(str(error))
        except SimulationStateError as error:
            parser.error(str(error))
    if arguments.formal_depth < 1:
        parser.error("--formal-depth must be positive")
    formal_output_design = result.formal_design
    formal_harness_text = result.formal_harness
    if arguments.formal_harness is not None:
        try:
            validate_legacy_formal_view(result, formal_output_design)
        except FormalOrchestrationError as error:
            parser.error(str(error))
    if arguments.formal_sby is not None:
        if arguments.formal_harness is None:
            parser.error("--formal-sby requires --formal-harness")
        if systemverilog_output is None:
            parser.error(
                "--formal-sby requires --systemverilog for a connected backend artifact"
            )
    if arguments.formal_harness is not None and systemverilog_output is not None:
        try:
            formal_artifact = emit_systemverilog_formal_artifact(
                result.ir, result.recursive_formal_design
            )
            formal_output_design = connect_formal_design(
                result.formal_design, formal_artifact
            )
            validate_legacy_formal_view(result, formal_output_design)
            formal_harness_text = emit_harness(
                formal_output_design, depth=arguments.formal_depth
            )
        except FormalOrchestrationError as error:
            parser.error(str(error))
        except (FormalError, SystemVerilogEmissionError) as error:
            if arguments.formal_sby is not None:
                parser.error(str(error))
            formal_output_design = replace(
                result.formal_design,
                non_executable_reason=str(error),
            )
            formal_harness_text = emit_harness(
                formal_output_design, depth=arguments.formal_depth
            )
    verification_report = None
    verification_exit_code = 0
    verification_temporary = None
    compiler_formal_plan = None
    candidate_plan_temporary = None
    prepared_candidate_equivalence: tuple[
        PreparedCandidateEquivalenceSite, ...
    ] = ()
    candidate_equivalence_reports: tuple[
        CandidateEquivalenceExecutionReport, ...
    ] = ()
    if arguments.verify or arguments.verification_bundle is not None:
        try:
            bundle_directory = arguments.verification_bundle
            if bundle_directory is None:
                verification_temporary = tempfile.TemporaryDirectory(
                    prefix="zlang-verification-"
                )
                bundle_directory = Path(verification_temporary.name)
            normalized_formal_policy = FormalPolicy(
                getattr(result.implementation_request, "formal_policy")
            )
            candidate_config = FormalExplorationConfig(
                policy=normalized_formal_policy,
                max_formal_candidates=arguments.formal_max_candidates,
                bmc_depth=arguments.formal_depth,
                timeout_seconds=arguments.formal_timeout,
                cache_directory=arguments.formal_cache,
                artifact_provider=result.formal_artifact_provider,
                tool_resolver=result.formal_tool_resolver,
            )
            # Candidate routes require the exact module-scoped verification
            # plan, but their backend preparation must happen only for the
            # requested formal-policy bundle. Build that base plan in a private
            # immutable bundle, prepare (without solver execution), then
            # publish the requested bundle once with the enriched linked plan.
            if arguments.verify and normalized_formal_policy is not FormalPolicy.OFF:
                candidate_plan_temporary = tempfile.TemporaryDirectory(
                    prefix="zlang-candidate-plan-"
                )
                preliminary_directory = Path(candidate_plan_temporary.name)
                publish_compilation_verification_bundle(
                    result,
                    preliminary_directory,
                )
                preliminary = load_verification_bundle(preliminary_directory)
                preliminary_payload = preliminary.verification_ir.get("payload")
                if not isinstance(preliminary_payload, dict):
                    raise VerificationBundleError(
                        "verification bundle has no compiler formal plan payload"
                    )
                preliminary_plan = CompilerFormalExecutionPlan.from_data(
                    preliminary_payload.get("compiler_execution_plan")
                )
                compiler_formal_plan, prepared_candidate_equivalence = (
                    prepare_selected_candidate_equivalence(
                        result,
                        preliminary_plan,
                        candidate_config,
                    )
                )
                publication_keywords: dict[str, object] = {
                    "compiler_execution_plan": compiler_formal_plan,
                }
                # Production preparation always returns the typed product.
                # Keeping this defensive boundary also lets focused CLI tests
                # replace the execution-only adapter without fabricating a
                # serializable backend artifact.
                if all(
                    isinstance(item, PreparedCandidateEquivalenceSite)
                    for item in prepared_candidate_equivalence
                ):
                    publication_keywords["prepared_candidate_equivalence"] = (
                        prepared_candidate_equivalence
                    )
                publish_compilation_verification_bundle(
                    result,
                    bundle_directory,
                    **publication_keywords,
                )
            else:
                publish_compilation_verification_bundle(
                    result,
                    bundle_directory,
                )
            loaded_bundle = load_verification_bundle(bundle_directory)
            verification_payload = loaded_bundle.verification_ir.get("payload")
            if not isinstance(verification_payload, dict):
                raise VerificationBundleError(
                    "verification bundle has no compiler formal plan payload"
                )
            compiler_formal_plan = CompilerFormalExecutionPlan.from_data(
                verification_payload.get("compiler_execution_plan")
            )
            if arguments.verify:
                assert verification_work_directory is not None
                verification_run_keywords: dict[str, object] = {
                    "config": VerificationRunConfig(
                        mode=(
                            ProofMode.PROVE
                            if (arguments.verify_require or "checked") == "proven"
                            else ProofMode.BMC
                        ),
                        depth=arguments.formal_depth,
                        timeout_seconds=arguments.formal_timeout,
                        jobs=arguments.formal_jobs,
                    ),
                    "work_directory": verification_work_directory,
                    "tool_resolver": result.formal_tool_resolver,
                }
                if arguments.formal_cache is not None:
                    verification_run_keywords["cache_directory"] = (
                        arguments.formal_cache
                    )
                verification_report = run_verification_bundle_staged(
                    bundle_directory,
                    **verification_run_keywords,
                )
                formal_policy = compiler_formal_plan.formal_policy
                if formal_policy is not FormalPolicy.OFF:
                    candidate_config = replace(
                        candidate_config,
                        work_directory=verification_work_directory,
                    )
                    candidate_equivalence_reports = (
                        execute_prepared_candidate_equivalence(
                            result,
                            compiler_formal_plan,
                            prepared_candidate_equivalence,
                            candidate_config,
                            jobs=arguments.formal_jobs,
                        )
                    )
        except (
            FormalError,
            FormalOrchestrationError,
            SystemVerilogEmissionError,
            VerificationBundleError,
            OSError,
        ) as error:
            print(f"{CLI_NAME}: verification unavailable: {error}", file=sys.stderr)
            if verification_temporary is not None:
                verification_temporary.cleanup()
            if candidate_plan_temporary is not None:
                candidate_plan_temporary.cleanup()
            return 2
    if arguments.implementation_manifest is not None and direct_artifact is None:
        parser.error("--implementation-manifest requires --systemverilog")
    has_sink = has_explicit_artifact_sink(arguments) or arguments.verify
    if rom_companions and not has_sink:
        parser.error(
            "initialized ROM emission requires an explicit -o, --systemverilog, "
            "or --verilog-dir output so companion images can be published"
        )
    clash_public_wrapper = None
    if arguments.verilog_dir is not None:
        try:
            clash_public_wrapper = ClashPublicTopWrapper.build(result.ir)
        except ClashPublicWrapperError as error:
            parser.error(str(error))
    clash_artifact = None
    if arguments.output is not None or arguments.verilog_dir is not None or (
        arguments.source_map is not None and systemverilog_output is None
    ):
        clash_artifact = emit_clash_artifact(
            result.ir,
            selected_ir_identity=result.selected_ir_identity,
        )
        if clash_public_wrapper is not None:
            try:
                clash_artifact = bind_artifact_to_public_wrapper(
                    clash_artifact,
                    clash_public_wrapper,
                )
            except ClashPublicWrapperError as error:
                parser.error(str(error))
    constraint_products: list[tuple[Path, ConstraintArtifact]] = []
    if constraint_requested:
        manifest_path = result.physical_inputs.project_manifest
        if manifest_path is None:
            parser.error("constraint publication requires a zlang.toml project")
        try:
            platform_profile = selected_platform_profile
            if platform_profile is None:
                raise PlatformConstraintError(
                    f"profile '{arguments.profile}' has no platform clock declaration"
                )
            constraint = platform_profile.clocks[0]
            bound_artifact = direct_artifact or clash_artifact
            if bound_artifact is None:
                raise PlatformConstraintError(
                    "constraint publication requires a generated backend artifact"
                )
            for path, format in (
                (arguments.constraints_xdc, ConstraintFormat.XDC),
                (arguments.constraints_sdc, ConstraintFormat.SDC),
            ):
                if path is not None:
                    constraint_products.append((
                        path,
                        build_constraint_artifact(
                            result.ir, bound_artifact, constraint, format
                        ),
                    ))
        except (PlatformConstraintError, OSError) as error:
            parser.error(str(error))
    try:
        _preflight_companion_paths(
            arguments,
            systemverilog_output=systemverilog_output,
            clash_companions=(clash_artifact.companions if clash_artifact else ()),
            direct_companions=(direct_artifact.companions if direct_artifact else ()),
            compilation_inputs=result.physical_inputs.all_paths,
        )
    except ValueError as error:
        parser.error(str(error))
    if not has_sink:
        print(result.clash, end="")
    elif arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        try:
            clash_companion_paths = publish_companion_bundle(
                clash_artifact.companions if clash_artifact else (),
                arguments.output.parent,
            )
        except CompanionArtifactError as error:
            parser.error(str(error))
        arguments.output.write_text(result.clash)
    if systemverilog_output is not None:
        systemverilog_output.parent.mkdir(
            parents=True, exist_ok=True
        )
        try:
            direct_companion_paths = publish_companion_bundle(
                direct_artifact.companions if direct_artifact else (),
                systemverilog_output.parent,
            )
        except CompanionArtifactError as error:
            parser.error(str(error))
        systemverilog_output.write_text(direct_systemverilog)
    if arguments.simulation_state_bundle is not None:
        assert simulation_state_bundle is not None
        assert systemverilog_output is not None
        try:
            simulation_state_bundle.validate_rtl_file(systemverilog_output)
            simulation_state_bundle.publish(arguments.simulation_state_bundle)
        except (OSError, SimulationStateError) as error:
            parser.error(str(error))
    for path, constraint_artifact in constraint_products:
        try:
            publish_constraint_artifact(constraint_artifact, path)
        except PlatformConstraintError as error:
            parser.error(str(error))
    if arguments.implementation_manifest is not None:
        arguments.implementation_manifest.parent.mkdir(parents=True, exist_ok=True)
        arguments.implementation_manifest.write_text(direct_artifact.to_json())
    if arguments.source_map is not None:
        if systemverilog_output is not None:
            # Direct-SV publication always constructed this exact artifact
            # before reaching the source-map path.  Falling back to emission
            # here would make --source-map render the design twice.
            mapped_artifact = direct_artifact
        else:
            mapped_artifact = clash_artifact or emit_clash_artifact(
                result.ir,
                selected_ir_identity=result.selected_ir_identity,
            )
        assert mapped_artifact is not None
        source_map = build_generated_source_map(result.ir, mapped_artifact)
        arguments.source_map.parent.mkdir(parents=True, exist_ok=True)
        arguments.source_map.write_text(source_map.to_json())
    if arguments.contracts_sva is not None:
        arguments.contracts_sva.parent.mkdir(parents=True, exist_ok=True)
        arguments.contracts_sva.write_text(result.contracts_sva)
    if arguments.implementation_report is not None:
        arguments.implementation_report.parent.mkdir(parents=True, exist_ok=True)
        arguments.implementation_report.write_text(result.implementation_report)
    if arguments.cost_report is not None:
        arguments.cost_report.parent.mkdir(parents=True, exist_ok=True)
        arguments.cost_report.write_text(result.cost_report)
    if arguments.pipeline_report is not None:
        arguments.pipeline_report.parent.mkdir(parents=True, exist_ok=True)
        arguments.pipeline_report.write_text(result.pipeline_report)
    if arguments.architecture_report is not None:
        arguments.architecture_report.parent.mkdir(parents=True, exist_ok=True)
        arguments.architecture_report.write_text(result.architecture_report)
    if arguments.exploration_report is not None:
        arguments.exploration_report.parent.mkdir(parents=True, exist_ok=True)
        arguments.exploration_report.write_text(result.exploration_report)
    if arguments.implementation_policy_report is not None:
        arguments.implementation_policy_report.parent.mkdir(
            parents=True, exist_ok=True
        )
        arguments.implementation_policy_report.write_text(
            result.implementation_policy_report
        )
    if arguments.backend_implementation_report is not None:
        arguments.backend_implementation_report.parent.mkdir(
            parents=True, exist_ok=True
        )
        arguments.backend_implementation_report.write_text(
            result.backend_implementation_report
        )
    if arguments.formal_harness is not None:
        arguments.formal_harness.parent.mkdir(parents=True, exist_ok=True)
        arguments.formal_harness.write_text(formal_harness_text)
    if arguments.formal_sby is not None:
        arguments.formal_sby.parent.mkdir(parents=True, exist_ok=True)
        arguments.formal_sby.write_text(emit_sby(
            formal_output_design,
            depth=arguments.formal_depth,
            source_file=os.path.relpath(
                arguments.formal_harness,
                arguments.formal_sby.parent,
            ),
        ))
    if verification_report is not None:
        public_verification_report = (
            CompilerVerificationReport(
                verification_report,
                compiler_formal_plan,
                candidate_equivalence_reports,
            )
            if candidate_equivalence_reports
            else verification_report
        )
        rendered_verification = (
            public_verification_report.to_json()
            if (arguments.verification_format or "text") == "json"
            else public_verification_report.to_text()
        )
        if arguments.verification_report is not None:
            if arguments.verification_bundle is not None:
                try:
                    arguments.verification_report.resolve(strict=False).relative_to(
                        arguments.verification_bundle.resolve(strict=False)
                    )
                except ValueError:
                    pass
                else:
                    parser.error(
                        "--verification-report must be outside the immutable bundle"
                    )
            arguments.verification_report.parent.mkdir(parents=True, exist_ok=True)
            arguments.verification_report.write_text(rendered_verification)
        print(rendered_verification, end="")
        verification_exit_code = public_verification_report.exit_code
        if verification_temporary is not None:
            verification_temporary.cleanup()
        if candidate_plan_temporary is not None:
            candidate_plan_temporary.cleanup()
        if (
            verification_exit_code != 0
            and arguments.evidence_report is None
            and arguments.build_manifest is None
        ):
            return verification_exit_code
    evidence_records = ()
    rendered_evidence = None
    if arguments.evidence_report is not None or arguments.build_manifest is not None:
        try:
            records = [
                evidence_for_typed_module(
                    result.ir,
                    high_level_ir_identity=result.high_level_ir_identity,
                )
            ]
            if result.ir.timing_contract is not None:
                records.append(evidence_for_validated_timing(
                    result.ir,
                    selected_ir_identity=result.selected_ir_identity,
                ))
            records.extend(collect_m39_evidence(result))
            if arguments.formal_harness is not None:
                records.extend(
                    evidence_for_unexecuted_property(property_)
                    for property_ in formal_output_design.properties
                )
            if verification_report is not None:
                records.extend(evidence_from_verification_report(verification_report))
            for report in candidate_equivalence_reports:
                records.extend(report.evidence_records)
            evidence_records = tuple(records)
            if arguments.evidence_report is not None:
                rendered_evidence = build_evidence_report(
                    "build.evidence",
                    evidence_records,
                    format=arguments.evidence_format,
                    formal_execution_plan=compiler_formal_plan,
                    candidate_equivalence=candidate_equivalence_reports,
                    logical_path=(
                        "reports/evidence.json"
                        if arguments.evidence_format == "json"
                        else "reports/evidence.txt"
                    ),
                )
                arguments.evidence_report.parent.mkdir(parents=True, exist_ok=True)
                arguments.evidence_report.write_text(rendered_evidence.content)
        except (EvidenceReportError, FormalOrchestrationError) as error:
            parser.error(str(error))
    if arguments.synthesis_report is not None:
        arguments.synthesis_report.parent.mkdir(parents=True, exist_ok=True)
        arguments.synthesis_report.write_text(synthesis_report)
    if arguments.optimization_ir is not None:
        arguments.optimization_ir.parent.mkdir(parents=True, exist_ok=True)
        arguments.optimization_ir.write_text(
            render_optimization_ir(result.optimization_ir)
        )
    if arguments.high_level_ir is not None:
        arguments.high_level_ir.parent.mkdir(parents=True, exist_ok=True)
        arguments.high_level_ir.write_text(
            render_optimization_ir(result.high_level_ir)
        )
    if arguments.saturation_report is not None:
        arguments.saturation_report.parent.mkdir(parents=True, exist_ok=True)
        arguments.saturation_report.write_text(saturation_report)
    if arguments.verilog_dir is not None:
        try:
            generated_verilog_files = generate_verilog(
                result.clash,
                result.ir.name,
                arguments.verilog_dir,
                arguments.clash,
                source_map=(
                    source_map
                    if source_map is not None and source_map.backend == "clash"
                    else None
                ),
                companions=(clash_artifact.companions if clash_artifact else ()),
                public_wrapper=clash_public_wrapper,
            )
            if arguments.build_manifest is not None and arguments.output is None:
                assert clash_artifact is not None
                if clash_artifact.text != result.clash:
                    raise ToolchainError(
                        "published Clash artifact differs from the source compiled to RTL"
                    )
                clash_manifest_source_path = (
                    arguments.verilog_dir
                    / ".zlang"
                    / "generated"
                    / f"{result.ir.name}.hs"
                )
                clash_manifest_source_path.parent.mkdir(parents=True, exist_ok=True)
                clash_manifest_source_path.write_text(
                    clash_artifact.text,
                    encoding="utf-8",
                )
            clash_rtl_companion_paths = tuple(sorted(
                {
                    parent / companion.logical_path
                    for parent in {
                        arguments.verilog_dir.resolve(),
                        *(path.resolve().parent for path in generated_verilog_files),
                    }
                    for companion in (
                        clash_artifact.companions if clash_artifact else ()
                    )
                },
                key=lambda path: path.as_posix(),
            ))
            clash_executable = arguments.clash or find_clash_executable()
            assert clash_executable is not None
            rtl_outputs = tuple(
                "backends/clash/rtl/"
                + path.resolve().relative_to(arguments.verilog_dir.resolve()).as_posix()
                for path in generated_verilog_files
            )
            tool_executions.append(_tool_execution(
                role="rtl_generation",
                tool="clash",
                version=_query_tool_version(clash_executable, "--version"),
                argv_shape=(
                    "clash", "--verilog", "<generated-source>",
                    "-outputdir", "<rtl-output>",
                    "-fclash-component-prefix", "<typed-core-prefix>",
                ),
                outputs=rtl_outputs,
            ))
            if arguments.verilator_lint:
                verilator_executable = arguments.verilator or shutil.which("verilator")
                assert verilator_executable is not None
                lint_with_verilator(
                    (
                        *generated_verilog_files,
                        *((arguments.contracts_sva,) if arguments.contracts_sva else ()),
                    ),
                    result.ir.name,
                    arguments.verilator,
                    source_map=(
                        source_map
                        if source_map is not None and source_map.backend == "clash"
                        else None
                    ),
                )
                tool_executions.append(_tool_execution(
                    role="rtl_lint",
                    tool="verilator",
                    version=_query_tool_version(verilator_executable, "--version"),
                    argv_shape=(
                        "verilator", "--lint-only", "--top-module", "<top>",
                        "<rtl-inputs>",
                    ),
                ))
        except (ToolchainError, ClashPublicWrapperError) as error:
            parser.error(str(error))
    for path, contents in (
        (arguments.csr_markdown, result.csr_markdown),
        (arguments.csr_json, result.csr_json),
    ):
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
    if arguments.build_manifest is not None:
        try:
            root_publication = source_publication(
                arguments.source,
                compiled_bytes=source_bytes,
            )
            all_publications: list[PhysicalPublication] = [root_publication]
            backend_builds = []
            reports: list[ReportRecord] = []
            constraint_publications: dict[str, list[PhysicalPublication]] = {}
            for path, constraint_artifact in constraint_products:
                publication = published_file_from_path(
                    "backends/"
                    f"{constraint_artifact.backend}/constraints/"
                    f"{result.ir.name}.{constraint_artifact.format.value}",
                    path,
                    kind=f"platform_constraint:{constraint_artifact.format.value}",
                )
                constraint_publications.setdefault(
                    constraint_artifact.backend, []
                ).append(publication)

            def add_report(
                attribute: str,
                kind: str,
                *,
                format: str | None = None,
                evidence_ids: tuple[str, ...] = (),
            ) -> None:
                path = getattr(arguments, attribute)
                if path is None:
                    return
                suffix = path.suffix.lower() or ".txt"
                logical_path = f"reports/{attribute.replace('_', '-')}{suffix}"
                record, publication = report_from_path(
                    f"build.{attribute.replace('_', '.')}",
                    kind,
                    format or _report_format(path),
                    logical_path,
                    path,
                    evidence_ids=evidence_ids,
                )
                reports.append(record)
                all_publications.append(publication)

            source_map_hash = None
            source_map_publication = None
            if arguments.source_map is not None:
                source_map_hash = hashlib.sha256(
                    arguments.source_map.read_bytes()
                ).hexdigest()
                source_map_publication = published_file_from_path(
                    "backends/"
                    + (
                        "clash" if source_map is not None and source_map.backend == "clash"
                        else "direct_systemverilog"
                    )
                    + "/source-map.json",
                    arguments.source_map,
                    kind="generated_source_map",
                )

            if clash_artifact is not None and (
                arguments.output is not None or arguments.verilog_dir is not None
            ):
                clash_files: list[PhysicalPublication] = []
                if arguments.output is not None:
                    clash_files.append(published_file_from_path(
                        backend_logical_path("clash", result.ir.name, "hs"),
                        arguments.output,
                        kind="clash_source",
                    ))
                elif clash_manifest_source_path is not None:
                    clash_files.append(published_file_from_path(
                        backend_logical_path("clash", result.ir.name, "hs"),
                        clash_manifest_source_path,
                        kind="clash_source",
                    ))
                for path in generated_verilog_files:
                    relative = path.resolve().relative_to(
                        arguments.verilog_dir.resolve()
                    ).as_posix()
                    clash_files.append(published_file_from_path(
                        f"backends/clash/rtl/{relative}",
                        path,
                        kind="generated_verilog",
                    ))
                if (
                    source_map_publication is not None
                    and source_map is not None
                    and source_map.backend == "clash"
                ):
                    clash_files.append(source_map_publication)
                clash_companions: list[PhysicalPublication] = []
                clash_companions.extend(
                    published_file_from_path(
                        f"backends/clash/companions/source/{path.name}",
                        path,
                        kind="rom_image",
                    )
                    for path in clash_companion_paths
                )
                if arguments.verilog_dir is not None:
                    clash_companions.extend(
                        published_file_from_path(
                            "backends/clash/companions/rtl/"
                            + path.resolve().relative_to(
                                arguments.verilog_dir.resolve()
                            ).as_posix(),
                            path,
                            kind="rom_image",
                        )
                        for path in clash_rtl_companion_paths
                    )
                clash_companions.extend(constraint_publications.get("clash", ()))
                clash_plan = result.backend_implementation_plans.plan_for("clash")
                backend_builds.append(backend_build_record(
                    artifact=clash_artifact,
                    plan=clash_plan,
                    publications=clash_files,
                    companions=clash_companions,
                    selected_ir_identity=result.selected_ir_identity,
                    implementation_graph_identity=(
                        clash_plan.graph.identity if clash_plan.graph is not None else None
                    ),
                    source_map_hash=(
                        source_map_hash
                        if source_map is not None and source_map.backend == "clash"
                        else None
                    ),
                ))
                all_publications.extend((*clash_files, *clash_companions))

            if direct_artifact is not None and systemverilog_output is not None:
                direct_files = [published_file_from_path(
                    backend_logical_path(
                        "direct_systemverilog", result.ir.name, "sv"
                    ),
                    systemverilog_output,
                    kind="direct_systemverilog",
                )]
                if arguments.implementation_manifest is not None:
                    direct_files.append(published_file_from_path(
                        "backends/direct_systemverilog/backend-artifact.json",
                        arguments.implementation_manifest,
                        kind="backend_artifact_manifest",
                    ))
                if (
                    source_map_publication is not None
                    and source_map is not None
                    and source_map.backend == "direct_systemverilog"
                ):
                    direct_files.append(source_map_publication)
                direct_companions = tuple(
                    published_file_from_path(
                        f"backends/direct_systemverilog/companions/{path.name}",
                        path,
                        kind="rom_image",
                    )
                    for path in direct_companion_paths
                )
                direct_companions = (
                    *direct_companions,
                    *constraint_publications.get("direct_systemverilog", ()),
                )
                direct_plan = result.backend_implementation_plans.plan_for(
                    "systemverilog"
                )
                backend_builds.append(backend_build_record(
                    artifact=direct_artifact,
                    plan=direct_plan,
                    publications=direct_files,
                    companions=direct_companions,
                    selected_ir_identity=result.selected_ir_identity,
                    implementation_graph_identity=(
                        result.implementation_graph.identity
                        if result.implementation_graph is not None else None
                    ),
                    source_map_hash=(
                        source_map_hash
                        if source_map is not None
                        and source_map.backend == "direct_systemverilog"
                        else None
                    ),
                ))
                all_publications.extend((*direct_files, *direct_companions))

            not_run_evidence_ids = tuple(
                item.evidence_id for item in evidence_records
                if item.status == "not_run" and item.claim == "m35.safety_property"
            )
            report_specs = (
                ("contracts_sva", "contracts", None, ()),
                ("implementation_report", "implementation", None, ()),
                ("cost_report", "cost", None, ()),
                ("pipeline_report", "pipeline", None, ()),
                ("architecture_report", "architecture", None, ()),
                ("exploration_report", "exploration", None, ()),
                ("implementation_policy_report", "implementation_policy", None, ()),
                ("backend_implementation_report", "backend_planning", None, ()),
                ("formal_harness", "formal_harness", None, not_run_evidence_ids),
                ("formal_sby", "formal_configuration", None, not_run_evidence_ids),
                ("synthesis_report", "synthesis", None, ()),
                ("optimization_ir", "selected_ir", None, ()),
                ("high_level_ir", "high_level_ir", None, ()),
                ("saturation_report", "saturation", None, ()),
                ("csr_markdown", "csr", None, ()),
                ("csr_json", "csr", None, ()),
            )
            for attribute, kind, format, linked_evidence in report_specs:
                add_report(
                    attribute,
                    kind,
                    format=format,
                    evidence_ids=linked_evidence,
                )
            if arguments.evidence_report is not None:
                assert rendered_evidence is not None
                evidence_publication = published_file_from_path(
                    rendered_evidence.record.logical_path,
                    arguments.evidence_report,
                    kind="report:evidence",
                )
                if (
                    evidence_publication.record.content_hash
                    != rendered_evidence.record.content_hash
                ):
                    raise BuildManifestError(
                        "published evidence report differs from its report record"
                    )
                reports.append(rendered_evidence.record)
                all_publications.append(evidence_publication)

            if synthesis_feedback is not None:
                synthesis_outputs = tuple(
                    report.logical_path
                    for report in reports
                    if report.kind == "synthesis" and report.logical_path is not None
                )
                tool_executions.extend((
                    _tool_execution(
                        role="synthesis_frontend",
                        tool="clash",
                        version=synthesis_feedback.clash_version,
                        argv_shape=("clash", "--verilog", "<candidate>"),
                        outputs=synthesis_outputs,
                    ),
                    _tool_execution(
                        role="synthesis_measurement",
                        tool="yosys",
                        version=synthesis_feedback.yosys_version,
                        argv_shape=("yosys", "<normalized-script>"),
                        outputs=synthesis_outputs,
                    ),
                ))

            request = result.implementation_request
            policy = result.implementation_policy
            assert request is not None and policy is not None
            platform_profile_identity = (
                selected_platform_profile.identity
                if selected_platform_profile is not None else None
            )
            profile_identity = (
                None
                if arguments.profile is None
                else stable_digest(
                    {
                        "schema": (
                            "zlang-selected-profile-v2"
                            if platform_profile_identity is not None
                            else "zlang-selected-profile-v1"
                        ),
                        "name": arguments.profile,
                        "request_identity": request.identity,
                        **(
                            {"platform_constraint_identity": platform_profile_identity}
                            if platform_profile_identity is not None else {}
                        ),
                    }
                )
            )
            manifest_metadata = [("module", result.ir.name)]
            for _, constraint_artifact in constraint_products:
                manifest_metadata.extend((
                    (
                        f"platform_constraint.{constraint_artifact.backend}."
                        f"{constraint_artifact.format.value}.identity",
                        constraint_artifact.identity,
                    ),
                    (
                        f"platform_constraint.{constraint_artifact.backend}."
                        f"{constraint_artifact.format.value}.reset",
                        ":".join((
                            constraint_artifact.reset_mode,
                            constraint_artifact.reset_polarity,
                            constraint_artifact.power_up,
                        )),
                    ),
                ))
            if result.ir.dependency_closure is not None:
                manifest_metadata.extend((
                    (
                        "dependency_closure_identity",
                        result.ir.dependency_closure.identity,
                    ),
                    (
                        "dependency_lock_identity",
                        result.ir.dependency_closure.lock_identity,
                    ),
                ))
            manifest = WholeBuildManifest(
                root_source=root_publication.record,
                dependency_closure=dependency_records(
                    result.ir.dependency_closure,
                    library_dependencies=result.ir.library_dependencies,
                ),
                high_level_ir=CanonicalIrRef(
                    "high_level",
                    result.high_level_ir_identity,
                    CANONICAL_IR_IDENTITY_SCHEMA,
                    result.high_level_ir_identity.split(":", 1)[1],
                ),
                selected_ir=CanonicalIrRef(
                    "selected",
                    result.selected_ir_identity,
                    CANONICAL_IR_IDENTITY_SCHEMA,
                    result.selected_ir_identity.split(":", 1)[1],
                ),
                implementation_request_identity=request.identity,
                implementation_policy_identity=policy.identity,
                profile_identity=profile_identity,
                backend_builds=tuple(backend_builds),
                tool_executions=tuple(tool_executions),
                reports=tuple(reports),
                evidence=evidence_records,
                metadata=tuple(manifest_metadata),
            )
            publish_manifest_atomically(
                manifest,
                arguments.build_manifest,
                publications=all_publications,
            )
        except BuildManifestError as error:
            parser.error(str(error))
    if arguments.verbose:
        written_paths: list[Path] = []
        for attribute in _ARTIFACT_SINK_ATTRIBUTES:
            path = getattr(arguments, attribute, None)
            if path is not None and path not in written_paths:
                written_paths.append(path)
        detail = (
            "; wrote " + ", ".join(str(path) for path in written_paths)
            if written_paths
            else "; Clash emitted to stdout"
        )
        print(
            f"{CLI_NAME}: ok: {arguments.source} (top {result.ir.name}){detail}",
            file=sys.stderr,
        )
    return verification_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
