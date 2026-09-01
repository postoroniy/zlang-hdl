"""Runtime logical spellings fail as typed diagnostics, never internal errors."""

from __future__ import annotations

import pytest

from zlang.compiler import compile_source
from zlang.semantic import SemanticError


@pytest.mark.parametrize("operator", ("&&", "||"))
def test_runtime_logical_binary_operator_has_structured_diagnostic(
    operator: str,
) -> None:
    with pytest.raises(SemanticError) as caught:
        compile_source(
            f"module RuntimeLogic {{ in a,b:bit out y:bit y=a {operator} b }}",
            include_clash=False,
        )
    assert caught.value.code == "ZL-SEMANTIC-RUNTIME-LOGIC"
    assert "runtime logical operator" in str(caught.value)
    assert caught.value.primary is not None
    assert caught.value.fixes


def test_bitwise_hardware_operator_remains_supported() -> None:
    result = compile_source(
        "module BitwiseLogic { in a,b:bit out y:bit y=(a & b) | a }",
        include_clash=False,
    )
    assert result.ir.name == "BitwiseLogic"

