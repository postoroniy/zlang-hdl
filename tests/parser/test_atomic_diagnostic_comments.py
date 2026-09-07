from __future__ import annotations

import pytest

from zlang.ast.nodes import ConditionalAction
from zlang.parser import ParseError, parse


@pytest.mark.parametrize(
    "comment",
    (
        "// Documentation may say: } else when\n",
        "/* Documentation may say: } else when */",
    ),
)
def test_else_when_text_inside_comments_is_not_source_syntax(comment: str) -> None:
    syntax = parse(f"module CommentOnly {{ {comment} out y:u1 y=0 }}")
    assert syntax.name == "CommentOnly"


def test_actual_nested_else_when_is_source_syntax() -> None:
    source = """
    module NestedElseWhen {
        clock clk
        reset rst
        in go, clear : bit
        reg value : u1 = 0
        when go {
            when clear { value <- 0 }
            else when value == 0 { value <- 1 }
            else { value <- value }
        }
        out y : u1
        y = value
    }
    """
    module = parse(source)
    branch = module.rules[0].actions[0]
    assert isinstance(branch, ConditionalAction)
    assert branch.when_false is not None
    assert isinstance(branch.when_false[0], ConditionalAction)


def test_orphan_else_remains_a_syntax_error() -> None:
    with pytest.raises(ParseError, match="syntax error"):
        parse("module Orphan { clock clk reset rst else { } }")
