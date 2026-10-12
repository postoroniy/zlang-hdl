"""Consolidated invariants for the optimizer infrastructure boundary."""

from dataclasses import replace

import pytest

from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir.type_codec import (
    TypeCodecError,
    scalar_type_data,
    scalar_type_from_data,
    scalar_type_from_name,
    scalar_type_name,
)
from zlang.ir.types import (
    BitType,
    BitsType,
    FixedOverflowPolicy,
    FixedType,
    SIntType,
    StructField,
    StructType,
    UFixedType,
    UIntType,
)
from zlang.opt import (
    RewriteRule,
    render_saturation,
    saturate,
)
from zlang.opt.rewrite_spec import (
    BUILTIN_REWRITE_SPECS,
    TypedRewriteSpec,
    builtin_rewrite_spec,
)
from zlang.opt.capabilities import (
    ArchitectureInterest,
    RewriteBarrier,
    architecture_interests,
    expression_capability,
    rewrite_barriers,
    semantic_expression_op,
)
from zlang.opt.ir import ExpressionOp
from zlang.resource_matching import DspMultiplyResourceMatcher
from zlang.targets import load_target


@pytest.mark.parametrize(
    ("type_", "payload"),
    (
        (BitType(), {"kind": "bit"}),
        (UIntType(9), {"kind": "uint", "width": 9}),
        (SIntType(13), {"kind": "sint", "width": 13}),
        (BitsType(7), {"kind": "bits", "width": 7}),
        (
            FixedType(16, 11),
            {"kind": "fixed", "width": 16, "fraction": 11, "overflow": "wrap"},
        ),
        (
            UFixedType(12, 4, FixedOverflowPolicy.SATURATE),
            {
                "kind": "ufixed",
                "width": 12,
                "fraction": 4,
                "overflow": "saturate",
            },
        ),
    ),
)
def test_shared_scalar_type_codec_preserves_frozen_egraph_spelling(
    type_: object,
    payload: dict[str, object],
) -> None:
    assert scalar_type_data(type_) == payload
    assert scalar_type_from_data(payload) == type_
    assert scalar_type_from_name(scalar_type_name(type_)) == type_


def test_scalar_type_codec_remains_fail_closed_for_aggregates() -> None:
    aggregate = StructType("Pair", (StructField("left", UIntType(8)),))
    with pytest.raises(TypeCodecError, match="only scalar"):
        scalar_type_data(aggregate)


def test_capabilities_and_rewrite_barriers_are_explicit() -> None:
    assert expression_capability(ExpressionOp.ADD).resource_matchable
    assert expression_capability(ExpressionOp.RUNTIME_INDEX).schedulable_scalar
    assert not expression_capability(ExpressionOp.RUNTIME_INDEX).egraph_exact
    assert RewriteBarrier.CARRY_GROWTH in rewrite_barriers(ExpressionOp.ADD)
    assert RewriteBarrier.TRUNCATION in rewrite_barriers(ExpressionOp.TRUNCATE)
    assert RewriteBarrier.ROUNDING in rewrite_barriers(ExpressionOp.FIXED_CONVERT)
    assert RewriteBarrier.BIT_REINTERPRETATION in rewrite_barriers(
        ExpressionOp.BITCAST
    )
    assert rewrite_barriers(ExpressionOp.FIFO_REF) == frozenset(
        {RewriteBarrier.STATE_OR_EFFECT}
    )


def test_semantic_operation_capability_mapping_preserves_scheduler_surface() -> None:
    value = expr.Binary(
        expr.BinaryOperator.MULTIPLY,
        expr.InputRef("a", UIntType(8)),
        expr.InputRef("b", UIntType(8)),
        UIntType(8),
        UIntType(16),
    )
    operation = semantic_expression_op(value)
    assert operation is ExpressionOp.BINARY
    assert expression_capability(operation).schedulable_scalar


def test_architecture_interest_is_target_neutral_and_identity_free() -> None:
    multiply_add = compile_source(
        "module M { in a,b:u8 in c:u16 out y:u17 y=a*b+c }"
    ).ir.assignments[0].expression
    preadder_multiply = compile_source(
        "module P { in a,b,c:u8 out y:u17 y=(a+b)*c }"
    ).ir.assignments[0].expression
    assert ArchitectureInterest.MULTIPLY_ADD in architecture_interests(multiply_add)
    assert ArchitectureInterest.PREADDER_MULTIPLY in architecture_interests(
        preadder_multiply
    )
    assert all("DSP" not in item.value for item in ArchitectureInterest)


def test_declarative_rewrite_spec_owns_stable_registration_metadata() -> None:
    spec = TypedRewriteSpec.builtin(
        "bit_or_zero",
        RewriteRule.BIT_OR_ZERO,
        ("or_zero", "|"),
    )
    assert spec.identity == "bit_or_zero"
    assert spec.provenance == ("builtin:bit_or_zero",)
    assert spec.direction == "equality"
    assert builtin_rewrite_spec("bit_or_zero") == spec
    assert len(BUILTIN_REWRITE_SPECS) == len(
        {item.identity for item in BUILTIN_REWRITE_SPECS}
    )
    with pytest.raises(ValueError, match="sorted and unique"):
        replace(spec, provenance=("z", "a"))


def test_production_has_no_second_python_rewrite_engine() -> None:
    import zlang.opt.saturation as engine

    assert not hasattr(engine, "_legacy_saturate")
    assert not hasattr(engine, "_rewrite_anywhere")
    assert not hasattr(engine, "_rewrite_root")


def test_rewrite_registration_and_report_are_deterministic() -> None:
    compilation = compile_source("module M { in x:u8 out y:u8 y=(x|0)^0 }")
    root = compilation.optimization_ir.assignments[0].expression
    first = saturate(compilation.optimization_ir, root)
    second = saturate(compilation.optimization_ir, root)
    assert first.registrations == second.registrations
    assert render_saturation(first) == render_saturation(second)
    assert all(item.identity and item.provenance for item in first.registrations)


def test_dsp_matcher_is_separate_and_preserves_width_and_timing_guards() -> None:
    _, _, resources = load_target("xc7z030ffg676-1")
    raw_dsp = next(item for item in resources if item.resource_class == "dsp_mac")
    dsp = replace(
        raw_dsp,
        pipeline_sites=tuple(
            replace(
                site,
                estimated_delay_ps=(900 if site.semantic_location == "multiply" else 0),
            )
            for site in raw_dsp.pipeline_sites
        ),
    )
    matcher = DspMultiplyResourceMatcher()
    legal = expr.Binary(
        expr.BinaryOperator.MULTIPLY,
        expr.InputRef("a", UIntType(17)),
        expr.InputRef("b", UIntType(8)),
        UIntType(17),
        UIntType(25),
    )
    assert matcher.match(legal, "multiply", dsp) is not None

    too_wide = replace(
        legal,
        left=expr.InputRef("a", UIntType(25)),
        type=UIntType(33),
    )
    assert matcher.match(too_wide, "multiply", dsp) is None
    assert matcher.match(legal, "add", dsp) is None

    no_timing = replace(
        dsp,
        pipeline_sites=tuple(
            replace(site, estimated_delay_ps=0) for site in dsp.pipeline_sites
        ),
    )
    assert matcher.match(legal, "multiply", no_timing) is None
