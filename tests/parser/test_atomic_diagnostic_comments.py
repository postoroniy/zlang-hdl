from __future__ import annotations

import pytest

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


def test_actual_else_when_retains_atomic_scheduling_diagnostic() -> None:
    source = """
    module InvalidElseWhen {
        clock clk
        reset rst
        in go : bit
        reg value : u1 = 0
        when go { value <- 1 }
        else when go { value <- 0 }
        out y : u1
        y = value
    }
    """
    with pytest.raises(
        ParseError,
        match=(
            "else when is not an atomic rule form; use independent when rules "
            r"or priority \{ \.\.\. \}"
        ),
    ):
        parse(source)
