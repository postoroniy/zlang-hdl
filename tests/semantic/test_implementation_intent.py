import pytest

from zlang.ast import nodes as ast
from zlang.candidate_sites import CandidateSiteKind
from zlang.compiler import compile_source
from zlang.exploration import TransformFamily
from zlang.ir import expressions as ir_expr
from zlang.parser import ParseError, parse
from zlang.semantic.analyze import SemanticError


def _compile(source: str):
    return compile_source(source)


def test_implement_parser_accepts_intent_constraints_and_objective() -> None:
    module = parse(
        """
        module ImplementDemo {
          in a : vec<4,u8>
          in b : vec<4,u8>
          out y : u18

          y = implement {
            dot(a, b)

            intent {
              latency <= 4
              ii == 1
              dsp <= 8
              fmax >= 300
              minimize lut
            }
          }
        }
        """
    )
    expression = module.assignments[0].expression
    assert isinstance(expression, ast.ImplementExpr)
    assert tuple((item.metric.value, item.relation.value, item.value) for item in expression.constraints) == (
        ("latency", "<=", 4),
        ("ii", "==", 1),
        ("dsp", "<=", 8),
        ("fmax_est", ">=", 300),
    )
    assert expression.objective == ast.ExplorationObjective(
        "minimize", ast.CostMetric.LUT
    )


def test_implement_lowers_to_normalized_policy_and_candidate_site() -> None:
    result = _compile(
        """
        module ImplementPolicy {
          clock clk
          reset rst
          in a : vec<4,u8>
          in b : vec<4,u8>
          out y : u18

          y = implement {
            dot(a, b)
            intent { latency <= 4 ii == 1 dsp <= 8 minimize lut }
          }
        }
        """
    )
    assert len(result.exploration_results) == 1
    exploration = result.exploration_results[0]
    assert exploration.site_kind == "implement"
    assert set(exploration.request.allowed) == {
        TransformFamily.DSP,
        TransformFamily.PIPELINE,
        TransformFamily.REDUCTION,
    }
    policy = next(item for item in result.implementation_policy.regions if item.source_form)
    assert policy.source_form == "implement"
    assert result.candidate_site_ledger.sites[0].kind is CandidateSiteKind.IMPLEMENT
    assert "source=implement" in result.implementation_policy_report
    assert "selected:" in result.implementation_report


def test_implement_defaults_enable_reduction_and_dsp_candidates() -> None:
    result = _compile(
        """
        module ImplementReduction {
          in a : vec<4,u3>
          in b : vec<4,u3>
          out y : u8

          y = implement {
            dot(a, b)
            intent { dsp <= 4 minimize lut }
          }
        }
        """
    )
    candidates = result.exploration_results[0].generated_candidates
    assert any(item.architecture is not None for item in candidates)
    assert any(any(stage.startswith("reduction:") for stage in item.stages) for item in candidates)


@pytest.mark.parametrize(
    ("objective", "expected_metric"),
    (
        ("minimize lut", ir_expr.CostMetric.LUT),
        ("minimize ff", ir_expr.CostMetric.FF),
        ("minimize dsp", ir_expr.CostMetric.DSP),
        ("minimize bram", ir_expr.CostMetric.BRAM),
        ("minimize latency", ir_expr.CostMetric.LATENCY),
        ("maximize fmax", ir_expr.CostMetric.FMAX_EST),
    ),
)
def test_implement_accepts_exact_objective_matrix(
    objective: str, expected_metric: ir_expr.CostMetric
) -> None:
    result = _compile(
        "module Objective { in a:u8 out y:u8 "
        f"y=implement {{ a intent {{ {objective} }} }} }}"
    )
    assert result.exploration_results[0].request.objective is expected_metric


def test_implement_pipeline_candidates_require_clocked_positive_latency() -> None:
    expression = "a*b+c*d+e*f+g*h"
    with pytest.raises(SemanticError, match="module clock and reset"):
        _compile(
            "module NoClock { in a:u2 in b:u2 in c:u2 in d:u2 "
            "in e:u2 in f:u2 in g:u2 in h:u2 out y:u7 "
            f"y=implement {{ {expression} intent {{ latency >= 1 minimize ff }} }} }}"
        )

    result = _compile(
        "module Clocked { clock clk reset rst in a:u2 in b:u2 in c:u2 in d:u2 "
        "in e:u2 in f:u2 in g:u2 in h:u2 out y:u7 "
        f"y=implement {{ {expression} intent {{ latency >= 1 minimize ff }} }} }}"
    )
    candidates = result.exploration_results[0].generated_candidates
    assert any(item.cost.latency.value and item.cost.latency.value > 0 for item in candidates)


@pytest.mark.parametrize("metric,limit", (("lut", 1000), ("ff", 1000), ("bram", 1)))
def test_positive_latency_projects_only_pipeline_metrics(metric: str, limit: int) -> None:
    """Generic M28 bounds must survive without leaking into PipelineMetric."""

    result = _compile(
        "module OuterBound { clock clk reset rst "
        "in a:vec<4,u3> in b:vec<4,u3> out y:u8 "
        f"y=implement {{ dot(a,b) intent {{ latency >= 1 {metric} <= {limit} "
        "minimize lut } } }"
    )
    assert result.exploration_results
    assert result.exploration_results[0].request.constraints


@pytest.mark.parametrize(
    ("metric", "relation", "value"),
    (("lut", "<=", 0), ("ff", "<=", 0), ("bram", ">=", 1)),
)
def test_impossible_outer_policy_has_bounded_structured_diagnostic(
    metric: str, relation: str, value: int,
) -> None:
    with pytest.raises(SemanticError) as caught:
        _compile(
            "module Impossible { clock clk reset rst "
            "in a:vec<4,u3> in b:vec<4,u3> out y:u8 "
            f"y=implement {{ dot(a,b) intent {{ latency >= 1 {metric} "
            f"{relation} {value} "
            "minimize lut } } }"
        )
    error = caught.value
    assert error.code == "ZL-IMPLEMENT-CONSTRAINTS"
    assert error.primary is not None
    assert f"{metric} {relation} {value}" in str(error)
    assert "candidates:" in str(error)
    assert "expression trees" in error.notes[0]
    assert len(str(error)) < 8_192


@pytest.mark.parametrize("objective", ("minimize fmax", "minimize ii", "maximize ii"))
def test_implement_rejects_unsupported_objective_matrix(objective: str) -> None:
    with pytest.raises(SemanticError, match="supports only"):
        _compile(
            "module BadObjective { in a:u8 out y:u8 "
            f"y=implement {{ a intent {{ {objective} }} }} }}"
        )


def test_removed_explore_requires_canonical_implement() -> None:
    with pytest.raises(ParseError, match="scalar explore was removed"):
        parse(
            "module LegacyExplore { in a:vec<4,u3> in b:vec<4,u3> out y:u8 "
            "y=explore { dot(a,b) minimize lut } }"
        )


def test_explicit_pipeline_and_choice_remain_distinct() -> None:
    pipeline_result = _compile(
        "module FixedPipe { clock c reset r in a:u8 out y:u8 "
        "y=pipeline(3){a} }"
    )
    assert isinstance(pipeline_result.ir.assignments[0].expression, ir_expr.Pipeline)
    assert pipeline_result.ir.assignments[0].expression.stages == 3
    assert pipeline_result.exploration_results == ()

    choice_result = _compile(
        "module Pick { in a:u4 in b:u4 in c:u8 out y:u9 "
        "y=choice(auto,minimize=lut,dsp<=1){mul_add=>a*b+c dsp_mac=>a*b+c} }"
    )
    assert isinstance(choice_result.ir.assignments[0].expression, ir_expr.ImplementationChoice)
    assert choice_result.implementation_policy.regions[0].source_form == "choice(auto)"


def test_malformed_implement_intent_is_rejected() -> None:
    with pytest.raises(ParseError, match="constraint or objective"):
        parse("module Empty { in a:u8 out y:u8 y=implement { a intent {} } }")
    with pytest.raises(ParseError, match="repeats 'lut' constraint"):
        parse(
            "module Repeat { in a:u8 out y:u8 "
            "y=implement { a intent { lut <= 1 lut <= 2 } } }"
        )
    with pytest.raises(ParseError, match="exactly one objective"):
        parse(
            "module RepeatObj { in a:u8 out y:u8 "
            "y=implement { a intent { minimize lut minimize ff } } }"
        )
    with pytest.raises(SemanticError, match="maximize currently supports"):
        _compile(
            "module BadMax { in a:u8 out y:u8 "
            "y=implement { a intent { maximize lut } } }"
        )
    with pytest.raises(SemanticError, match="complete wire-output assignment"):
        _compile(
            "module Nested { in a:u8 out y:u8 "
            "y=(implement { a intent { minimize lut } }) + 0 }"
        )
