"""Copied pure function regions receive one producer, not conflicting aliases."""

from dataclasses import replace

import pytest

from zlang.backend.expression_materialization import ExpressionAliasMap
from zlang.backend.systemverilog.module_rendering import _function_region_emission
from zlang.backend.systemverilog.context import emission_scope
from zlang.ir import expressions as expr
from zlang.ir import CompileTimeBinderRef, CompileTimeExpr, FunctionalCaptureRef
from zlang.ir.types import UIntType, VecType, TupleType, BitsType


@pytest.fixture(autouse=True)
def active_emission():
    with emission_scope('CopiedRegions'):
        yield


def region(name='values'):
    u8 = UIntType(8)
    binder = CompileTimeBinderRef('fixture:index', 'index', 0, 32)
    capture = FunctionalCaptureRef(f'fixture:{name}', name, VecType(32, u8))
    return expr.FunctionalRegion(
        expr.FunctionalRegionKind.GENERATE, binder,
        expr.VectorIndex(capture, CompileTimeExpr.ref(binder), u8), (),
        ((capture, expr.InputRef(name, capture.type)),), VecType(32, u8),
    )


def test_copied_function_regions_have_one_local_producer():
    original = region()
    copied = replace(original)
    assert copied is not original
    root = expr.TupleConstruct((original, copied), TupleType((original.type, original.type)))
    rewritten, declarations, statements = _function_region_emission(root, reserved_names=('values',))
    assert len([line for line in declarations if 'zlang_fn_region_' in line]) == 1
    assert isinstance(rewritten, expr.TupleConstruct)
    assert rewritten.elements[0].name == rewritten.elements[1].name
    assert statements


def test_distinct_captures_are_not_merged():
    left, right = region('left'), region('right')
    root = expr.TupleConstruct((left, right), TupleType((left.type, right.type)))
    rewritten, declarations, _ = _function_region_emission(root, reserved_names=('left', 'right'))
    assert len([line for line in declarations if 'zlang_fn_region_' in line]) == 2
    assert rewritten.elements[0].name != rewritten.elements[1].name


def test_alias_conflict_still_fails_closed():
    original = region()
    aliases = ExpressionAliasMap(((original, 'first'),))
    with pytest.raises(ValueError, match='conflicting backend aliases'):
        aliases[replace(original)] = 'second'


def test_closed_region_owns_complex_capture_once_before_its_loop():
    original = region()
    packed = BitsType(256)
    captured = expr.Bitcast(expr.Binary(
        expr.BinaryOperator.BIT_XOR, expr.InputRef('values', packed),
        expr.Constant(0xaa, packed), packed, packed), original.type)
    original = replace(original, captures=((original.captures[0][0], captured),))
    root = expr.TupleConstruct((original, replace(original)), TupleType((original.type, original.type)))
    _, declarations, statements = _function_region_emission(root, reserved_names=('values',))
    assert sum('zlang_fn_capture_' in line for line in declarations) == 1
    assert statements[0].strip().startswith('zlang_fn_capture_')
    assert sum('values' in line for line in statements) == 1
