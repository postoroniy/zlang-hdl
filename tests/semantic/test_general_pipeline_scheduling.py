from __future__ import annotations

from dataclasses import replace

import pytest

from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir.pipelines import PipelineCostSource, PipelinePlan
from zlang.ir.traversal import walk_expression
from zlang.ir.types import UIntType
from zlang.opt import CanonicalizationError, lower, restore
from zlang.opt.ir import ExpressionOp
from zlang.pipeline_scheduling import (
    TargetResourceOperationCostModel,
    erase_pipeline_timing,
)
from zlang.semantic import SemanticError
from zlang.simulate import simulate_cycles
from zlang.timing import timing_info
from zlang.targets import load_target


GENERAL = """
module GeneralExpressionPipeline {
  clock clk reset rst
  in a,b,c,d,e:u8
  in f:u25
  out y:u26
  y=pipeline(3){(a*b+c*d)*e+f}
}
"""


def _scheduled(source: str = GENERAL) -> tuple[object, expr.Pipeline, PipelinePlan]:
    module = compile_source(source, include_clash=False).ir
    value = module.assignments[0].expression
    assert isinstance(value, expr.Pipeline)
    assert value.pipeline_plan is not None
    return module, value, value.pipeline_plan


def test_general_dag_is_partitioned_into_exact_visible_latency() -> None:
    _, value, plan = _scheduled()
    assert timing_info(value).latency == 3
    assert plan.requested_latency == 3
    assert plan.initiation_interval == 1
    assert plan.scheduler == "dag_partition_v1"
    assert plan.cost_source is PipelineCostSource.STRUCTURAL_ESTIMATE
    assert plan.timed_equivalence == "verified"
    assert [[
        next(op.operation for op in plan.operations if op.identity == identity)
        for identity in stage.operation_identities
    ] for stage in plan.stages] == [
        ["multiply", "multiply", "add"],
        ["multiply"],
        ["add"],
    ]
    assert max(stage.estimated_delay_ps for stage in plan.stages) < sum(
        operation.cost.delay_ps for operation in plan.operations
    )
    assert erase_pipeline_timing(value).type == value.type


@pytest.mark.parametrize("latency", (1, 2, 3, 5))
def test_pipeline_contract_is_exact_for_every_requested_latency(latency: int) -> None:
    source = GENERAL.replace("pipeline(3)", f"pipeline({latency})")
    _, value, plan = _scheduled(source)
    assert timing_info(value).latency == latency
    assert plan.requested_latency == latency
    assert len(plan.stages) == latency


def test_reconvergent_short_path_receives_exact_balancing_delay() -> None:
    _, value, plan = _scheduled(
        "module R { clock clk reset rst in a,b:u8 out y:u17 "
        "y=pipeline(3){a*b+a} }"
    )
    assert timing_info(value).latency == 3
    assert any(
        delay.cycles == 1 and delay.width == 8
        for delay in plan.timing_dag.alignment_delays
    )
    operations = tuple(item.operation for item in plan.operations)
    assert operations == ("multiply", "add")


def test_common_subexpression_is_scheduled_once() -> None:
    _, _, plan = _scheduled(
        "module Shared { clock clk reset rst in a,b,c,d:u8 out y:u34 "
        "t=a*b y=pipeline(3){(t+c)*(t+d)} }"
    )
    # One shared a*b, two adds, and the final multiply.
    assert tuple(item.operation for item in plan.operations).count("multiply") == 2
    assert len({item.identity for item in plan.operations}) == 4


def test_cycle_simulation_preserves_values_and_latency() -> None:
    module, _, _ = _scheduled()
    samples = [
        {"a": i + 1, "b": 2, "c": i + 3, "d": 4, "e": 5, "f": i}
        for i in range(6)
    ]
    outputs = simulate_cycles(module, samples)
    expected = [((s["a"] * s["b"] + s["c"] * s["d"]) * s["e"] + s["f"])
                for s in samples]
    assert [item["y"] for item in outputs] == [0, 0, 0, *expected[:3]]


def test_signed_and_fixed_conversion_boundaries_remain_exact() -> None:
    signed = compile_source(
        "module Signed { clock clk reset rst in a,b:s8 in c:s16 "
        "out y:s17 y=pipeline(2){a*b+c} }",
        include_clash=False,
    ).ir.assignments[0].expression
    assert isinstance(signed, expr.Pipeline)
    assert str(signed.type) == "s17"
    assert erase_pipeline_timing(signed).type == signed.type

    fixed = compile_source(
        "module Fixed { clock clk reset rst in a,b,c,d:SF8.8 "
        "out y:SF_Sat8.8 y=pipeline(3){"
        "quantize<SF_Sat8.8>(a*b+c*d){round nearest_even overflow saturate}"
        "} }",
        include_clash=False,
    ).ir.assignments[0].expression
    assert isinstance(fixed, expr.Pipeline)
    assert fixed.pipeline_plan is not None
    assert tuple(item.operation for item in fixed.pipeline_plan.operations).count(
        "fixed_convert"
    ) == 1
    exact = erase_pipeline_timing(fixed)
    # The conversion remains the root of the value expression: no boundary was
    # moved through it and no intermediate quantization was introduced.
    assert isinstance(exact, expr.FixedConvert)
    assert exact.rounding is expr.FixedRounding.NEAREST_EVEN
    assert exact.overflow is expr.FixedOverflow.SATURATE


def test_supported_scalar_selection_bit_and_packing_nodes_are_scheduled() -> None:
    module = compile_source(
        "module Mixed { clock clk reset rst in a,b,c:u8 in select:bit "
        "out arithmetic:u8 out packed:bits<8> "
        "arithmetic=pipeline(3){select ? (a << 1) : (b ^ c)} "
        "packed=pipeline(2){concat(a[7:4],b[3:0])} }",
        include_clash=False,
    ).ir
    operation_sets = tuple(
        {
            operation.operation
            for operation in assignment.expression.pipeline_plan.operations
        }
        for assignment in module.assignments
    )
    assert {"shift", "bitwise", "mux"} <= operation_sets[0]
    assert {"slice", "concat"} <= operation_sets[1]
    assert tuple(timing_info(item.expression).latency for item in module.assignments) == (
        3,
        2,
    )


def test_pure_callable_is_expanded_before_dag_scheduling() -> None:
    module, _, plan = _scheduled(
        "fn mixed(a:u8,b:u8,c:u8,d:u8,e:u8,f:u25) { "
        "(a*b+c*d)*e+f } "
        "module Callable { clock clk reset rst in a,b,c,d,e:u8 in f:u25 "
        "out y:u26 y=pipeline(3){mixed(a,b,c,d,e,f)} }"
    )
    assert tuple(item.operation for item in plan.operations) == (
        "multiply",
        "multiply",
        "add",
        "multiply",
        "add",
    )
    assert not any(
        isinstance(item, expr.Call)
        for item in walk_expression(module.assignments[0].expression)
    )


def test_exact_typed_egraph_alternative_is_selected_before_partitioning() -> None:
    module, value, plan = _scheduled(
        "module Shifted { clock clk reset rst in a:u8 out y:u16 "
        "y=pipeline(2){a*8} }"
    )
    assert plan.source_expression is not None
    assert isinstance(plan.source_expression, expr.Binary)
    assert plan.source_expression.operator is expr.BinaryOperator.MULTIPLY
    assert plan.selected_value_identity != plan.source_expression_identity
    assert plan.rewrite_certificate[0] == "egglog_exact_typed"
    assert any(
        item.startswith("rewrite=multiply_power_of_two")
        for item in plan.rewrite_certificate
    )
    assert tuple(item.operation for item in plan.operations) == ("extend", "shift")
    assert erase_pipeline_timing(value).type == plan.source_expression.type
    assert [item["y"] for item in simulate_cycles(
        module,
        ({"a": 3}, {"a": 17}, {"a": 255}),
    )] == [0, 0, 24]
    restored = restore(lower(module))
    restored_plan = restored.assignments[0].expression.pipeline_plan
    assert restored_plan is not None
    assert restored_plan.source_expression_identity == plan.source_expression_identity
    assert restored_plan.rewrite_certificate == plan.rewrite_certificate


def test_schedule_and_canonical_identity_are_deterministic() -> None:
    first, first_value, _ = _scheduled()
    second, second_value, _ = _scheduled()
    assert first_value == second_value
    assert lower(first) == lower(second)
    assert restore(lower(first)) == first


def test_canonical_restore_rejects_invalid_schedule_metadata() -> None:
    module, _, _ = _scheduled()
    canonical = lower(module)
    index = next(
        i for i, node in enumerate(canonical.expressions)
        if node.op is ExpressionOp.PIPELINE
        and "pipeline_plan" in dict(node.attributes)
    )
    node = canonical.expressions[index]
    attributes = tuple(
        (name, "corrupted" if name == "pipeline_plan" else value)
        for name, value in node.attributes
    )
    broken = replace(
        canonical,
        expressions=canonical.expressions[:index]
        + (replace(node, attributes=attributes),)
        + canonical.expressions[index + 1:],
    )
    with pytest.raises(CanonicalizationError, match="invalid schedule metadata"):
        restore(broken)


def test_canonical_restore_rejects_schedule_expression_identity_mismatch() -> None:
    module, _, _ = _scheduled()
    canonical = lower(module)
    index = next(
        i for i, node in enumerate(canonical.expressions)
        if node.op is ExpressionOp.PIPELINE
        and "pipeline_plan" in dict(node.attributes)
    )
    node = canonical.expressions[index]
    plan = dict(node.attributes)["pipeline_plan"]
    attributes = tuple(
        (
            name,
            replace(plan, scheduled_expression_identity="0" * 64)
            if name == "pipeline_plan" else value,
        )
        for name, value in node.attributes
    )
    broken = replace(
        canonical,
        expressions=canonical.expressions[:index]
        + (replace(node, attributes=attributes),)
        + canonical.expressions[index + 1:],
    )
    with pytest.raises(CanonicalizationError, match="schedule identity disagrees"):
        restore(broken)


def test_shared_scheduled_graph_rejects_inconsistent_dependency_and_binding() -> None:
    _, _, plan = _scheduled()
    graph = plan.scheduled_value_graph
    assert graph is not None
    source, destination, cycles = next(
        item for item in graph.dependencies
        if item[0] in graph.operation_identities
        and dict(graph.stage_assignment)[item[0]]
        < dict(graph.stage_assignment)[item[1]]
    )
    with pytest.raises(ValueError, match="latency disagrees"):
        replace(
            graph,
            dependencies=tuple(
                (a, b, value + 1) if (a, b, value) == (source, destination, cycles)
                else (a, b, value)
                for a, b, value in graph.dependencies
            ),
        )

    from zlang.ir.scheduled import ScheduledValueResourceBinding

    binding = ScheduledValueResourceBinding(
        graph.operation_identities[0], "dsp0", "std.target.xilinx7.DSP48E1"
    )
    with pytest.raises(ValueError, match="more than one resource binding"):
        replace(graph, resource_bindings=(binding, replace(binding, resource_instance_identity="dsp1")))


def test_shared_scheduled_graph_rejects_same_stage_operation_cycle() -> None:
    _, _, plan = _scheduled()
    graph = plan.scheduled_value_graph
    assert graph is not None
    stages = dict(graph.stage_assignment)
    first, second = next(
        (left, right)
        for left in graph.operation_identities
        for right in graph.operation_identities
        if left != right and stages[left] == stages[right]
    )
    dependencies = tuple(
        item for item in graph.dependencies
        if not (item[0] == first and item[1] == second)
        and not (item[0] == second and item[1] == first)
    ) + ((first, second, 0), (second, first, 0))
    with pytest.raises(ValueError, match="operation cycle"):
        replace(graph, dependencies=dependencies)


def test_target_cost_model_counts_unsigned_physical_sign_bit() -> None:
    _, _, resources = load_target("xc7z030ffg676-1")
    model = TargetResourceOperationCostModel(resources)
    left = expr.InputRef("a", UIntType(25))
    right = expr.InputRef("b", UIntType(8))
    product = expr.Binary(
        expr.BinaryOperator.MULTIPLY,
        left,
        right,
        left.type,
        UIntType(33),
    )
    cost = model.cost(product, "multiply")
    assert cost.resource_class == "logic_multiplier"
    assert cost.dsp == 0


def test_stateful_or_nested_timing_nodes_fail_closed() -> None:
    source = (
        "module Bad { clock clk reset rst in a:u8 out y:u8 "
        "y=pipeline(2){delay<1>(a)} }"
    )
    with pytest.raises(SemanticError, match="nested delay/pipeline"):
        compile_source(source, include_clash=False)
