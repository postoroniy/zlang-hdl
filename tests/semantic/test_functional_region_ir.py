from __future__ import annotations

from dataclasses import replace

import pytest

from zlang.ir.expressions import (
    Add,
    Call,
    Constant,
    FunctionalCaptureRef,
    FunctionalRegion,
    FunctionalTableLookup,
    InputRef,
    ParameterRef,
    ReadyValidRef,
    Reduce,
    ReductionOperator,
    RegisterRef,
    ValueRange,
    VectorIndex,
)
from zlang.ir.interfaces import ReadyValidSignal
from zlang.ir.functional import compact_functional_elements
from zlang.ir.functional_regions import (
    CompileTimeBinderRef,
    CompileTimeExpr,
    CompileTimeOperator,
    ExactReductionCombine,
    ExactReductionLevel,
    ExactReductionOperation,
    ExactReductionPlan,
    FunctionalRegionKind,
    FunctionalTable,
    build_exact_reduction_plan,
    compile_time_range,
    evaluate_compile_time,
)
from zlang.ir.module import (
    Assignment,
    Function,
    FunctionParameter,
    Module,
    Port,
    PortDirection,
)
from zlang.ir.types import UIntType, VecType
from zlang.compiler import compile_source
from zlang import exploration as exploration_module
from zlang.opt.ir import ExpressionOp
from zlang.opt.lowering import (
    CanonicalizationError,
    lower,
    lower_expression_graph,
    restore,
    restore_expression,
)
from zlang.simulate import simulate


U8 = UIntType(8)
U9 = UIntType(9)
U10 = UIntType(10)


def _table_region(values: tuple[int, ...], type_=U8) -> FunctionalRegion:
    binder = CompileTimeBinderRef("fixture:binder", "k", 0, len(values))
    table = FunctionalTable(
        "fixture:values",
        tuple(Constant(value, type_) for value in values),
        type_,
    )
    return FunctionalRegion(
        FunctionalRegionKind.GENERATE,
        binder,
        FunctionalTableLookup(
            table.name,
            CompileTimeExpr.ref(binder),
            type_,
        ),
        (table,),
        (),
        VecType(len(values), type_),
    )


def _widen(left, right) -> ExactReductionCombine:
    return ExactReductionCombine(UIntType(max(left.width, right.width) + 1))


def test_compile_time_binder_expression_has_stable_bounded_integer_semantics() -> None:
    binder = CompileTimeBinderRef("fixture:k", "k", 2, 6)
    expression = CompileTimeExpr(
        CompileTimeOperator.ADD,
        (
            CompileTimeExpr(
                CompileTimeOperator.MULTIPLY,
                (CompileTimeExpr.ref(binder), 3),
            ),
            1,
        ),
    )
    assert compile_time_range(expression) == (7, 16)
    assert evaluate_compile_time(expression, {binder.identity: 4}) == 13
    with pytest.raises(ValueError, match="escaped domain"):
        evaluate_compile_time(expression, {binder.identity: 6})


def test_functional_region_round_trips_and_reduces_lazily() -> None:
    vector_type = VecType(3, U8)
    binder = CompileTimeBinderRef("fixture:index", "index", 0, 3)
    capture = FunctionalCaptureRef("fixture:capture", "values", vector_type)
    bias = FunctionalTable(
        "fixture:bias",
        (Constant(1, U8), Constant(2, U8), Constant(3, U8)),
        U8,
    )
    template = Add(
        VectorIndex(capture, CompileTimeExpr.ref(binder), U8),
        FunctionalTableLookup(bias.name, CompileTimeExpr.ref(binder), U8),
        U9,
    )
    region = FunctionalRegion(
        FunctionalRegionKind.GENERATE,
        binder,
        template,
        (bias,),
        ((capture, InputRef("values", vector_type)),),
        VecType(3, U9),
    )
    plan = build_exact_reduction_plan(U9, 3, _widen)
    reduction = Reduce(
        ReductionOperator.ADD,
        region,
        plan.root_type,
        plan=plan,
    )
    input_port = Port(PortDirection.INPUT, "values", vector_type)
    output_port = Port(PortDirection.OUTPUT, "result", plan.root_type)
    module = Module(
        "FunctionalRegionFixture",
        (input_port, output_port),
        (Assignment(output_port, reduction),),
    )
    canonical = lower(module)
    assert restore(canonical) == module
    assert any(
        node.op is ExpressionOp.FUNCTIONAL_REGION
        for node in canonical.expressions
    )
    assert simulate(module, values=[10, 20, 30]) == {"result": 66}


def test_compacted_constant_table_retains_exact_value_range() -> None:
    binder = CompileTimeBinderRef("fixture:range", "i", 0, 3)
    compacted = compact_functional_elements(
        FunctionalRegionKind.GENERATE,
        binder,
        (Constant(1, U8), Constant(7, U8), Constant(3, U8)),
        VecType(3, U8),
    )
    assert compacted is not None
    region, inlined = compacted
    assert inlined == ()
    assert isinstance(region.template, FunctionalTableLookup)
    assert region.template.value_range == ValueRange(
        1, 7, "constant_table"
    )
    nodes, root = lower_expression_graph(Module("Fixture", (), ()), region)
    assert restore_expression(nodes, root) == region


@pytest.mark.parametrize(
    ("attribute_name", "value", "message"),
    (
        ("range_maximum", None, "incomplete value range"),
        ("range_maximum", 256, "outside its result type"),
        ("range_provenance", "", "invalid value range"),
    ),
)
def test_functional_table_lookup_rejects_malformed_canonical_ranges(
    attribute_name: str, value: object, message: str,
) -> None:
    binder = CompileTimeBinderRef("fixture:malformed-range", "i", 0, 2)
    lookup = FunctionalTableLookup(
        "fixture:table",
        CompileTimeExpr.ref(binder),
        U8,
        ValueRange(1, 7, "constant_table"),
    )
    nodes, root = lower_expression_graph(Module("Fixture", (), ()), lookup)
    attributes = tuple(
        (name, value if name == attribute_name else current)
        for name, current in nodes[root].attributes
    )
    malformed = (*nodes[:root], replace(nodes[root], attributes=attributes))
    with pytest.raises(CanonicalizationError, match=message):
        restore_expression(malformed, root)


def test_functional_region_kind_is_explicit_in_canonical_identity() -> None:
    generated = _table_region((4, 5))
    mapped = replace(generated, kind=FunctionalRegionKind.MAP)
    output = Port(PortDirection.OUTPUT, "result", mapped.type)
    module = Module(
        "MappedRegionFixture",
        (output,),
        (Assignment(output, mapped),),
    )
    canonical = lower(module)
    region = next(
        node
        for node in canonical.expressions
        if node.op is ExpressionOp.FUNCTIONAL_REGION
    )
    assert region.attribute("kind") is FunctionalRegionKind.MAP
    assert restore(canonical) == module
    assert simulate(module) == {"result": [4, 5]}


def test_exact_reduction_plan_preserves_midpoint_tree_for_odd_lengths() -> None:
    three = build_exact_reduction_plan(U8, 3, _widen)
    assert tuple(
        (
            tuple((item.left_index, item.right_index) for item in level.operations),
            level.carry_indices,
        )
        for level in three.levels
    ) == ((((1, 2),), (0,)), (((0, 1),), ()))

    five = build_exact_reduction_plan(U8, 5, _widen)
    assert tuple(
        (
            tuple((item.left_index, item.right_index) for item in level.operations),
            level.carry_indices,
        )
        for level in five.levels
    ) == (
        (((0, 1), (3, 4)), (2,)),
        (((1, 2),), (0,)),
        (((0, 1),), ()),
    )


def test_exact_reduction_plan_rejects_left_association_for_three_leaves() -> None:
    first = ExactReductionLevel(
        (U8, U8, U8),
        (ExactReductionOperation(0, 1, U8, U8, U9),),
        (2,),
    )
    second = ExactReductionLevel(
        (U9, U8),
        (ExactReductionOperation(0, 1, U9, U8, U10),),
    )
    with pytest.raises(ValueError, match="recursive midpoint topology"):
        ExactReductionPlan(3, U8, U10, (first, second))


def test_builtin_exact_reduction_rejects_narrowed_intermediate_type() -> None:
    with pytest.raises(
        ValueError,
        match="built-in exact reduction result type must be u9, got u8",
    ):
        ExactReductionOperation(0, 1, U8, U8, U8)


def test_nominal_exact_plan_validates_callable_identity_and_simulates() -> None:
    add8 = Function(
        "add8",
        (FunctionParameter("left", U8), FunctionParameter("right", U8)),
        U9,
        Add(ParameterRef("left", U8), ParameterRef("right", U8), U9),
    )
    add89 = Function(
        "add89",
        (FunctionParameter("left", U8), FunctionParameter("right", U9)),
        U10,
        Add(ParameterRef("left", U8), ParameterRef("right", U9), U10),
    )
    definitions = {(U8, U8): add8, (U8, U9): add89}

    def resolve(left, right) -> ExactReductionCombine:
        function = definitions[(left, right)]
        return ExactReductionCombine(
            function.return_type,
            function.name,
            function.callee_identity,
        )

    plan = build_exact_reduction_plan(U8, 3, resolve)
    reduction = Reduce(
        ReductionOperator.ADD,
        _table_region((1, 2, 3)),
        U10,
        plan=plan,
    )
    output = Port(PortDirection.OUTPUT, "result", U10)
    module = Module(
        "NominalPlanFixture",
        (output,),
        (Assignment(output, reduction),),
        functions=(add8, add89),
    )
    canonical = lower(module)
    assert restore(canonical) == module
    assert simulate(module) == {"result": 6}

    reduce_index = next(
        index
        for index, node in enumerate(canonical.expressions)
        if node.op is ExpressionOp.REDUCE
    )
    node = canonical.expressions[reduce_index]
    bad_first_level = replace(
        plan.levels[0],
        operations=(
            replace(plan.levels[0].operations[0], callee_identity="missing"),
        ),
    )
    bad_plan = replace(plan, levels=(bad_first_level, *plan.levels[1:]))
    with pytest.raises(ValueError, match="invalid callable"):
        Module(
            "BadNominalPlanFixture",
            (output,),
            (
                Assignment(
                    output,
                    replace(reduction, plan=bad_plan),
                ),
            ),
            functions=(add8, add89),
        )
    bad_node = replace(
        node,
        attributes=tuple(
            (name, bad_plan) if name == "plan" else (name, value)
            for name, value in node.attributes
        ),
    )
    with pytest.raises(ValueError, match="invalid exact callable"):
        replace(
            canonical,
            expressions=(
                *canonical.expressions[:reduce_index],
                bad_node,
                *canonical.expressions[reduce_index + 1 :],
            ),
        )


def test_compactor_builds_one_template_table_and_capture() -> None:
    vector_type = VecType(3, U8)
    binder = CompileTimeBinderRef("fixture:compact", "k", 0, 3)
    combine = Function(
        "combine",
        (FunctionParameter("left", U8), FunctionParameter("right", U8)),
        U9,
        Add(ParameterRef("left", U8), ParameterRef("right", U8), U9),
    )
    constant_zero = Function("constant_zero", (), U8, Constant(0, U8))
    constant_two = Function("constant_two", (), U8, Constant(2, U8))
    constants = (constant_zero, constant_two)
    selected_constants = (
        constant_zero,
        constant_zero,
        constant_two,
    )
    elements = tuple(
        Call(
            combine.name,
            (
                VectorIndex(InputRef("values", vector_type), index, U8),
                Call(
                    constant.name,
                    (),
                    U8,
                    constant.callee_identity,
                ),
            ),
            U9,
            combine.callee_identity,
        )
        for index, constant in enumerate(selected_constants)
    )
    compacted = compact_functional_elements(
        FunctionalRegionKind.GENERATE,
        binder,
        elements,
        VecType(3, U9),
        (combine, *constants),
    )
    assert compacted is not None
    region, inlined = compacted
    assert len(region.tables) == 1
    assert len(region.captures) == 1
    assert inlined == tuple(
        function.callee_identity for function in selected_constants
    )
    input_port = Port(PortDirection.INPUT, "values", vector_type)
    output_port = Port(PortDirection.OUTPUT, "result", region.type)
    module = Module(
        "CompactedFixture",
        (input_port, output_port),
        (Assignment(output_port, region),),
        functions=(combine, *constants),
    )
    assert restore(lower(module)) == module
    assert simulate(module, values=[10, 20, 30]) == {
        "result": [10, 20, 32]
    }


def test_functional_region_rejects_effectful_template() -> None:
    binder = CompileTimeBinderRef("fixture:effect", "k", 0, 2)
    with pytest.raises(ValueError, match="pure combinational"):
        FunctionalRegion(
            FunctionalRegionKind.GENERATE,
            binder,
            RegisterRef("state", U8),
            (),
            (),
            VecType(2, U8),
        )


def test_semantic_compaction_captures_ready_valid_payload_only() -> None:
    module = compile_source(
        "module Top { "
        "in input : rv<vec<64,u8>> "
        "out values : vec<64,u9> "
        "input.ready = 1 "
        "values = generate(i in 0..64) { input.payload[i] + 1 } "
        "}",
    ).ir

    region = module.assignments[1].expression
    assert isinstance(region, FunctionalRegion)
    assert len(region.captures) == 1
    reference, captured = region.captures[0]
    assert reference.type == VecType(64, U8)
    assert isinstance(captured, ReadyValidRef)
    assert captured.signal is ReadyValidSignal.PAYLOAD


@pytest.mark.parametrize("signal", ("valid", "transfer"))
def test_semantic_compaction_keeps_ready_valid_control_outside_regions(
    signal: str,
) -> None:
    module = compile_source(
        "module Top { "
        "in input : rv<u8> "
        "out values : vec<32,bit> "
        "input.ready = 1 "
        f"values = generate(i in 0..32) input.{signal} "
        "}",
    ).ir

    assert not isinstance(module.assignments[1].expression, FunctionalRegion)


def test_functional_region_rejects_foreign_compile_time_binder() -> None:
    owner = CompileTimeBinderRef("fixture:owner", "k", 0, 2)
    escaped = CompileTimeBinderRef("fixture:escaped", "j", 0, 2)
    table = FunctionalTable(
        "fixture:foreign-table",
        (Constant(1, U8), Constant(2, U8)),
        U8,
    )
    with pytest.raises(ValueError, match="foreign or escaped"):
        FunctionalRegion(
            FunctionalRegionKind.GENERATE,
            owner,
            FunctionalTableLookup(
                table.name,
                CompileTimeExpr.ref(escaped),
                U8,
            ),
            (table,),
            (),
            VecType(2, U8),
        )


def test_compactor_rejects_binder_dependent_hardware_result_type() -> None:
    binder = CompileTimeBinderRef("fixture:dependent", "k", 0, 2)
    compacted = compact_functional_elements(
        FunctionalRegionKind.GENERATE,
        binder,
        (Constant(1, U8), Constant(2, U9)),
        VecType(2, U8),
    )
    assert compacted is None


def test_semantic_compaction_keeps_untracked_ordinary_helper_definition() -> None:
    module = compile_source(
        "fn zero()->u8{0} module Top{out y:vec<32,u8> "
        "y=generate(i in 0..32) zero()}",
    ).ir
    assert isinstance(module.assignments[0].expression, FunctionalRegion)
    assert tuple(function.name for function in module.functions) == ("zero",)


def test_semantic_compaction_prunes_only_unreachable_generic_call_graph() -> None:
    module = compile_source(
        "fn inner<K>()->u8{K} fn outer<K>()->u8{inner<K=K>()} "
        "module Top{out y:vec<32,u8> "
        "y=generate(i in 0..32) outer<K=i>()}",
    ).ir
    assert isinstance(module.assignments[0].expression, FunctionalRegion)
    assert module.callable_definitions == ()
    assert module.generic_specializations == ()


def test_semantic_compaction_retains_specialization_used_outside_region() -> None:
    module = compile_source(
        "fn value<K>()->u8{K} module Top{out scalar:u8 out values:vec<32,u8> "
        "scalar=value<K=7>() "
        "values=generate(i in 0..32) value<K=7>()}",
    ).ir
    assert isinstance(module.assignments[1].expression, FunctionalRegion)
    assert len(module.callable_definitions) == 1
    assert len(module.generic_specializations) == 1
    assert isinstance(module.assignments[0].expression, Call)
    assert (
        module.assignments[0].expression.callee_identity
        == module.callable_definitions[0].callee_identity
    )


def test_exploration_reads_compact_region_dependencies_without_materializing() -> None:
    binder = CompileTimeBinderRef("fixture:explore", "i", 0, 32)
    capture = FunctionalCaptureRef("fixture:input", "x", U8)
    table = FunctionalTable(
        "fixture:constants",
        tuple(Constant(index, U8) for index in range(32)),
        U8,
    )
    region = FunctionalRegion(
        FunctionalRegionKind.GENERATE,
        binder,
        Add(
            capture,
            FunctionalTableLookup(
                table.name,
                CompileTimeExpr.ref(binder),
                U8,
            ),
            U9,
        ),
        (table,),
        ((capture, InputRef("x", U8)),),
        VecType(32, U9),
    )

    assert set(exploration_module._input_refs(region)) == {"x"}
    assert exploration_module._logic_depth(region) == 1
