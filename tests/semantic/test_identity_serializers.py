"""Regression checks for value, physical and historical evidence identities."""

from __future__ import annotations

from dataclasses import replace

from zlang.ir import expressions as expr
from zlang.ir.pipelines import PipelineCostSource, PipelinePlan
from zlang.ir.scheduled import ScheduledValueGraph
from zlang.ir.signed_reductions import (
    expression_semantic_identity,
    selection_expression_semantic_identity,
)
from zlang.ir.target import ImplementationDelay, ImplementationGraph, TimingDAG
from zlang.ir.types import FixedType, UIntType
from zlang.costs import MetricSource
from zlang.compiler import compile_source
from zlang.compilation_session import CompilationSession
from zlang.formal_counterexample_codec import counterexample_to_data
from zlang.ir.equivalence import EquivalenceCounterexample
from zlang.ir.formal import Counterexample
from zlang.target_planner import (
    MeasurementKey,
    QoREvidence,
    _compatible_evidence,
)
from zlang.targets import generic_implementation_graph
from zlang.opt import lower


def test_counterexample_codec_preserves_exact_closed_union_shapes() -> None:
    assert counterexample_to_data(
        Counterexample("safety", 3, (("count", "4"),), "safety_verification trace")
    ) == {
        "kind": "safety_verification",
        "property_id": "safety",
        "cycle": 3,
        "values": [["count", "4"]],
        "raw_trace": "safety_verification trace",
    }
    assert counterexample_to_data(
        EquivalenceCounterexample(
            "equivalence", 5, 2, (("expected", "7"),), "semantic_equivalence trace"
        )
    ) == {
        "kind": "semantic_equivalence",
        "property_id": "equivalence",
        "failure_cycle": 5,
        "sample_cycle": 2,
        "values": [["expected", "7"]],
        "raw_trace": "semantic_equivalence trace",
    }
    assert counterexample_to_data(None) is None


def test_value_identity_ignores_pipeline_plan_and_cost_feedback() -> None:
    word = UIntType(8)
    input_ = expr.InputRef("a", word)
    plan = PipelinePlan(("boundary",), 1)
    pipeline = expr.Pipeline(1, input_, 1, word, pipeline_plan=plan)
    updated = replace(
        pipeline,
        pipeline_plan=replace(plan, cost_source=PipelineCostSource.MEASURED),
    )
    assert expression_semantic_identity(pipeline) == expression_semantic_identity(updated)

    applicability = expr.ImplementationApplicability(
        "multiply", (), word, word, word, word, expr.ImplementationResource.LOGIC
    )
    semantics = expr.ImplementationSemantics(word, 0, 1, ())
    alternative = expr.ImplementationAlternative(
        expr.ImplementationKind.MUL_ADD, input_, applicability, semantics,
        estimate=expr.ImplementationCostEstimate(1, 0, 0, 0, 0, 1),
    )
    choice = expr.ImplementationChoice(
        expr.ImplementationKind.MUL_ADD, (alternative,), (), word
    )
    changed = replace(
        choice,
        alternatives=(
            replace(alternative, estimate=replace(alternative.estimate, lut=2)),
        ),
    )
    assert expression_semantic_identity(choice) == expression_semantic_identity(changed)


def test_selection_identity_normalizes_nested_allocation_ids() -> None:
    word = UIntType(8)
    input_ = expr.InputRef("a", word)
    left = expr.Add(expr.Pipeline(1, input_, 1, word), input_, word)
    right = expr.Add(expr.Pipeline(1, input_, 2, word), input_, word)
    assert expression_semantic_identity(left) != expression_semantic_identity(right)
    assert selection_expression_semantic_identity(left) == (
        selection_expression_semantic_identity(right)
    )


def test_value_identity_is_independent_of_python_dag_sharing() -> None:
    word = UIntType(8)
    shared = expr.InputRef("a", word)
    shared_value = expr.Add(shared, shared, UIntType(9))
    duplicated_value = expr.Add(
        expr.InputRef("a", word),
        expr.InputRef("a", word),
        UIntType(9),
    )
    assert expression_semantic_identity(shared_value) == (
        expression_semantic_identity(duplicated_value)
    )


def test_frontend_identity_and_round_trip_are_bounded_by_unique_dag_nodes() -> None:
    rounds = 64
    source = "\n".join(
        [
            "module SharedDag {",
            "in seed:u32",
            "out result:u32",
            "state_00:u32=seed",
        ]
        + [
            f"state_{index:02d}:u32=truncate<32>((state_{index - 1:02d}^"
            f"(state_{index - 1:02d}>>7))+0x9e3779b9)"
            for index in range(1, rounds + 1)
        ]
        + [f"result=state_{rounds:02d}", "}"]
    )
    module = CompilationSession(source, top="SharedDag").planning.module
    canonical = lower(module)

    # Every round adds a bounded number of unique nodes even though its fully
    # expanded expression tree has more than 2**64 logical paths.
    assert len(canonical.expressions) < rounds * 8


def test_scheduled_physical_identity_ignores_estimate_and_provenance() -> None:
    graph = ScheduledValueGraph(
        "source", "selected", ("op",), (("op", "value"),), (),
        (("op", 0),), (100,), 1, 1, "structural_estimate",
    )
    changed = replace(
        graph, stage_delays_ps=(200,), cost_source="target_estimate",
        rewrite_certificate=("same_value",),
    )
    assert graph.identity == changed.identity
    assert graph.legacy_identity != changed.legacy_identity
    assert graph.identity != replace(graph, stage_assignment=(("op", 0),), clock_domain="clkB").identity


def test_implementation_physical_identity_ignores_timing_estimate() -> None:
    alignment = ImplementationDelay(
        "balance", "alignment", "source", "destination", 1, 8, 8,
    )
    timing = TimingDAG(
        (), (), (), alignment_delays=(alignment,), output_latency=1,
        estimated_critical_delay_ps=100,
    )
    graph = ImplementationGraph(
        "semantic", "generic", None, None, (), (), (), 1, 1,
        timing_dag=timing,
        legality_evidence=("old estimate",),
    )
    changed = replace(
        graph,
        timing_dag=replace(
            timing,
            alignment_delays=(replace(alignment, ff_cost=16),),
            estimated_critical_delay_ps=200,
        ),
        legality_evidence=("new estimate",),
    )
    assert graph.identity == changed.identity
    assert graph.legacy_identity != changed.legacy_identity


def test_historical_qor_key_matches_only_the_exact_old_graph() -> None:
    graph = ImplementationGraph(
        "semantic", "generic", "target", "target-hash", (), (), (), 1, 1,
        target_part="part",
        pipeline_configuration_identity="configuration",
    )
    key = MeasurementKey(
        "target", "part", "generic", graph.legacy_identity, "configuration",
        "direct_systemverilog", "Vivado", "2024.2", 5.0,
    )
    evidence = QoREvidence(
        key, MetricSource.SYNTHESIS_MEASUREMENT, 1, 1, 0, 0, 200.0,
    )
    options = {
        "backend": "direct_systemverilog", "tool": "Vivado",
        "tool_version": "2024.2", "clock_period_ns": 5.0,
    }
    assert _compatible_evidence((evidence,), graph, **options) is evidence
    assert _compatible_evidence(
        (evidence,), replace(graph, latency=2), **options
    ) is None


def test_fixed_quantization_participates_in_physical_and_legacy_qor_guard() -> None:
    accumulator = expr.InputRef("acc", FixedType(48, 16))
    conversion = expr.FixedConvert(
        accumulator, expr.FixedRounding.NEAREST_EVEN,
        expr.FixedOverflow.WRAP, expr.FixedConversionKind.RESCALE,
        FixedType(16, 8),
    )
    graph = ImplementationGraph(
        "accumulator", "fir", "target", "target-hash", (), (), (), 1, 1,
        target_part="part", pipeline_configuration_identity="configuration",
        quantization=conversion,
    )
    changed = replace(
        graph, quantization=replace(conversion, rounding=expr.FixedRounding.FLOOR)
    )
    assert graph.identity != changed.identity
    assert graph.legacy_identity == changed.legacy_identity

    key = MeasurementKey(
        "target", "part", "fir", graph.legacy_identity, "configuration",
        "direct_systemverilog", "Vivado", "2024.2", 5.0,
    )
    bare = QoREvidence(key, MetricSource.SYNTHESIS_MEASUREMENT, 1, 1, 0, 0, 200.0)
    guarded = replace(
        bare, quantization_identity=expression_semantic_identity(conversion)
    )
    options = {
        "backend": "direct_systemverilog", "tool": "Vivado",
        "tool_version": "2024.2", "clock_period_ns": 5.0,
    }
    assert _compatible_evidence((bare,), graph, **options) is None
    assert _compatible_evidence((guarded,), graph, **options) is guarded
    assert _compatible_evidence((guarded,), changed, **options) is None


def test_generic_module_identity_ignores_pipeline_cost_metadata() -> None:
    source = (
        "module M { clock clk reset rst in a,b,c:u8 out y:u17 "
        "y=pipeline(2){(a+b)*c} }"
    )
    module = compile_source(source).ir
    assignment = module.assignments[0]
    pipeline = assignment.expression
    assert isinstance(pipeline, expr.Pipeline)
    assert pipeline.pipeline_plan is not None
    changed = replace(
        module,
        assignments=(replace(
            assignment,
            expression=replace(
                pipeline,
                pipeline_plan=replace(
                    pipeline.pipeline_plan,
                    cost_source=PipelineCostSource.MEASURED,
                ),
            ),
        ),),
    )
    assert generic_implementation_graph(module).identity == (
        generic_implementation_graph(changed).identity
    )
    other = compile_source(source.replace("a+b", "a+c")).ir
    assert generic_implementation_graph(module).identity != (
        generic_implementation_graph(other).identity
    )
