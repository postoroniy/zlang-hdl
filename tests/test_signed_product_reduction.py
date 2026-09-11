from dataclasses import replace
from pathlib import Path

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_target_artifact
from zlang.compiler import compile_source
from zlang.ir import (
    Add,
    Binary,
    BinaryOperator,
    FixedConvert,
    FixedType,
    UFixedType,
    ProductTermSign,
    SignedProductJoinOperator,
    recognize_signed_product_reduction,
)
from zlang.targets import (
    TargetArchitectureError,
    load_architecture_templates,
    load_target,
    map_auto_signed_product_configuration,
)


ROOT = Path(__file__).resolve().parents[1]
TARGET = "xc7z030ffg676-1"


def _expression(shape: str):
    result_width = 8 + shape.count("*") - 1
    source = f"""
module SignedReduction {{
    in a : fixed<4,2>
    in b : fixed<4,2>
    in c : fixed<4,2>
    in d : fixed<4,2>
    in e : fixed<4,2>
    in f : fixed<4,2>
    out y : fixed<{result_width},4>
    y = {shape}
}}
"""
    return compile_source(source).ir.assignments[0].expression


@pytest.mark.parametrize(
    ("shape", "signs", "operators"),
    (
        ("a*b + c*d", ("add", "add"), ("add",)),
        ("a*b - c*d", ("add", "subtract"), ("subtract",)),
        ("a*b + c*d - e*f", ("add", "add", "subtract"), ("add", "subtract")),
        ("a*b - c*d + e*f", ("add", "subtract", "add"), ("subtract", "add")),
        ("a*b - c*d - e*f", ("add", "subtract", "subtract"), ("subtract", "subtract")),
    ),
)
def test_recognizes_frozen_ordered_left_spines(shape, signs, operators):
    expression = _expression(shape)
    reduction = recognize_signed_product_reduction(expression)
    assert reduction is not None
    assert tuple(item.sign.value for item in reduction.terms) == signs
    assert tuple(item.operator.value for item in reduction.joins) == operators
    assert reduction.original_expression is expression
    assert tuple(item.ordinal for item in reduction.terms) == tuple(range(len(signs)))
    assert tuple(item.ordinal for item in reduction.joins) == tuple(range(len(operators)))
    assert all(item.product_type == FixedType(8, 4) for item in reduction.terms)
    assert tuple(item.result_type.width for item in reduction.joins) == tuple(
        range(9, 9 + len(operators))
    )
    assert all(item.fractional_scale == 4 for item in reduction.terms)
    assert all(item.fractional_scale == 4 for item in reduction.joins)
    assert all(item.signedness == "signed" for item in reduction.terms)


def test_original_add_subtract_nodes_and_intermediate_types_are_retained():
    expression = _expression("a*b - c*d + e*f")
    reduction = recognize_signed_product_reduction(expression)
    assert reduction is not None
    first, second = reduction.joins
    assert isinstance(first.original_expression, Binary)
    assert first.original_expression.operator is BinaryOperator.SUBTRACT
    assert first.original_expression is expression.left
    assert first.result_type == FixedType(9, 4)
    assert isinstance(second.original_expression, Add)
    assert second.original_expression is expression
    assert second.left_type == FixedType(9, 4)
    assert second.result_type == FixedType(10, 4)


def test_parenthesized_right_tree_and_intermediate_conversion_fail_closed():
    right_tree = _expression("a*b - (c*d + e*f)")
    assert recognize_signed_product_reduction(right_tree) is None
    source = """
module ConvertedTerm {
    in a:fixed<4,2> in b:fixed<4,2>
    in c:fixed<4,2> in d:fixed<4,2>
    out y:fixed<9,4>
    y = quantize<fixed<8,4>>(a*b) { round floor overflow wrap } - c*d
}
"""
    converted = compile_source(source).ir.assignments[0].expression
    assert recognize_signed_product_reduction(converted) is None


def _pipeline_source(*, unsigned: bool = False, additive: bool = False) -> str:
    family = "ufixed" if unsigned else "fixed"
    operator = "+" if additive else "-"
    return f"""
module SignedProductAuto {{
    clock clk
    reset rst
    in a:{family}<4,2> in b:{family}<4,2>
    in c:{family}<4,2> in d:{family}<4,2>
    out y:{family}<6,2>
    y = implement {{
        quantize<{family}<6,2>>(a*b {operator} c*d) {{
            round nearest_even
            overflow saturate
        }}
        intent {{ latency <= 1 ii==1 }}
    }}
}}
"""


def _signed_mapping(source: str = None):
    result = compile_source(source or _pipeline_source(), target=TARGET)
    target, family, resources = load_target(TARGET)
    template = next(
        item for item in load_architecture_templates(operation="signed_product_reduction")
        if item.name == "Xilinx7SignedProductCascade"
    )
    resource = next(item for item in resources if item.name == template.resource_name)
    configuration = resource.pipeline_configuration("unregistered")
    graph = map_auto_signed_product_configuration(
        result.ir, target, family, resources, template, configuration,
    )
    return result, target, family, resources, template, configuration, graph


def test_generic_candidate_preserves_convert_and_has_ordered_timing_dag():
    # Exercise the generic timing DAG independently of target selection. The
    # target-aware planner now also publishes physical signed-product graphs.
    result = compile_source(_pipeline_source())
    exploration = result.ir.pipeline_explorations[0]
    assert exploration.selected.startswith("dag_partition_")
    assert "preserve_exact_signed_product_reduction" in (
        exploration.selected_candidate.transformations
    )
    assert isinstance(exploration.source_expression, FixedConvert)
    reduction = recognize_signed_product_reduction(
        exploration.source_expression.expression
    )
    assert reduction is not None
    assert result.implementation_graph.quantization is exploration.source_expression
    assert result.implementation_graph.semantic_region_identity == reduction.semantic_identity
    kinds = tuple(item.kind for item in result.implementation_graph.timing_dag.nodes)
    assert kinds == (
        "input_boundary", "product_segment", "product_segment",
        "subtract_join", "fixed_quantization", "output_boundary",
    )
    assert result.implementation_graph.timing_dag.output_latency == 1

    artifact = emit_target_artifact(result.ir, result.implementation_graph)
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.implementation.semantic_region_identity == reduction.semantic_identity
    assert tuple(item[1] for item in restored.implementation.timing_nodes) == kinds


def test_resource_capabilities_distinguish_add_and_subtract():
    _, target, family, resources, template, configuration, graph = _signed_mapping()
    assert tuple(dict(item.configuration)["accumulator_mode"] for item in graph.resources) == (
        "accumulator_plus_product", "accumulator_minus_product",
    )
    assert tuple(item.kind for item in graph.timing_dag.nodes if "signed_product" in item.kind) == (
        "signed_product_add_segment", "signed_product_subtract_segment",
    )
    resource = next(item for item in resources if item.name == template.resource_name)
    only_add = replace(
        resource,
        capabilities=tuple(
            (name, "accumulator_plus_product" if name == "accumulator_modes" else value)
            for name, value in resource.capabilities
        ),
    )
    changed = tuple(only_add if item.identity == resource.identity else item for item in resources)
    with pytest.raises(TargetArchitectureError, match="accumulator_minus_product"):
        map_auto_signed_product_configuration(
            compile_source(_pipeline_source()).ir, target, family, changed,
            template, configuration,
        )


def test_add_only_reduction_needs_only_add_capability():
    result, target, family, resources, template, configuration, _ = _signed_mapping(
        _pipeline_source(additive=True)
    )
    resource = next(item for item in resources if item.name == template.resource_name)
    only_add = replace(
        resource,
        capabilities=tuple(
            (name, "accumulator_plus_product" if name == "accumulator_modes" else value)
            for name, value in resource.capabilities
        ),
    )
    changed = tuple(only_add if item.identity == resource.identity else item for item in resources)
    graph = map_auto_signed_product_configuration(
        result.ir, target, family, changed, template, configuration,
    )
    assert all(
        dict(item.configuration)["accumulator_mode"] == "accumulator_plus_product"
        for item in graph.resources
    )


def test_unsigned_subtraction_is_zero_extended_into_physical_dsp_cascade():
    result = compile_source(_pipeline_source(unsigned=True), target=TARGET)
    assert not result.implementation_graph.is_generic
    assert len(result.implementation_graph.resources) == 2
    target, family, resources = load_target(TARGET)
    template = next(
        item for item in load_architecture_templates(operation="signed_product_reduction")
        if item.name == "Xilinx7SignedProductCascade"
    )
    resource = next(item for item in resources if item.name == template.resource_name)
    graph = map_auto_signed_product_configuration(
        result.ir, target, family, resources, template,
        resource.pipeline_configuration("unregistered"),
    )
    assert tuple(
        dict(item.configuration)["accumulator_mode"] for item in graph.resources
    ) == ("accumulator_plus_product", "accumulator_minus_product")
    assert all(
        mapping.expression is None
        or isinstance(mapping.expression.type, UFixedType)
        for item in graph.resources
        for mapping in item.semantic_mappings
        if mapping.resource_port in {"a", "b"}
    )


def test_graph_and_descriptor_identities_are_deterministic():
    first = _signed_mapping()
    second = _signed_mapping()
    assert first[-1].identity == second[-1].identity
    first_reduction = recognize_signed_product_reduction(
        first[0].ir.pipeline_explorations[0].source_expression.expression
    )
    second_reduction = recognize_signed_product_reduction(
        second[0].ir.pipeline_explorations[0].source_expression.expression
    )
    assert first_reduction.semantic_identity == second_reduction.semantic_identity
    assert first[-1].semantic_region_identity == first_reduction.semantic_identity


def test_fft_real_and_imag_are_recognized_without_width_annotations():
    source = (ROOT / "examples/fft/complex_multiply_pipeline_auto.zhl").read_text()
    real = compile_source(
        source, top="FFTComplexMultiplyRealAuto"
    ).ir.pipeline_explorations[0].source_expression
    real_reduction = recognize_signed_product_reduction(real.expression)
    assert real_reduction is not None
    assert tuple(term.sign for term in real_reduction.terms) == (
        ProductTermSign.ADD, ProductTermSign.SUBTRACT,
    )
    assert all(term.product_type == FixedType(34, 30) for term in real_reduction.terms)
    assert real_reduction.result_type == FixedType(35, 30)

    imag = compile_source(
        source, top="FFTComplexMultiplyImagAuto"
    ).ir.pipeline_explorations[0].source_expression
    imag_reduction = recognize_signed_product_reduction(imag.expression)
    assert imag_reduction is not None
    assert tuple(term.sign for term in imag_reduction.terms) == (
        ProductTermSign.ADD, ProductTermSign.ADD,
    )
    assert imag_reduction.joins[0].operator is SignedProductJoinOperator.ADD
    assert imag_reduction.result_type == FixedType(35, 30)


def test_real_target_profile_publishes_physical_signed_product_emission():
    result = compile_source(_pipeline_source(), target=TARGET)
    physical = tuple(
        item for item in result.target_planning_result.generated_candidates
        if "SignedProduct" in item.name and not item.graph.is_generic
    )
    assert len(physical) == 4
    assert all(len(item.graph.resources) == 2 for item in physical)
    assert not result.implementation_graph.is_generic
    assert result.implementation_graph.pipeline_configuration_identity
