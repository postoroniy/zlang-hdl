"""Minimized exact-state blocker for the IEEE inverse DIF-SDF stage."""

from pathlib import Path

import pytest

from zlang.compiler import compile_source
from zlang.ir.expressions import (
    FixedConversionKind,
    FixedConvert,
    FixedOverflow,
    FixedRounding,
)
from zlang.ir.state import StateActionKind
from zlang.ir.types import FixedType


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "docs/reproducers/ifft_sdf_exact_feedback_type_growth.zhl"


def _compile(depth: int):
    source = SOURCE.read_text()
    if depth != 4:
        source = source.replace("<D=4>", f"<D={depth}>", 1)
    return compile_source(
        source,
        top="IFFTExactFeedbackTypeGrowth",
        include_clash=False,
    ).ir


@pytest.mark.parametrize("depth", (4, 8))
def test_exact_sdf_feedback_requires_forbidden_intermediate_narrowing(
    depth: int,
) -> None:
    module = _compile(depth)
    assert module.fifos[0].depth == depth

    transition = module.resolved_transition
    assert transition is not None
    push = next(
        action
        for group in transition.action_groups
        for action in group.actions
        if action.kind is StateActionKind.FIFO_PUSH
    )
    assert len(push.operands) == 1
    conversion = push.operands[0]
    assert isinstance(conversion, FixedConvert)

    # The current cell is fixed<17,15>.  Exact subtraction widens it to
    # fixed<18,15>; returning it to the homogeneous cell is therefore a real
    # numerical narrowing boundary, not a harmless representation cast.
    assert conversion.expression.type == FixedType(18, 15)
    assert conversion.type == FixedType(17, 15)
    assert conversion.kind is FixedConversionKind.RESCALE
    assert conversion.rounding is FixedRounding.TOWARD_ZERO
    assert conversion.overflow is FixedOverflow.WRAP


def test_larger_homogeneous_cell_only_moves_the_same_growth_boundary() -> None:
    source = SOURCE.read_text().replace(
        "fixed<17,15>", "fixed<23,15>"
    )
    module = compile_source(
        source,
        top="IFFTExactFeedbackTypeGrowth",
        include_clash=False,
    ).ir
    transition = module.resolved_transition
    assert transition is not None
    push = next(
        action
        for group in transition.action_groups
        for action in group.actions
        if action.kind is StateActionKind.FIFO_PUSH
    )
    conversion = push.operands[0]
    assert isinstance(conversion, FixedConvert)
    assert conversion.expression.type == FixedType(24, 15)
    assert conversion.type == FixedType(23, 15)
