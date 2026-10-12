"""Compiler command parser and argument validation."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from zlang._version import __version__
from zlang.backend.companions import CompanionArtifact
from zlang.costs import SourcePolicy
from zlang.formal_exploration import FormalPolicy
from zlang.source_identity import CLI_NAME, PUBLIC_LANGUAGE_NAME, SOURCE_SUFFIX
from zlang.targets import ArchitectureSelectionMode



ARTIFACT_SINK_ATTRIBUTES = (
    "systemverilog",
    "simulation_state_bundle",
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
    "verification_bundle",
    "verification_report",
    "synthesis_report",
    "source_map",
    "constraints_xdc",
    "constraints_sdc",
    "evidence_report",
    "build_manifest",
    "generated_navigation_bundle",
)

OWNED_OUTPUT_DIRECTORY_ATTRIBUTES = (
    "verification_bundle",
    "simulation_state_bundle",
    "formal_cache",
    "synthesis_cache",
    "generated_navigation_bundle",
)

EXPLICIT_FILE_SINK_ATTRIBUTES = tuple(
    attribute
    for attribute in ARTIFACT_SINK_ATTRIBUTES
    if attribute not in OWNED_OUTPUT_DIRECTORY_ATTRIBUTES
)


def has_explicit_artifact_sink(arguments: argparse.Namespace) -> bool:
    """Return whether the invocation explicitly requests a file artifact."""

    return any(
        getattr(arguments, attribute, None) is not None
        for attribute in ARTIFACT_SINK_ATTRIBUTES
    )


def resolved_cli_path(path: Path) -> Path:
    """Resolve aliases without requiring a not-yet-created output to exist."""

    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"cannot resolve publication path '{path}': {error}") from error


def option_for_attribute(attribute: str) -> str:
    return "--" + attribute.replace("_", "-")


def _resolved_attributes(
    arguments: argparse.Namespace,
    attributes: Sequence[str],
) -> tuple[tuple[str, Path], ...]:
    return tuple(
        (attribute, resolved_cli_path(path))
        for attribute in attributes
        if (path := getattr(arguments, attribute, None)) is not None
    )


def paths_overlap_file_and_directory(file_path: Path, directory_path: Path) -> bool:
    """Return whether a file sink and an owned directory cannot coexist safely."""

    return (
        file_path == directory_path
        or file_path.is_relative_to(directory_path)
        or directory_path.is_relative_to(file_path)
    )


def preflight_explicit_output_paths(arguments: argparse.Namespace) -> None:
    """Reject resolved input/output aliases before compilation writes anything."""

    source_path = resolved_cli_path(arguments.source)
    file_sinks = _resolved_attributes(arguments, EXPLICIT_FILE_SINK_ATTRIBUTES)
    for attribute, sink_path in file_sinks:
        if sink_path == source_path:
            if attribute == "build_manifest":
                raise ValueError(
                    "--build-manifest output collides with the source file"
                )
            raise ValueError(
                f"explicit sink {option_for_attribute(attribute)} "
                "collides with the source file"
            )

    for index, (left_attribute, left_path) in enumerate(file_sinks):
        for right_attribute, right_path in file_sinks[index + 1:]:
            attributes = frozenset({left_attribute, right_attribute})
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
                    f"{option_for_attribute(other)}"
                )
            raise ValueError(
                f"explicit sinks {option_for_attribute(left_attribute)} and "
                f"{option_for_attribute(right_attribute)} resolve to the same path"
            )

    owned_directories = _resolved_attributes(
        arguments, OWNED_OUTPUT_DIRECTORY_ATTRIBUTES
    )
    for attribute, directory_path in owned_directories:
        option = option_for_attribute(attribute)
        if paths_overlap_file_and_directory(source_path, directory_path):
            raise ValueError(
                f"source file collides with explicit output directory {option}"
            )
        for sink_attribute, sink_path in file_sinks:
            if not paths_overlap_file_and_directory(sink_path, directory_path):
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
                f"explicit sink {option_for_attribute(sink_attribute)} collides "
                f"with explicit output directory {option}"
            )

    for index, (left_attribute, left_path) in enumerate(owned_directories):
        for right_attribute, right_path in owned_directories[index + 1:]:
            if not paths_overlap_file_and_directory(left_path, right_path):
                continue
            raise ValueError(
                f"explicit output directories {option_for_attribute(left_attribute)} "
                f"and {option_for_attribute(right_attribute)} overlap"
            )


def preflight_compilation_input_paths(
    arguments: argparse.Namespace,
    result: object,
) -> None:
    """Protect every physical compiler input discovered during compilation."""

    inputs = result.physical_inputs.all_paths
    for attribute, sink_path in _resolved_attributes(
        arguments, EXPLICIT_FILE_SINK_ATTRIBUTES
    ):
        collision = next((path for path in inputs if path == sink_path), None)
        if collision is not None:
            raise ValueError(
                f"explicit sink {option_for_attribute(attribute)} collides "
                f"with compilation input '{collision}'"
            )

    for attribute, directory_path in _resolved_attributes(
        arguments, OWNED_OUTPUT_DIRECTORY_ATTRIBUTES
    ):
        collision = next(
            (
                path
                for path in inputs
                if paths_overlap_file_and_directory(path, directory_path)
            ),
            None,
        )
        if collision is not None:
            raise ValueError(
                f"explicit output directory {option_for_attribute(attribute)} "
                f"collides with compilation input '{collision}'"
            )


def preflight_companion_paths(
    arguments: argparse.Namespace,
    *,
    systemverilog_output: Path | None,
    direct_companions: Sequence[CompanionArtifact],
    compilation_inputs: Sequence[Path],
) -> None:
    """Reject deterministic companion aliases before publishing any product."""

    destinations: list[tuple[str, Path, CompanionArtifact]] = []
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
        destination_path = resolved_cli_path(destination)
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

    file_sinks = _resolved_attributes(arguments, EXPLICIT_FILE_SINK_ATTRIBUTES)
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
                f"{option_for_attribute(sink_attribute)}"
            )
        if destination_path in input_paths:
            raise ValueError(
                f"deterministic {description} collides with compilation input "
                f"'{destination_path}'"
            )
def _build_compile_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=CLI_NAME,
        usage=(
            "%(prog)s SOURCE [OPTIONS]\n"
            "       %(prog)s sim SOURCE [SIMULATION OPTIONS]\n"
            "       %(prog)s verify BUNDLE [VERIFICATION OPTIONS]\n"
            "       %(prog)s lock update [PROJECT OPTIONS]\n"
            "       %(prog)s lsp"
        ),
        description=f"Compile or check {PUBLIC_LANGUAGE_NAME} source",
        epilog=(
            "subcommands:\n"
            "  zlang sim SOURCE --top TOP --clock CLK --cycles N\n"
            "  zlang verify BUNDLE --mode bmc --depth 20\n"
            "  zlang lock update --project zlang.toml\n"
            "  zlang lsp\n"
            "  Run 'zlang sim --help' or 'zlang COMMAND --help' for a "
            "subcommand's complete interface.\n\n"
            "common examples:\n"
            "  zlang design.zhl --check\n"
            "  zlang design.zhl --top Top --systemverilog build/Top.sv\n"
            "  zlang sim design.zhl --top Top --clock clk --cycles 100 --json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    source_options = parser.add_argument_group("source selection")
    source_options.add_argument(
        "--top", help="select a top module from a multi-module source"
    )
    source_options.add_argument(
        "--project",
        type=Path,
        help="use this zlang.toml (or containing directory) instead of parent discovery",
    )
    source_options.add_argument(
        "--profile",
        help="select one strict implementation profile from zlang.toml",
    )
    source_options.add_argument(
        "--diagnostic-format",
        choices=("text", "json"),
        default="text",
        help="render compiler diagnostics as compatible text or stable JSON",
    )
    parser.add_argument("source", type=Path, help=f"input {SOURCE_SUFFIX} file")
    primary_actions = parser.add_argument_group("primary actions")
    primary_actions.add_argument(
        "--check",
        action="store_true",
        help="check syntax and semantics without emitting an artifact",
    )
    primary_actions.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="report successful compilation and explicitly written artifacts",
    )
    primary_actions.add_argument(
        "--systemverilog",
        type=Path,
        help="write direct SystemVerilog for the supported backend subset",
    )
    rtl_options = parser.add_argument_group("SystemVerilog and implementation tools")
    rtl_options.add_argument(
        "--simulation-state-bundle",
        type=Path,
        help=(
            "publish simulation-only Verilator VPI state bindings "
            "(requires --systemverilog)"
        ),
    )
    rtl_options.add_argument(
        "--constraints-xdc",
        type=Path,
        help="publish a typed single-domain XDC create_clock constraint",
    )
    rtl_options.add_argument(
        "--constraints-sdc",
        type=Path,
        help="publish a typed single-domain SDC create_clock constraint",
    )
    rtl_options.add_argument(
        "--verilator-lint",
        action="store_true",
        help="lint generated SystemVerilog with Verilator (requires --systemverilog)",
    )
    rtl_options.add_argument("--verilator", help="explicit Verilator executable")
    rtl_options.add_argument("--yosys", help="explicit Yosys executable")
    implementation_options = parser.add_argument_group("implementation selection")
    implementation_options.add_argument(
        "--target",
        help=(
            "select a compiler-shipped target instance (for example "
            "xc7z030ffg676-1 or sky130-fd-sc-hd)"
        ),
    )
    implementation_options.add_argument(
        "--target-architecture",
        help="manually select one source-described architecture template",
    )
    implementation_options.add_argument(
        "--target-architecture-mode",
        choices=tuple(item.value for item in ArchitectureSelectionMode),
        default=None,
        help="generic, preferred fallback, or required manual target architecture",
    )
    implementation_options.add_argument(
        "--target-evidence-policy",
        choices=tuple(item.value for item in SourcePolicy),
        default=None,
        help="estimate_only, measured_preferred, or routed-Fmax measured_required",
    )
    build_artifacts = parser.add_argument_group("build artifacts")
    build_artifacts.add_argument(
        "--implementation-manifest",
        type=Path,
        help="write the versioned selected-resource BackendArtifact manifest",
    )
    build_artifacts.add_argument(
        "--source-map",
        type=Path,
        help=(
            "write a generated-line to ZLang-origin sidecar for exactly one "
            "explicit direct-SystemVerilog output"
        ),
    )
    build_artifacts.add_argument(
        "--generated-navigation-bundle",
        type=Path,
        help=(
            "publish a relocatable, hash-validated generated RTL/source-map "
            "bundle (requires --systemverilog)"
        ),
    )
    build_artifacts.add_argument(
        "--evidence-report",
        type=Path,
        help="write deterministic typed build evidence",
    )
    build_artifacts.add_argument(
        "--evidence-format",
        choices=("text", "json"),
        default="json",
        help="format selected by --evidence-report (default: json)",
    )
    build_artifacts.add_argument(
        "--build-manifest",
        type=Path,
        help="write a deterministic whole-build manifest after validating outputs",
    )
    build_artifacts.add_argument(
        "--csr-markdown",
        type=Path,
        help="write CSR Markdown documentation (requires a CSR block)",
    )
    build_artifacts.add_argument(
        "--csr-json",
        type=Path,
        help="write the software-readable CSR JSON map (requires a CSR block)",
    )
    build_artifacts.add_argument(
        "--contracts-sva",
        type=Path,
        help="write bindable SystemVerilog assume/assert contracts",
    )
    inspection_options = parser.add_argument_group("compiler inspection reports")
    inspection_options.add_argument(
        "--high-level-ir",
        type=Path,
        help="write canonical IR before implementation-cost extraction",
    )
    inspection_options.add_argument(
        "--optimization-ir",
        type=Path,
        help="write the normalized canonical optimization IR",
    )
    inspection_options.add_argument(
        "--saturation-report",
        type=Path,
        help="write bounded equality-saturation alternatives for one output",
    )
    inspection_options.add_argument(
        "--saturate-output",
        help="wire output to inspect (requires --saturation-report)",
    )
    inspection_options.add_argument(
        "--implementation-report",
        type=Path,
        help="write explicit implementation applicability and timing metadata",
    )
    inspection_options.add_argument(
        "--cost-report",
        type=Path,
        help="write estimated candidate costs, constraints, and extraction choice",
    )
    inspection_options.add_argument(
        "--pipeline-report",
        type=Path,
        help="write automatic pipeline candidates, constraints, and selection",
    )
    inspection_options.add_argument(
        "--architecture-report",
        type=Path,
        help="write implementation architecture candidates and selection",
    )
    inspection_options.add_argument(
        "--exploration-report",
        type=Path,
        help="write unified explore candidate, rejection, and selection details",
    )
    inspection_options.add_argument(
        "--implementation-policy-report",
        type=Path,
        help="write the normalized profile/source/CLI implementation policy",
    )
    inspection_options.add_argument(
        "--backend-implementation-report",
        type=Path,
        help="write the direct-SystemVerilog implementation planning result",
    )
    formal_options = parser.add_argument_group("formal verification")
    formal_options.add_argument("--formal-depth", type=int, default=32, help="bounded formal depth")
    formal_options.add_argument("--formal-policy", choices=tuple(item.value for item in FormalPolicy),
                        default=None,
                        help="formal-aware selection formal exploration eligibility policy")
    formal_options.add_argument("--formal-max-candidates", type=int, default=8,
                        help="maximum candidates to execute formal proofs for")
    formal_options.add_argument("--formal-timeout", type=int, default=120,
                        help="per-candidate formal timeout in seconds")
    formal_options.add_argument(
        "--formal-jobs",
        type=int,
        default=1,
        help=(
            "maximum independent verification-bundle jobs and selected-candidate "
            "sites to run concurrently"
        ),
    )
    formal_options.add_argument(
        "--formal-cache",
        type=Path,
        help="versioned formal proof and verification-result cache directory",
    )
    formal_options.add_argument(
        "--verify",
        action="store_true",
        help="execute connected safety checks and bounded cover goals",
    )
    formal_options.add_argument(
        "--verification-bundle",
        type=Path,
        help="publish an immutable replayable verification bundle",
    )
    formal_options.add_argument(
        "--verification-report",
        type=Path,
        help="write the --verify result report",
    )
    formal_options.add_argument(
        "--verification-work-dir",
        type=Path,
        help="retain solver configurations, logs, and traces for --verify",
    )
    formal_options.add_argument(
        "--verification-format",
        choices=("text", "json"),
        default=None,
        help="verification report format (default: text)",
    )
    formal_options.add_argument(
        "--verify-require",
        choices=("checked", "proven"),
        default=None,
        help="required safety evidence: bounded checked or unbounded proven",
    )
    synthesis_options = parser.add_argument_group("measured synthesis")
    synthesis_options.add_argument(
        "--synthesis-report",
        type=Path,
        help="write cached Yosys measurements and feedback selection",
    )
    synthesis_options.add_argument(
        "--synthesis-cache",
        type=Path,
        help="cache directory for normalized-candidate Yosys measurements",
    )
    synthesis_options.add_argument(
        "--synthesis-target",
        choices=("generic-lut6",),
        default="generic-lut6",
        help="Yosys characterization target",
    )
    return parser


def _validate_compile_arguments(
    parser: argparse.ArgumentParser, arguments: argparse.Namespace,
) -> bool:
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
    if arguments.verilator_lint and arguments.systemverilog is None:
        parser.error("--verilator-lint requires --systemverilog")
    if arguments.simulation_state_bundle is not None and arguments.systemverilog is None:
        parser.error("--simulation-state-bundle requires --systemverilog")
    if arguments.generated_navigation_bundle is not None and arguments.systemverilog is None:
        parser.error("--generated-navigation-bundle requires --systemverilog")
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
        requested_backends = int(arguments.systemverilog is not None)
        if requested_backends != 1:
            parser.error(
                "constraint publication requires exactly one direct-SystemVerilog output"
            )
    try:
        preflight_explicit_output_paths(arguments)
    except ValueError as error:
        parser.error(str(error))
    if (
        arguments.build_manifest is not None
        and arguments.systemverilog is None
    ):
        parser.error(
            "--build-manifest requires a published direct-SystemVerilog output"
        )

    return constraint_requested
