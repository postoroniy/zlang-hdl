"""Bounded high-level target-aware planning for the first fixed FIR slice."""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
from typing import Iterable

from zlang.costs import (
    CandidateCost,
    CostExtractionError,
    ExtractionResult,
    MetricSource,
    MetricValue,
    SourcePolicy,
    extract_best,
)
from zlang.ir import expressions as expr
from zlang.ir.module import Module
from zlang.ir.pipelines import PipelineMetric, PipelineRelation
from zlang.ir.target import ImplementationGraph
from zlang.ir.signed_reductions import recognize_signed_product_reduction
from zlang.pipelines import pipeline_constraints_to_unified
from zlang.target_timing import build_signed_product_timing_dag, delay_ff_cost
from zlang.targets import (
    TargetArchitectureError,
    generic_implementation_graph,
    load_architecture_templates,
    load_target,
    map_auto_signed_product_configuration,
    map_auto_symmetric_configuration,
)


# v2 removes source-provenance spelling from the implementation-graph
# identity.  v1 records are intentionally rejected rather than interpreted as
# evidence for a canonical ``implement`` region.
EVIDENCE_SCHEMA = "zlang-target-qor-v2"
DEFAULT_EVIDENCE = Path(__file__).with_name("data") / "xc7z030_dsp_pipeline_qor.json"


@dataclass(frozen=True)
class MeasurementKey:
    target_identity: str
    target_part: str
    architecture_template_identity: str
    implementation_graph_identity: str
    pipeline_configuration_identity: str
    backend: str
    tool: str
    tool_version: str
    clock_period_ns: float

    @property
    def identity(self) -> str:
        return sha256(repr((EVIDENCE_SCHEMA, self)).encode()).hexdigest()


@dataclass(frozen=True)
class QoREvidence:
    key: MeasurementKey
    stage: MetricSource
    lut: int
    ff: int
    dsp: int
    bram: int
    fmax_mhz: float
    wns_ns: float | None = None
    provenance: str = ""

    def __post_init__(self) -> None:
        if self.stage not in {
            MetricSource.SYNTHESIS_MEASUREMENT,
            MetricSource.ROUTED_MEASUREMENT,
        }:
            raise ValueError("physical QoR evidence must be synthesis or routed")

    @property
    def identity(self) -> str:
        return sha256(repr((EVIDENCE_SCHEMA, self)).encode()).hexdigest()


@dataclass(frozen=True)
class TargetCandidate:
    name: str
    graph: ImplementationGraph
    cost: CandidateCost
    evidence: QoREvidence | None = None
    rejection_reasons: tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        return self.graph.identity

    @property
    def implementation_identity(self) -> str:
        # Keep extraction deterministic across source spellings and evidence
        # migrations.  Physical configurations with equal measured cost are
        # ordered by the number of active pipeline sites before their stable
        # configuration identity; the graph hash remains the immutable
        # artifact identity exposed by ``identity``.
        if self.graph.pipeline_configuration_identity is not None:
            return (
                f"{len(self.graph.active_pipeline_sites):04d}:"
                f"{self.graph.pipeline_configuration_identity}:"
                f"{self.graph.identity}"
            )
        return self.graph.identity

    @property
    def legal(self) -> bool:
        return not self.rejection_reasons


@dataclass(frozen=True)
class TargetPlanningResult:
    target: str | None
    requirements: tuple[tuple[str, str, int], ...]
    source_policy: SourcePolicy
    generated_candidates: tuple[TargetCandidate, ...]
    rejected_candidates: tuple[TargetCandidate, ...]
    selected_candidate: TargetCandidate
    extraction: ExtractionResult | None
    search_bound: int = 5

    @property
    def selected_graph(self) -> ImplementationGraph:
        return self.selected_candidate.graph

    @property
    def report(self) -> str:
        return render_target_planner_report(self)


def _requirements(module: Module):
    fixed = tuple(
        item for item in module.pipeline_explorations
        if isinstance(item.source_expression, expr.FixedConvert)
    )
    if not fixed:
        return None, (), None, None
    if len(fixed) != 1:
        raise TargetArchitectureError(
            "target-aware implementation selection accepts exactly one fixed "
            "implementation region"
        )
    exploration = fixed[0]
    exact = next((item.value for item in exploration.constraints
                  if item.metric is PipelineMetric.LATENCY
                  and item.relation is PipelineRelation.EXACT), None)
    maximum = next((item.value for item in exploration.constraints
                    if item.metric is PipelineMetric.LATENCY
                    and item.relation is PipelineRelation.MAXIMUM), None)
    normalized = tuple(
        ("ii" if item.metric is PipelineMetric.THROUGHPUT else item.metric.value,
         item.relation.value, item.value)
        for item in exploration.constraints
    )
    return exploration, normalized, exact, maximum


def _generic_candidate(module: Module, target, exploration, requirements) -> TargetCandidate:
    graph = generic_implementation_graph(module, target)
    selected = exploration.selected_candidate
    graph = replace(
        graph,
        latency=selected.latency,
        initiation_interval=selected.initiation_interval,
        policy_requirements=requirements,
    )
    conversion = exploration.source_expression
    reduction = (
        recognize_signed_product_reduction(conversion.expression)
        if isinstance(conversion, expr.FixedConvert) else None
    )
    if reduction is not None:
        graph = replace(
            graph,
            semantic_region_identity=reduction.semantic_identity,
            quantization=conversion,
            source_origin=reduction.source_origin,
            timing_dag=build_signed_product_timing_dag(
                reduction, quantization=conversion,
                output_latency=selected.latency,
                target_identity=target.identity if target else None,
            ),
        )
    cost = CandidateCost.estimate(
        lut=selected.estimate.lut,
        ff=selected.estimate.ff,
        dsp=selected.estimate.dsp,
        latency=selected.latency,
        ii=selected.initiation_interval,
        fmax_est=selected.estimate.fmax_mhz,
        structural_cost=len(selected.transformations),
    )
    return TargetCandidate("generic", graph, cost)


def _candidate_cost(graph: ImplementationGraph, evidence: QoREvidence | None) -> CandidateCost:
    compensation_ff = delay_ff_cost(graph.timing_dag)
    if evidence is None:
        return CandidateCost.estimate(
            lut=max(1, graph.resources[-1].semantic_mappings[-1].expression.type.width
                    if graph.resources[-1].semantic_mappings[-1].expression is not None else 48),
            ff=compensation_ff,
            dsp=len(graph.resources),
            bram=0,
            latency=graph.latency,
            ii=graph.initiation_interval,
            fmax_est=None,
            structural_cost=len(graph.resources) + len(graph.dedicated_edges),
        )
    source = evidence.stage
    return CandidateCost(
        MetricValue(evidence.lut, source),
        MetricValue(evidence.ff, source),
        MetricValue(evidence.dsp, source),
        MetricValue(evidence.bram, source),
        MetricValue(graph.latency, MetricSource.STRUCTURAL_ESTIMATE),
        MetricValue(graph.initiation_interval, MetricSource.STRUCTURAL_ESTIMATE),
        MetricValue(evidence.fmax_mhz, source),
        len(graph.resources) + len(graph.dedicated_edges),
    )


def _compatible_evidence(
    records: Iterable[QoREvidence], graph: ImplementationGraph,
    *, backend: str, tool: str, tool_version: str, clock_period_ns: float,
) -> QoREvidence | None:
    """Return evidence only for the exact immutable candidate identity.

    Source spelling is intentionally absent from ``MeasurementKey``.  That is
    safe because the key's implementation graph identity is the canonical
    typed/physical contract.  No target/template-only fallback is permitted:
    an ``implement`` candidate must never borrow a measurement for a different
    semantic graph merely because its physical configuration happens to match.
    """
    matches = tuple(item for item in records if (
        item.key.target_identity == graph.target_identity
        and item.key.target_part == graph.target_part
        and item.key.architecture_template_identity == graph.architecture_template_identity
        and item.key.implementation_graph_identity == graph.identity
        and item.key.pipeline_configuration_identity == graph.pipeline_configuration_identity
        and item.key.backend == backend
        and item.key.tool == tool
        and item.key.tool_version == tool_version
        and item.key.clock_period_ns == clock_period_ns
    ))
    if not matches:
        return None
    return max(matches, key=lambda item: (
        item.stage is MetricSource.ROUTED_MEASUREMENT, item.identity,
    ))


def load_qor_evidence(path: Path | None = None) -> tuple[QoREvidence, ...]:
    selected = path or DEFAULT_EVIDENCE
    if not selected.exists():
        return ()
    payload = json.loads(selected.read_text())
    if payload.get("schema") != EVIDENCE_SCHEMA:
        raise ValueError(f"unsupported target QoR evidence schema in '{selected}'")
    records = []
    for item in payload.get("records", ()):
        key = item["key"]
        records.append(QoREvidence(
            MeasurementKey(
                key["target_identity"], key["target_part"],
                key["architecture_template_identity"],
                key["implementation_graph_identity"],
                key["pipeline_configuration_identity"], key["backend"],
                key["tool"], key["tool_version"], float(key["clock_period_ns"]),
            ),
            MetricSource(item["stage"]), int(item["lut"]), int(item["ff"]),
            int(item["dsp"]), int(item["bram"]), float(item["fmax_mhz"]),
            float(item["wns_ns"]) if item.get("wns_ns") is not None else None,
            item.get("provenance", ""),
        ))
    return tuple(records)


def plan_target_pipeline(
    module: Module,
    *,
    target: str | None,
    source_policy: SourcePolicy | str = SourcePolicy.MEASURED_PREFERRED,
    evidence: Iterable[QoREvidence] | None = None,
    evidence_path: Path | None = None,
    backend: str = "direct_systemverilog",
    tool: str = "Vivado",
    tool_version: str = "2024.2",
    clock_period_ns: float = 10.0,
    architecture: str | None = None,
    required_architecture: bool = False,
) -> TargetPlanningResult | None:
    exploration, requirements, exact_latency, maximum_latency = _requirements(module)
    if exploration is None:
        return None
    policy = SourcePolicy(source_policy)
    selected_target = family = None
    resources = ()
    if target is not None and target != "generic":
        selected_target, family, resources = load_target(target)
    generic = _generic_candidate(module, selected_target, exploration, requirements)
    generated: list[TargetCandidate] = [generic]
    rejected: list[TargetCandidate] = []
    evidence_records = tuple(evidence) if evidence is not None else load_qor_evidence(evidence_path)
    if selected_target is not None:
        templates = (
            *load_architecture_templates(operation="symmetric_fir_cascade"),
            *load_architecture_templates(operation="signed_product_reduction"),
        )
        if architecture is not None:
            templates = tuple(item for item in templates if architecture in {
                item.identity, item.name,
            })
            if not templates:
                raise TargetArchitectureError(
                    f"unknown target-aware architecture '{architecture}'"
                )
        groups: dict[tuple[object, ...], list[object]] = {}
        for item in templates:
            groups.setdefault((
                item.operation, item.resource_name, item.resource_count,
                item.initiation_interval, item.dedicated_link,
            ), []).append(item)
        for group in sorted(groups.values(), key=lambda items: min(item.identity for item in items))[:8]:
            template = min(group, key=lambda item: (len(item.identity), item.identity))
            matches = tuple(item for item in resources if item.name == template.resource_name)
            if len(matches) != 1:
                rejected.append(replace(
                    generic, name=template.name,
                    rejection_reasons=(f"resource '{template.resource_name}' unavailable",),
                ))
                continue
            resource = matches[0]
            configurations = tuple(sorted(
                resource.pipeline_configurations,
                key=lambda item: (item.latency, item.name),
            ))[:8]
            for configuration in configurations:
                name = f"{template.name}/{configuration.name}"
                try:
                    if template.operation == "signed_product_reduction":
                        physical_coverage = set(
                            dict(resource.capabilities)
                            .get("physical_architectures", "").split(".")
                        )
                        if template.operation not in physical_coverage:
                            raise TargetArchitectureError(
                                f"resource '{resource.identity}' supports signed accumulator "
                                "arithmetic but its physical backend binding does not publish "
                                "signed_product_reduction emission"
                            )
                        try:
                            graph = map_auto_signed_product_configuration(
                                module, selected_target, family, resources, template,
                                configuration, exact_latency=exact_latency,
                            )
                        except ValueError as error:
                            # A physically realized configuration can still be
                            # reported when it cannot meet an exact latency
                            # contract.  Build its natural-latency graph so the
                            # unified extractor can classify it (and expose a
                            # useful rejection reason) instead of dropping it
                            # during target mapping.
                            if exact_latency is None or "exceeds exact latency" not in str(error):
                                raise
                            graph = map_auto_signed_product_configuration(
                                module, selected_target, family, resources, template,
                                configuration, exact_latency=None,
                            )
                    else:
                        try:
                            graph = map_auto_symmetric_configuration(
                                module, selected_target, family, resources, template,
                                configuration, exact_latency=exact_latency,
                            )
                        except ValueError as error:
                            if exact_latency is None or "exceeds exact latency" not in str(error):
                                raise
                            graph = map_auto_symmetric_configuration(
                                module, selected_target, family, resources, template,
                                configuration, exact_latency=None,
                            )
                    if graph.realization_backend != backend:
                        raise TargetArchitectureError(
                            f"architecture '{graph.architecture_template_identity}' "
                            f"is realized by {graph.realization_backend}, not {backend}"
                        )
                        # Keep every successfully realized candidate in the
                        # generated set.  Constraint legality (including
                        # maximum/exact latency) is evaluated centrally by
                        # the unified extractor below.  Rejecting a graph
                        # here made the report depend on whether a source
                        # policy came from one spelling rather than another,
                        # and hid otherwise valid
                        # target configurations from diagnostics.
                    route = _compatible_evidence(
                        evidence_records, graph, backend=backend, tool=tool,
                        tool_version=tool_version, clock_period_ns=clock_period_ns,
                    )
                    cost = _candidate_cost(graph, route if policy is not SourcePolicy.ESTIMATE_ONLY else None)
                    graph = replace(
                        graph,
                        policy_requirements=requirements,
                        objective="lut",
                        selected_cost=_cost_items(cost),
                        evidence_identity=route.identity if route else None,
                    )
                    generated.append(TargetCandidate(name, graph, cost, route))
                except (TargetArchitectureError, ValueError) as error:
                    rejected.append(TargetCandidate(
                        name, generic.graph, generic.cost, None, (str(error),),
                    ))
    eligible = tuple(
        item for item in generated
        if not (required_architecture and item.graph.is_generic)
    )
    try:
        extraction = extract_best(
            eligible,
            objective=expr.CostMetric.LUT,
            constraints=pipeline_constraints_to_unified(exploration.constraints),
            source_policy=policy,
            cost_fn=lambda item: item.cost,
        )
    except CostExtractionError as error:
        if required_architecture:
            raise TargetArchitectureError(
                f"required target architecture has no legal candidate: {error}"
            ) from error
        raise TargetArchitectureError(str(error)) from error
    for evaluation in extraction.evaluations:
        if not evaluation.legal:
            reasons = tuple(
                item.reason for item in evaluation.constraints
                if item.status != "satisfied"
            ) or ("objective metric is unknown",)
            rejected.append(replace(evaluation.candidate, rejection_reasons=reasons))
    selected = extraction.selected
    selected_graph = replace(
        selected.graph,
        selected_cost=_cost_items(extraction.selected_cost),
    )
    selected = replace(selected, graph=selected_graph)
    generated = [selected if item.identity == selected.identity else item for item in generated]
    return TargetPlanningResult(
        selected_target.identity if selected_target else target,
        requirements, policy, tuple(generated), tuple(rejected), selected,
        extraction, min(64, 1 + 8 * 8),
    )


def _cost_items(cost: CandidateCost) -> tuple[tuple[str, int | float | None, str], ...]:
    return tuple(
        (metric.value, cost.metric(metric).value, cost.metric(metric).source.value)
        for metric in (
            expr.CostMetric.LUT, expr.CostMetric.FF, expr.CostMetric.DSP,
            expr.CostMetric.BRAM, expr.CostMetric.LATENCY,
            expr.CostMetric.INITIATION_INTERVAL, expr.CostMetric.FMAX_EST,
        )
    )


def render_target_planner_report(result: TargetPlanningResult) -> str:
    lines = [
        "Target-aware pipeline planner: bounded exact fixed-product slice",
        f"target: {result.target or 'none'}",
        "requirements: " + ", ".join(
            f"{metric} {relation} {value}" for metric, relation, value in result.requirements
        ),
        f"source policy: {result.source_policy.value}; objective: minimize lut",
        f"semantic region: {result.selected_candidate.graph.semantic_region_identity}",
        f"selected realization backend: {result.selected_candidate.graph.realization_backend}",
        f"candidates considered: {len(result.generated_candidates)}; search bound: {result.search_bound}",
    ]
    rejected_ids = {item.name: item.rejection_reasons for item in result.rejected_candidates}
    for candidate in result.generated_candidates:
        cost = candidate.cost
        status = "selected" if candidate.identity == result.selected_candidate.identity else (
            "rejected" if candidate.name in rejected_ids else "legal"
        )
        graph = candidate.graph
        lines.append(
            f"candidate {candidate.name}: {status}; backend={graph.realization_backend}; "
            f"timing={graph.latency_knowledge}; latency={graph.latency}; "
            f"ii={graph.initiation_interval}; lut={cost.lut.value}; ff={cost.ff.value}; "
            f"dsp={cost.dsp.value}; bram={cost.bram.value}; fmax={cost.fmax_est.value}; "
            f"evidence={cost.fmax_est.source.value}"
        )
        if candidate.name in rejected_ids:
            lines.append("  rejected: " + "; ".join(rejected_ids[candidate.name]))
    for candidate in result.rejected_candidates:
        if candidate.name not in {item.name for item in result.generated_candidates}:
            lines.append(
                f"candidate {candidate.name}: rejected; " + "; ".join(candidate.rejection_reasons)
            )
    selected = result.selected_candidate
    graph = selected.graph
    lines.extend((
        f"selected: {selected.name}",
        f"architecture: {graph.architecture_template_identity}",
        f"pipeline configuration: {graph.pipeline_configuration_identity or 'generic'}",
        "pipeline sites: " + (", ".join(graph.active_pipeline_sites) or "none"),
        f"resources: dsp={selected.cost.dsp.value} lut={selected.cost.lut.value} ff={selected.cost.ff.value}",
        f"latency={graph.latency}; ii={graph.initiation_interval}",
        f"fmax={selected.cost.fmax_est.value} MHz ({selected.cost.fmax_est.source.value})",
        f"alignment delays: {len(graph.timing_dag.alignment_delays) if graph.timing_dag else 0}",
        f"compensation cycles: {sum(item.cycles for item in graph.timing_dag.compensation_delays) if graph.timing_dag else 0}",
        "physical bindings: " + (", ".join(graph.physical_binding_identities) or "generic"),
        "reason: " + (result.extraction.reason if result.extraction else "generic fallback"),
    ))
    return "\n".join(lines) + "\n"


__all__ = [
    "MeasurementKey", "QoREvidence", "TargetCandidate", "TargetPlanningResult",
    "load_qor_evidence", "plan_target_pipeline", "render_target_planner_report",
]
