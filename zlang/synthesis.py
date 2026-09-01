"""Optional cached Yosys characterization and measured-cost feedback."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from zlang.backend.clash import emit
from zlang.implementations import select_implementation
from zlang.ir import expressions as expr
from zlang.ir.module import Assignment, Module
from zlang.ir.module import dependency_context_identity
from zlang.opt import lower, render
from zlang.common import stable_digest
from zlang.toolchain import (
    ToolchainError,
    clash_subprocess_environment,
    find_clash_executable,
    generate_verilog,
)


CACHE_SCHEMA = "zlang-yosys-feedback-v1"


class SynthesisFeedbackError(RuntimeError):
    """Optional synthesis feedback could not be produced or applied."""


@dataclass(frozen=True)
class YosysTarget:
    name: str = "generic-lut6"
    lut_inputs: int = 6
    flatten: bool = True
    exclude_flip_flops_from_depth: bool = True

    def __post_init__(self) -> None:
        if self.name != "generic-lut6" or self.lut_inputs != 6:
            raise ValueError("Milestone 21 supports only the generic-lut6 target")

    @property
    def constraints(self) -> tuple[tuple[str, str], ...]:
        return (
            ("flatten", str(self.flatten).lower()),
            ("lut_inputs", str(self.lut_inputs)),
            (
                "exclude_flip_flops_from_depth",
                str(self.exclude_flip_flops_from_depth).lower(),
            ),
        )


@dataclass(frozen=True)
class SynthesisCandidateResult:
    output: str
    kind: expr.ImplementationKind
    estimate: expr.ImplementationCostEstimate
    measurement: expr.YosysMeasurement
    cache_hit: bool
    legal: bool
    violations: tuple[str, ...]
    objective_value: int
    objective_source: str


@dataclass(frozen=True)
class SynthesisDecision:
    output: str
    goal: expr.CostMetric
    constraints: tuple[expr.CostConstraint, ...]
    selected: expr.ImplementationKind


@dataclass(frozen=True)
class SynthesisFeedbackResult:
    module: Module
    target: YosysTarget
    yosys_version: str
    clash_version: str
    candidates: tuple[SynthesisCandidateResult, ...]
    decisions: tuple[SynthesisDecision, ...]


def normalized_candidate_hash(
    module: Module,
    output: str,
    kind: expr.ImplementationKind | str,
) -> str:
    """Hash normalized typed behavior plus explicit implementation intent."""

    selected = (
        kind
        if isinstance(kind, expr.ImplementationKind)
        else expr.ImplementationKind(kind)
    )
    _, choice = _find_choice(module, output)
    alternative = next(
        (item for item in choice.alternatives if item.kind is selected),
        None,
    )
    if alternative is None:
        raise SynthesisFeedbackError(
            f"output '{output}' has no '{selected.value}' candidate"
        )
    normalized_assignments: list[Assignment] = []
    for assignment in module.assignments:
        expression = assignment.expression
        if isinstance(expression, expr.ImplementationChoice):
            if assignment.target.name == output:
                expression = alternative.expression
            else:
                expression = min(
                    expression.alternatives,
                    key=lambda item: item.kind.value,
                ).expression
            assignment = replace(assignment, expression=expression)
        normalized_assignments.append(assignment)
    normalized = replace(module, assignments=tuple(normalized_assignments))
    identity_lines = [
        CACHE_SCHEMA,
        f"implementation_kind={selected.value}",
        f"applicability={alternative.applicability!r}",
        f"semantics={alternative.semantics!r}",
    ]
    dependency_identity = dependency_context_identity(module)
    if dependency_identity is not None:
        identity_lines.append(f"dependency_identity={dependency_identity}")
    identity_lines.append(render(lower(normalized), include_origins=False))
    payload = "\n".join(identity_lines)
    return stable_digest(payload)


def characterize_with_yosys(
    module: Module,
    cache_directory: Path,
    *,
    clash_executable: str | None = None,
    yosys_executable: str | None = None,
    target: YosysTarget = YosysTarget(),
) -> SynthesisFeedbackResult:
    """Characterize opted-in choices and re-extract using measured feedback."""

    choices = tuple(
        (assignment.target.name, assignment.expression)
        for assignment in module.assignments
        if isinstance(assignment.expression, expr.ImplementationChoice)
        and assignment.expression.cost_policy is not None
        and assignment.expression.cost_policy.feedback
        is expr.SynthesisFeedback.OPTIONAL_YOSYS
    )
    if not choices:
        raise SynthesisFeedbackError(
            "synthesis feedback requires choice(auto,...,feedback=optional_yosys)"
        )
    clash = clash_executable or find_clash_executable()
    if clash is None or not _is_executable(clash):
        raise SynthesisFeedbackError(
            "Clash executable was not found for synthesis feedback"
        )
    yosys = yosys_executable or shutil.which("yosys")
    if yosys is None or not _is_executable(yosys):
        raise SynthesisFeedbackError(
            "Yosys executable was not found; install Yosys or omit synthesis feedback"
        )
    clash_version = _tool_version(
        (clash, "--version"),
        environment=clash_subprocess_environment(clash),
    )
    yosys_version = _tool_version((yosys, "-V"))
    cache_directory.mkdir(parents=True, exist_ok=True)

    assignments = list(module.assignments)
    results: list[SynthesisCandidateResult] = []
    decisions: list[SynthesisDecision] = []
    for output, original_choice in choices:
        policy = original_choice.cost_policy
        assert policy is not None
        characterized: list[expr.ImplementationAlternative] = []
        cache_hits: dict[expr.ImplementationKind, bool] = {}
        for alternative in original_choice.alternatives:
            if alternative.estimate is None:
                raise SynthesisFeedbackError(
                    f"candidate '{output}.{alternative.kind.value}' has no estimate"
                )
            candidate_hash = normalized_candidate_hash(
                module, output, alternative.kind
            )
            measurement, cache_hit = _load_or_measure(
                module,
                output,
                alternative.kind,
                candidate_hash,
                cache_directory,
                clash,
                clash_version,
                yosys,
                yosys_version,
                target,
            )
            characterized.append(
                replace(alternative, measurement=measurement)
            )
            cache_hits[alternative.kind] = cache_hit

        assessments: list[SynthesisCandidateResult] = []
        for alternative in characterized:
            estimate = alternative.estimate
            measurement = alternative.measurement
            assert estimate is not None and measurement is not None
            violations = tuple(
                _render_violation(constraint, estimate, measurement)
                for constraint in policy.constraints
                if _feedback_metric_value(
                    constraint.metric, estimate, measurement
                )[0]
                > constraint.maximum
            )
            objective_value, objective_source = _feedback_metric_value(
                policy.goal, estimate, measurement
            )
            assessments.append(
                SynthesisCandidateResult(
                    output,
                    alternative.kind,
                    estimate,
                    measurement,
                    cache_hits[alternative.kind],
                    not violations,
                    violations,
                    objective_value,
                    objective_source,
                )
            )
        legal = tuple(item for item in assessments if item.legal)
        if not legal:
            detail = "; ".join(
                f"{item.kind.value} violates " + ", ".join(item.violations)
                for item in sorted(assessments, key=lambda item: item.kind.value)
            )
            raise SynthesisFeedbackError(
                f"no legal measured implementation for output '{output}': {detail}"
            )
        selected = min(
            legal,
            key=lambda item: (
                item.objective_value,
                item.measurement.logic_depth,
                item.kind.value,
            ),
        )
        assignment_index, _ = _find_choice(module, output)
        assignments[assignment_index] = replace(
            assignments[assignment_index],
            expression=replace(
                original_choice,
                selected=selected.kind,
                alternatives=tuple(characterized),
            ),
        )
        results.extend(assessments)
        decisions.append(
            SynthesisDecision(
                output,
                policy.goal,
                policy.constraints,
                selected.kind,
            )
        )

    measured_module = replace(module, assignments=tuple(assignments))
    return SynthesisFeedbackResult(
        measured_module,
        target,
        yosys_version,
        clash_version,
        tuple(results),
        tuple(decisions),
    )


def render_synthesis_report(result: SynthesisFeedbackResult) -> str:
    """Render estimates, measurements, cache provenance, and extraction."""

    constraints = ",".join(
        f"{name}={value}" for name, value in result.target.constraints
    )
    lines = [
        f"module {result.module.name}",
        "feedback_source=measured tool=yosys "
        f"version=[{result.yosys_version}] clash_version=[{result.clash_version}]",
        f"target={result.target.name} constraints=[{constraints}]",
    ]
    decisions = {decision.output: decision for decision in result.decisions}
    for candidate in sorted(
        result.candidates,
        key=lambda item: (item.output, item.kind.value),
    ):
        estimate = candidate.estimate
        measurement = candidate.measurement
        violations = ",".join(candidate.violations) or "none"
        lines.extend(
            (
                f"candidate output={candidate.output} kind={candidate.kind.value} "
                f"candidate_hash={measurement.candidate_hash} "
                f"cache_key={measurement.cache_key} "
                f"cache_hit={str(candidate.cache_hit).lower()}",
                f"  estimate lut={estimate.lut} ff={estimate.ff} "
                f"dsp={estimate.dsp} bram={estimate.bram} "
                f"latency={estimate.latency} ii={estimate.initiation_interval}",
                f"  measured target={measurement.target} "
                f"lut_cells={measurement.lut_cells} "
                f"flip_flops={measurement.flip_flops} "
                f"total_cells={measurement.total_cells} "
                f"logic_depth={measurement.logic_depth}",
                f"  legal={str(candidate.legal).lower()} "
                f"violations=[{violations}] objective="
                f"{candidate.objective_value} "
                f"objective_source={candidate.objective_source}",
            )
        )
    for output, decision in sorted(decisions.items()):
        selected = next(
            item
            for item in result.candidates
            if item.output == output and item.kind is decision.selected
        )
        hard_constraints = ",".join(
            f"{constraint.metric.value}<={constraint.maximum}"
            f"({_feedback_metric_value(constraint.metric, selected.estimate, selected.measurement)[1]})"
            for constraint in decision.constraints
        )
        lines.append(
            f"selected output={output} kind={decision.selected.value} "
            f"goal=minimize_{decision.goal.value} "
            f"hard_constraints=[{hard_constraints}] "
            f"objective_source={selected.objective_source} "
            f"objective={selected.objective_value} "
            f"measured_logic_depth_tie_break={selected.measurement.logic_depth} "
            "final_tie_break=implementation_kind"
        )
    return "\n".join(lines) + "\n"


def _load_or_measure(
    module: Module,
    output: str,
    kind: expr.ImplementationKind,
    candidate_hash: str,
    cache_directory: Path,
    clash_executable: str,
    clash_version: str,
    yosys_executable: str,
    yosys_version: str,
    target: YosysTarget,
) -> tuple[expr.YosysMeasurement, bool]:
    key_payload = {
        "schema": CACHE_SCHEMA,
        "candidate_hash": candidate_hash,
        "clash_version": clash_version,
        "yosys_version": yosys_version,
        "target": target.name,
        "constraints": target.constraints,
    }
    dependency_identity = dependency_context_identity(module)
    if dependency_identity is not None:
        key_payload["dependency_identity"] = dependency_identity
    cache_key = stable_digest(key_payload)
    cache_path = cache_directory / f"{cache_key}.json"
    if cache_path.is_file():
        try:
            payload = json.loads(cache_path.read_text())
            if payload.get("schema") != CACHE_SCHEMA:
                raise ValueError("schema mismatch")
            measurement_data = payload["measurement"]
            measurement_data["constraints"] = tuple(
                tuple(item) for item in measurement_data["constraints"]
            )
            measurement = expr.YosysMeasurement(**measurement_data)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise SynthesisFeedbackError(
                f"invalid synthesis cache entry '{cache_path}': {error}"
            ) from error
        if measurement.cache_key != cache_key:
            raise SynthesisFeedbackError(
                f"synthesis cache entry '{cache_path}' has the wrong key"
            )
        expected = (
            measurement.candidate_hash == candidate_hash
            and measurement.yosys_version == yosys_version
            and measurement.clash_version == clash_version
            and measurement.target == target.name
            and measurement.constraints == target.constraints
        )
        if not expected:
            raise SynthesisFeedbackError(
                f"synthesis cache entry '{cache_path}' has mismatched provenance"
            )
        return measurement, True

    measurement = _measure_candidate(
        module,
        output,
        kind,
        candidate_hash,
        cache_key,
        clash_executable,
        clash_version,
        yosys_executable,
        yosys_version,
        target,
    )
    cache_path.write_text(
        json.dumps(
            {
                "schema": CACHE_SCHEMA,
                "measurement": {
                    "candidate_hash": measurement.candidate_hash,
                    "cache_key": measurement.cache_key,
                    "yosys_version": measurement.yosys_version,
                    "clash_version": measurement.clash_version,
                    "target": measurement.target,
                    "constraints": measurement.constraints,
                    "lut_cells": measurement.lut_cells,
                    "flip_flops": measurement.flip_flops,
                    "total_cells": measurement.total_cells,
                    "logic_depth": measurement.logic_depth,
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return measurement, False


def _measure_candidate(
    module: Module,
    output: str,
    kind: expr.ImplementationKind,
    candidate_hash: str,
    cache_key: str,
    clash_executable: str,
    clash_version: str,
    yosys_executable: str,
    yosys_version: str,
    target: YosysTarget,
) -> expr.YosysMeasurement:
    candidate_module = select_implementation(module, output, kind)
    with tempfile.TemporaryDirectory(prefix="zlang-yosys-") as temporary:
        root = Path(temporary)
        try:
            verilog_files = generate_verilog(
                emit(candidate_module),
                candidate_module.name,
                root / "rtl",
                clash_executable,
            )
        except ToolchainError as error:
            raise SynthesisFeedbackError(str(error)) from error
        stats_path = root / "stats.json"
        depth_path = root / "depth.txt"
        read_files = " ".join(_yosys_quote(path) for path in verilog_files)
        script = "; ".join(
            (
                f"read_verilog {read_files}",
                f"hierarchy -check -top {candidate_module.name}",
                f"synth -top {candidate_module.name} -flatten",
                f"abc -lut {target.lut_inputs}",
                "clean",
                f"tee -o {_yosys_quote(stats_path)} stat -json",
                f"tee -o {_yosys_quote(depth_path)} ltp -noff",
            )
        )
        completed = subprocess.run(
            (yosys_executable, "-q", "-p", script),
            text=True,
            capture_output=True,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise SynthesisFeedbackError(
                f"Yosys characterization failed for "
                f"'{output}.{kind.value}': {detail}"
            )
        try:
            statistics = json.loads(stats_path.read_text())["design"]
            cell_types = statistics["num_cells_by_type"]
            total_cells = int(statistics["num_cells"])
            depth_text = depth_path.read_text()
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise SynthesisFeedbackError(
                f"Yosys returned invalid statistics for '{output}.{kind.value}'"
            ) from error
    depth_match = re.search(r"Longest topological path.*\(length=(\d+)\)", depth_text)
    if depth_match is None:
        raise SynthesisFeedbackError(
            f"Yosys did not report logic depth for '{output}.{kind.value}'"
        )
    return expr.YosysMeasurement(
        candidate_hash=candidate_hash,
        cache_key=cache_key,
        yosys_version=yosys_version,
        clash_version=clash_version,
        target=target.name,
        constraints=target.constraints,
        lut_cells=int(cell_types.get("$lut", 0)),
        flip_flops=sum(
            int(count)
            for cell_type, count in cell_types.items()
            if "DFF" in cell_type.upper()
        ),
        total_cells=total_cells,
        logic_depth=int(depth_match.group(1)),
    )


def _feedback_metric_value(
    metric: expr.CostMetric,
    estimate: expr.ImplementationCostEstimate,
    measurement: expr.YosysMeasurement,
) -> tuple[int, str]:
    if metric is expr.CostMetric.LUT:
        return measurement.lut_cells, "measured_yosys_lut6"
    if metric is expr.CostMetric.FF:
        return measurement.flip_flops, "measured_yosys"
    return estimate.value(metric), "estimate"


def _render_violation(
    constraint: expr.CostConstraint,
    estimate: expr.ImplementationCostEstimate,
    measurement: expr.YosysMeasurement,
) -> str:
    value, source = _feedback_metric_value(
        constraint.metric, estimate, measurement
    )
    return (
        f"{constraint.metric.value}({source})={value} > {constraint.maximum}"
    )


def _find_choice(
    module: Module,
    output: str,
) -> tuple[int, expr.ImplementationChoice]:
    matches = tuple(
        (index, assignment.expression)
        for index, assignment in enumerate(module.assignments)
        if assignment.target.name == output
        and isinstance(assignment.expression, expr.ImplementationChoice)
    )
    if len(matches) != 1:
        raise SynthesisFeedbackError(
            f"output '{output}' does not have one implementation choice"
        )
    index, choice = matches[0]
    assert isinstance(choice, expr.ImplementationChoice)
    return index, choice


def _tool_version(
    command: tuple[str, ...],
    *,
    environment: dict[str, str] | None = None,
) -> str:
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        env=environment,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise SynthesisFeedbackError(
            f"could not query tool version for '{command[0]}': {detail}"
        )
    return (completed.stdout.strip() or completed.stderr.strip()).splitlines()[0]


def _is_executable(path: str) -> bool:
    candidate = Path(path)
    return candidate.is_file() and candidate.stat().st_mode & 0o111 != 0


def _yosys_quote(path: Path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'
