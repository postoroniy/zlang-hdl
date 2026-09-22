from __future__ import annotations

from zlang.compiler import compile_source
from zlang.ir import Call, Constant
from zlang.ir.normalization import normalize_selected_values
from zlang.parser import parse
from zlang.semantic import analyze


def test_selected_normalization_folds_parameter_independent_call_and_keeps_semantic_ir() -> None:
    source = "fn fixed(unused:u8)->u8{255} module Top{in x:u8 out y:u8 y=fixed(x)}"

    semantic = analyze(parse(source))
    selected = normalize_selected_values(compile_source(source).ir)

    assert tuple(function.name for function in semantic.functions) == ("fixed",)
    assert isinstance(semantic.assignments[0].expression, Call)
    assert selected.functions == ()
    assert isinstance(selected.assignments[0].expression, Constant)
    assert selected.assignments[0].expression.value == 255


def test_selected_normalization_folds_nested_constant_calls_with_exact_overflow() -> None:
    result = normalize_selected_values(compile_source(
        "fn plus(x:u8,y:u8)->u8{truncate<8>(x+y)} "
        "fn nested()->u8{plus(250,10)} "
        "module Top{out y:u8 y=nested()}",
    ).ir)

    assert result.functions == ()
    assert result.callable_definitions == ()
    value = result.assignments[0].expression
    assert isinstance(value, Constant)
    assert value.value == 4


def test_selected_normalization_inlines_one_small_use_but_retains_multi_use() -> None:
    single = normalize_selected_values(compile_source(
        "fn widen(x:u8)->u9{x+1} module Top{in x:u8 out y:u9 y=widen(x)}"
    ).ir)
    repeated = normalize_selected_values(compile_source(
        "fn widen(x:u8)->u9{x+1} "
        "module Top{in x:u8 out a:u9 out b:u9 a=widen(x) b=widen(x)}"
    ).ir)

    assert single.functions == ()
    assert not isinstance(single.assignments[0].expression, Call)
    assert tuple(function.name for function in repeated.functions) == ("widen",)
    assert all(isinstance(item.expression, Call) for item in repeated.assignments)


def test_selected_normalization_retains_generic_provenance_after_dce() -> None:
    result = normalize_selected_values(compile_source(
        "fn value<K>()->u8{K} module Top{out y:u8 y=value<K=7>()}"
    ).ir)

    assert result.callable_definitions == ()
    assert len(result.generic_specializations) == 1
    assert isinstance(result.assignments[0].expression, Constant)
    assert result.assignments[0].expression.value == 7


def test_selected_normalization_visits_each_shared_dag_node_once() -> None:
    module = compile_source(
        "module Top{in x:u8 out a:u9 out b:u9 a=x+1 b=x+1}"
    ).ir

    normalized = normalize_selected_values(module)
    statistics = normalized.selected_value_normalization_statistics

    assert statistics is not None
    assert statistics.expression_cache_hits > 0
    assert statistics.unique_expression_visits == (
        statistics.expression_requests - statistics.expression_cache_hits
    )
