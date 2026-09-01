from pathlib import Path

from zlang.compiler import compile_source
from zlang.ir import (
    FixedConvert,
    ProductTermSign,
    RuntimeIndex,
    Truncate,
    RegisterRef,
    recognize_signed_product_reduction,
)
from zlang.parser import parse
from zlang.semantic import analyze


ROOT = Path(__file__).resolve().parents[2]


def test_exact_complex_real_pipeline_auto_keeps_generic_fallback_and_physical_candidates() -> None:
    source = (
        ROOT / "examples" / "fft" / "complex_multiply_pipeline_auto.zl"
    ).read_text()
    result = compile_source(
        source, top="FFTComplexMultiplyRealAuto", target="xc7z030ffg676-1"
    )
    exploration = result.ir.pipeline_explorations[0]
    assert isinstance(exploration.source_expression, FixedConvert)
    reduction = recognize_signed_product_reduction(
        exploration.source_expression.expression
    )
    assert reduction is not None
    assert tuple(term.sign for term in reduction.terms) == (
        ProductTermSign.ADD, ProductTermSign.SUBTRACT,
    )
    physical = tuple(
        candidate for candidate in result.target_planning_result.generated_candidates
        if "SignedProduct" in candidate.name and not candidate.graph.is_generic
    )
    assert len(physical) == 4
    assert result.implementation_graph.is_generic
    assert result.implementation_graph.timing_dag.output_latency == 1
    rejected = tuple(
        candidate for candidate in result.target_planning_result.rejected_candidates
        if "SignedProduct" in candidate.name
    )
    assert all(
        any("fmax_est requirement cannot be proven" in reason
            for reason in candidate.rejection_reasons)
        for candidate in rejected
    )


def test_reusable_sdf_delay_depth_parameter_elaborates() -> None:
    source = (
        ROOT / "tests" / "fixtures" / "fft" /
        "parameterized_fifo_depth.zl"
    ).read_text()
    module = analyze(parse(source))
    assert module.fifos[0].depth == 4


def test_sdf_delay_literal_and_parameter_have_identical_concrete_storage() -> None:
    source = (
        ROOT / "tests" / "fixtures" / "fft" /
        "parameterized_fifo_depth.zl"
    ).read_text()
    literal_fixture = source.replace("fifo<u8,D>", "fifo<u8,4>")
    parameterized = analyze(parse(source))
    literal = analyze(parse(literal_fixture))
    assert parameterized.fifos == literal.fifos


def test_reusable_sdf_module_type_and_depth_parameters_elaborate() -> None:
    source = (
        ROOT / "tests" / "fixtures" / "fft" /
        "module_type_specialization.zl"
    ).read_text()
    syntax = parse(source)
    assert syntax.instances[0].arguments[0].name == "Sample"
    module = analyze(syntax)
    child = module.children[0]
    assert str(child.inputs[0].type) == "u8"
    assert str(child.outputs[0].type) == "u8"
    assert child.fifos[0].depth == 4
    assert str(child.fifos[0].element_type) == "u8"


def test_reusable_sdf_phase_state_and_fifo_use_unified_transition() -> None:
    source = (
        ROOT / "tests" / "fixtures" / "fft" /
        "unified_state_storage.zl"
    ).read_text()
    syntax = parse(source)
    stage = syntax.submodules[0]
    assert tuple(parameter.name for parameter in stage.parameters) == (
        "Sample", "Twiddle", "D",
    )
    module = analyze(syntax)
    assert module.fifos[0].scheduled
    assert len(module.rules) == 2
    assert module.resolved_transition is not None
    kinds = {
        action.kind.value
        for group in module.resolved_transition.action_groups
        for action in group.actions
    }
    assert kinds == {"register_write", "fifo_push", "fifo_pop"}


def test_reusable_sdf_value_parameter_expression_is_accepted() -> None:
    source = (
        ROOT / "tests" / "fixtures" / "fft" /
        "value_parameter_expression.zl"
    ).read_text()
    syntax = parse(source)
    assert tuple(parameter.name for parameter in syntax.parameters) == ("D", "STEP")
    module = analyze(syntax)
    assert module.assignments[0].expression.right.value == 4
    assert module.assignments[1].expression.condition.right.value == 7
    assert module.assignments[2].expression.right.value == 2


def test_numerical_sdf_stage_register_derived_twiddle_index_is_accepted() -> None:
    source = (
        ROOT / "tests" / "fixtures" / "fft" /
        "runtime_twiddle_index.zl"
    ).read_text()
    syntax = parse(source)
    stage = syntax.submodules[0]
    assert tuple(parameter.name for parameter in stage.parameters) == (
        "Twiddle", "D", "CW", "IW",
    )
    module = analyze(syntax)
    lookup = module.children[0]
    access = lookup.assignments[0].expression
    assert isinstance(access, RuntimeIndex)
    assert isinstance(access.index, Truncate)
    assert isinstance(access.index.expression, RegisterRef)
    assert access.vector_length == 4
    assert (access.index_range.minimum, access.index_range.maximum) == (0, 3)
    assert access.index_range.provenance == "truncate"
