"""Compilation-command orchestration behind the small public CLI facade."""
from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from zlang import compiler as compiler_api
from zlang import source_identity as source_api
from zlang.backend import systemverilog as sv_backend
from zlang.simulation_state import SimulationStateError
from zlang.backend.external import load_profile_external_mappings
from zlang.implementations import render_implementation_report
from zlang.opt.ir import OptimizationStage
from zlang.opt.module_lowering import lower
from zlang.opt.module_restoration import restore
from zlang.opt.rewrite_model import render_saturation
from zlang.opt.saturation import SaturationError, saturate
from zlang import synthesis as synthesis_api
from zlang.diagnostics import DiagnosticError
from zlang.workspace import WorkspaceError
from zlang import project as project_api
from zlang import platform_constraints as constraint_api
from zlang import cli_arguments
from zlang import cli_publication
from zlang import cli_verification
from zlang import diagnostics as diagnostic_api
from zlang import workspace as workspace_api
from zlang.compilation_products import CompilationResult, SemanticCheckResult
from zlang.compilation_session import CompilationSession, SessionTopSelectionError
from zlang.opt.identity import canonical_ir_identity
from zlang.parser import ParseError, parse
from zlang.semantic import SemanticError


def fallback_diagnostic(error: BaseException) -> diagnostic_api.Diagnostic:
    """Attach stable codes to public exceptions not migrated at their source."""

    if isinstance(error, source_api.SourceExtensionError):
        return diagnostic_api.Diagnostic(
            "ZL-SOURCE-EXTENSION",
            str(error),
            fixes=(f"rename the source file to use '{source_api.SOURCE_SUFFIX}'",),
        )
    if isinstance(error, compiler_api.TopSelectionError):
        code = "ZL-TOP-001"
    elif isinstance(error, workspace_api.WorkspaceError):
        code = "ZL-PROJECT-001"
    elif isinstance(error, OSError):
        code = "ZL-IO-001"
    else:
        code = "ZL-ERROR-001"
    return diagnostic_api.diagnostic_from_exception(error, code=code)


def print_cli_diagnostic(
    error: BaseException,
    diagnostic_format: str,
    *,
    legacy_message: str | None = None,
    source: Path | None = None,
    project: Path | None = None,
) -> None:
    """Write one diagnostic with a compiler-owned code and source location."""

    diagnostic = fallback_diagnostic(error)
    if legacy_message is not None:
        diagnostic = diagnostic_api.Diagnostic(
            diagnostic.code,
            legacy_message,
            diagnostic.primary,
            diagnostic.notes,
            diagnostic.fixes,
        )
    if diagnostic_format == "json":
        print(diagnostic.to_json(), file=sys.stderr)
        return

    location = diagnostic_location(diagnostic, source=source, project=project)
    prefix = f"{source_api.CLI_NAME}: "
    if location is not None:
        prefix += f"{location}: "
    print(
        f"{prefix}error[{diagnostic.code}]: {diagnostic.message}",
        file=sys.stderr,
    )


def diagnostic_location(
    diagnostic: diagnostic_api.Diagnostic,
    *,
    source: Path | None,
    project: Path | None,
) -> str | None:
    """Resolve a logical compiler origin to a physical CLI source location."""

    origin = diagnostic.primary
    if origin is None:
        return None
    source_path = source
    source_unit = origin.source_unit
    if source_path is not None and source_unit not in {None, source_path.name}:
        resolved_source: Path | None = None
        try:
            workspace = workspace_api.load_project_workspace(source_path, project=project)
        except (OSError, ValueError):
            workspace = None
        if workspace is not None:
            resolved_source = workspace.source_path_for_unit(source_unit)
        source_path = resolved_source
    rendered_source = (
        str(source_path)
        if source_path is not None
        else (source_unit or "<unknown-source>")
    )
    return (
        f"{rendered_source}:{origin.span.start_line}:"
        f"{origin.span.start_column}"
    )
@dataclass(frozen=True)
class CompiledSource:
    result: CompilationResult | SemanticCheckResult | _CliCompilation
    source_bytes: bytes
    source_digest: str
    checked_module_names: tuple[str, ...]


@dataclass(frozen=True)
class _CliCompilation:
    """Demand-driven compiler products for one CLI artifact request.

    The public Python compilation facade remains eager.  This private view
    projects the same session-owned values while allowing an artifact command
    to avoid unrelated terminal products.
    """

    session: CompilationSession
    measured_ir: object | None = None
    measured_optimization_ir: object | None = None
    measured_implementation_report: str | None = None

    @property
    def ast(self):
        return self.session.syntax

    @property
    def ir(self):
        return self.session.planning.module if self.measured_ir is None else self.measured_ir

    @property
    def optimization_ir(self):
        return (
            self.session.optimization_ir
            if self.measured_optimization_ir is None
            else self.measured_optimization_ir
        )

    @property
    def high_level_ir(self):
        return self.session.high_level_ir

    @property
    def high_level_ir_identity(self) -> str:
        return canonical_ir_identity(self.high_level_ir)

    @property
    def selected_ir_identity(self) -> str:
        return canonical_ir_identity(self.optimization_ir)

    @property
    def physical_inputs(self):
        return self.session.physical_inputs

    @property
    def exploration_results(self):
        return self.session.selection.exploration_results

    @property
    def implementation_policy(self):
        return self.session.implementation_policy

    @property
    def implementation_request(self):
        return self.session.implementation_request

    @property
    def implementation_regions(self):
        return tuple(item.region for item in self.session.implementation_policy.regions)

    @property
    def implementation_policy_report(self) -> str:
        return self.session.implementation_policy.report

    @property
    def candidate_site_ledger(self):
        return self.session.candidate_site_ledger

    @property
    def backend_implementation_plans(self):
        return self.session.backend_implementation_plans

    @property
    def backend_implementation_report(self) -> str:
        return self.session.backend_implementation_plans.report

    @property
    def implementation_graph(self):
        return self.session.implementation_graph

    @property
    def target_planning_result(self):
        return self.session.target_planning_result

    @property
    def target_planner_report(self) -> str:
        planning = self.session.target_planning_result
        return "" if planning is None else planning.report

    @property
    def physical_formal_records(self):
        return self.session.physical_formal_records

    @property
    def formal_design(self):
        return self.session.formal_products.design

    @property
    def recursive_formal_design(self):
        return self.session.formal_products.recursive_design

    @property
    def formal_artifact_provider(self):
        return self.session.formal_artifact_provider

    @property
    def formal_tool_resolver(self):
        return self.session.formal_tool_resolver

    @property
    def target_instance(self):
        return self.session.target_instance

    @property
    def csr_markdown(self) -> str:
        return self.session.documents.csr_markdown

    @property
    def csr_json(self) -> str:
        return self.session.documents.csr_json

    @property
    def contracts_sva(self) -> str:
        return self.session.documents.contracts_sva

    @property
    def implementation_report(self) -> str:
        if self.measured_implementation_report is not None:
            return self.measured_implementation_report
        return self.session.reports.implementation

    @property
    def cost_report(self) -> str:
        return self.session.reports.cost

    @property
    def pipeline_report(self) -> str:
        return self.session.reports.pipeline

    @property
    def architecture_report(self) -> str:
        return self.session.reports.architecture

    @property
    def exploration_report(self) -> str:
        return self.session.reports.exploration

    def with_measured_ir(
        self,
        *,
        ir: object,
        optimization_ir: object,
        implementation_report: str,
    ) -> _CliCompilation:
        return _CliCompilation(
            self.session,
            ir,
            optimization_ir,
            implementation_report,
        )


def _compile_artifact_snapshot(
    source: Path,
    source_text: str,
    **compile_options,
) -> _CliCompilation:
    """Create the private demand-driven view used by artifact commands."""

    session = compiler_api.create_file_compilation_session_snapshot(
        source,
        source_text,
        **compile_options,
    )
    return _CliCompilation(session)


def compile_source_request(
    arguments: argparse.Namespace,
    verification_work_directory: Path | None,
) -> CompiledSource:
    """Read and compile the one exact root-source snapshot for this invocation."""

    source_api.validate_source_path(arguments.source)
    source_bytes = arguments.source.read_bytes()
    source_text = source_bytes.decode("utf-8")
    source_digest = hashlib.sha256(source_bytes).hexdigest()
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
            compile_options = {
                "source_digest": source_digest,
                "project": arguments.project,
                "profile": arguments.profile,
                "formal_policy": arguments.formal_policy,
                "formal_depth": arguments.formal_depth,
                "formal_max_candidates": arguments.formal_max_candidates,
                "formal_timeout": arguments.formal_timeout,
                "formal_cache": arguments.formal_cache,
                "formal_work_directory": verification_work_directory,
                "top": compile_top,
                "target": arguments.target,
                "architecture": arguments.target_architecture,
                "architecture_mode": arguments.target_architecture_mode,
                "target_evidence_policy": arguments.target_evidence_policy,
            }
            if arguments.check:
                result = compiler_api.check_file_snapshot(
                    arguments.source,
                    source_text,
                    **compile_options,
                )
            else:
                result = _compile_artifact_snapshot(
                    arguments.source,
                    source_text,
                    **compile_options,
                )
                # Demand the normal selected/planned hardware while source and
                # top diagnostics are still handled by this request boundary.
                result.ir
        except SessionTopSelectionError as error:
            raise compiler_api.TopSelectionError(str(error)) from error
        except (ParseError, SemanticError, compiler_api.TopSelectionError) as error:
            if arguments.check and len(compile_tops) > 1:
                raise SemanticError(
                    f"while checking module '{compile_top}': {error}",
                    code=error.code,
                    primary=error.primary,
                    notes=error.notes,
                    fixes=error.fixes,
                    machine_fixes=error.machine_fixes,
                ) from error
            raise
    assert result is not None
    return CompiledSource(result, source_bytes, source_digest, checked_module_names)

def run_compile_command(effective_argv: list[str]) -> int:
    parser = cli_arguments._build_compile_parser()
    arguments = parser.parse_args(effective_argv)
    constraint_requested = cli_arguments._validate_compile_arguments(parser, arguments)

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
        compiled = compile_source_request(arguments, verification_work_directory)
    except UnicodeDecodeError as error:
        print_cli_diagnostic(
            error,
            arguments.diagnostic_format,
            legacy_message=(
                f"cannot read source file '{arguments.source.name}': invalid UTF-8"
            ),
            source=arguments.source,
            project=arguments.project,
        )
        return 1
    except OSError as error:
        detail = error.strerror or "unable to read source file"
        print_cli_diagnostic(
            error,
            arguments.diagnostic_format,
            legacy_message=(
                f"cannot read source file '{arguments.source.name}': {detail}"
            ),
            source=arguments.source,
            project=arguments.project,
        )
        return 1
    except (
        DiagnosticError,
        source_api.SourceExtensionError,
        compiler_api.TopSelectionError,
        WorkspaceError,
    ) as error:
        # Keep the Python API typed, but make source failures concise and
        # artifact-safe at the CLI boundary.  argparse usage errors below
        # intentionally retain exit status 2.
        print_cli_diagnostic(
            error,
            arguments.diagnostic_format,
            source=arguments.source,
            project=arguments.project,
        )
        return 1
    result = compiled.result
    source_bytes = compiled.source_bytes
    source_digest = compiled.source_digest
    checked_module_names = compiled.checked_module_names
    try:
        cli_arguments.preflight_compilation_input_paths(arguments, result)
    except ValueError as error:
        parser.error(str(error))
    selected_platform_profile = None
    if arguments.profile is not None and result.physical_inputs.project_manifest is not None:
        try:
            selected_platform_profile = constraint_api.parse_platform_profile(
                project_api.ProjectManifest.load(result.physical_inputs.project_manifest),
                arguments.profile,
            )
        except (constraint_api.PlatformConstraintError, OSError) as error:
            parser.error(str(error))
    if arguments.check:
        checked = (
            f"top {result.ir.name}"
            if arguments.top is not None
            else f"{len(checked_module_names)} module"
            f"{'s' if len(checked_module_names) != 1 else ''}"
        )
        print(
            f"{source_api.CLI_NAME}: ok: {arguments.source} "
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
        parser.error("--pipeline-report requires an implementation region")
    if arguments.architecture_report is not None and not result.architecture_report:
        parser.error("--architecture-report requires an implementation region")
    if arguments.exploration_report is not None and not result.exploration_report:
        parser.error(
            "--exploration-report requires an implementation-selection expression"
        )
    synthesis_report = ""
    synthesis_feedback = None
    if arguments.synthesis_report is not None:
        assert arguments.synthesis_cache is not None
        try:
            feedback = synthesis_api.characterize_with_yosys(
                result.ir,
                arguments.synthesis_cache,
                yosys_executable=arguments.yosys,
                target=synthesis_api.YosysTarget(arguments.synthesis_target),
            )
        except synthesis_api.SynthesisFeedbackError as error:
            parser.error(str(error))
        measured_optimization_ir = lower(
            feedback.module,
            stage=OptimizationStage.SELECTED_ARCHITECTURE,
        )
        measured_ir = restore(measured_optimization_ir)
        if measured_ir != feedback.module:
            raise RuntimeError("measured optimization IR did not restore typed IR")
        if isinstance(result, _CliCompilation):
            result = result.with_measured_ir(
                ir=measured_ir,
                optimization_ir=measured_optimization_ir,
                implementation_report=render_implementation_report(measured_ir),
            )
        else:
            result = replace(
                result,
                ir=measured_ir,
                optimization_ir=measured_optimization_ir,
                implementation_report=render_implementation_report(measured_ir),
            )
        synthesis_report = synthesis_api.render_synthesis_report(feedback)
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
    systemverilog_output = arguments.systemverilog
    if arguments.source_map is not None:
        if systemverilog_output is None:
            parser.error("--source-map requires --systemverilog")
    direct_systemverilog = ""
    direct_artifact = None
    simulation_state_bundle = None
    external_mappings = ()
    if systemverilog_output is not None:
        try:
            if result.physical_inputs.project_manifest is not None:
                manifest = project_api.ProjectManifest.load(result.physical_inputs.project_manifest)
                assert result.physical_inputs.project_lock is not None
                lock = project_api.ProjectLock.load(result.physical_inputs.project_lock)
                external_mappings = load_profile_external_mappings(
                    manifest, lock, arguments.profile, result.ir
                )
            if result.implementation_graph is not None and not result.implementation_graph.is_generic:
                direct_artifact = sv_backend.emit_target_artifact(
                    result.ir,
                    result.implementation_graph,
                    selected_ir_identity=result.selected_ir_identity,
                )
            else:
                direct_artifact = sv_backend.emit_artifact(
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
                simulation_state_bundle = sv_backend.build_systemverilog_simulation_state_bundle(
                    result.ir, direct_artifact
                )
        except (sv_backend.ExternalMappingError, sv_backend.SystemVerilogEmissionError) as error:
            if arguments.diagnostic_format == "json":
                print_cli_diagnostic(error, "json")
                return 2
            parser.error(str(error))
        except SimulationStateError as error:
            parser.error(str(error))
    if arguments.formal_depth < 1:
        parser.error("--formal-depth must be positive")
    verification = cli_verification._run_requested_verification(
        parser,
        arguments,
        result,
        verification_work_directory,
    )
    if verification.stop:
        return verification.exit_code
    verification_exit_code = verification.exit_code
    published = cli_publication._publish_requested_artifacts(
        parser,
        arguments,
        result,
        constraint_requested=constraint_requested,
        selected_platform_profile=selected_platform_profile,
        systemverilog_output=systemverilog_output,
        direct_systemverilog=direct_systemverilog,
        direct_artifact=direct_artifact,
        simulation_state_bundle=simulation_state_bundle,
        source_digest=source_digest,
        synthesis_report=synthesis_report,
        saturation_report=saturation_report,
        verification=verification,
    )
    cli_publication._publish_build_manifest(
        parser,
        arguments,
        result,
        source_bytes=source_bytes,
        direct_artifact=direct_artifact,
        systemverilog_output=systemverilog_output,
        direct_companion_paths=published.companion_paths,
        constraint_products=published.constraints,
        source_map=published.source_map,
        evidence_records=published.evidence,
        rendered_evidence=published.rendered_evidence,
        synthesis_feedback=synthesis_feedback,
        tool_executions=published.tool_executions,
        selected_platform_profile=selected_platform_profile,
    )
    if arguments.verbose:
        written_paths: list[Path] = []
        for attribute in cli_arguments.ARTIFACT_SINK_ATTRIBUTES:
            path = getattr(arguments, attribute, None)
            if path is not None and path not in written_paths:
                written_paths.append(path)
        detail = (
            "; wrote " + ", ".join(str(path) for path in written_paths)
            if written_paths
            else "; direct SystemVerilog emitted to stdout"
        )
        print(
            f"{source_api.CLI_NAME}: ok: {arguments.source} (top {result.ir.name}){detail}",
            file=sys.stderr,
        )
    return verification_exit_code
