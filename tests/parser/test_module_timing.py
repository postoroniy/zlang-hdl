from __future__ import annotations

import pytest

from zlang.ast import ModuleTimingDecl
from zlang.parser import ParseError, parse


def test_module_timing_block_retains_exact_directives_origin_and_order() -> None:
    module = parse(
        """module Timed {
  clock clk
  reset rst
  in x : u8
  out y : u8
  timing {
    latency 4
    ii 1
  }
  y = pipeline(4) { x }
}
"""
    )

    assert module.timing == ModuleTimingDecl(4, 1)
    assert module.timing is not None
    assert module.timing.origin is not None
    assert module.timing.origin.render() == "6:3-9:4"
    assert sum(isinstance(item, ModuleTimingDecl) for item in module.ordered_items) == 1


@pytest.mark.parametrize(
    ("body", "message"),
    (
        ("timing {}", "exactly one latency"),
        ("timing { latency 0 }", "exactly one latency"),
        ("timing { ii 1 }", "exactly one latency"),
        ("timing { latency 0 latency 1 ii 1 }", "exactly one latency"),
        ("timing { latency 0 ii 1 ii 1 }", "exactly one latency"),
    ),
)
def test_module_timing_requires_each_directive_exactly_once(
    body: str,
    message: str,
) -> None:
    with pytest.raises(ParseError, match=message):
        parse(f"module Bad {{ {body} }}")


def test_module_accepts_at_most_one_timing_block() -> None:
    with pytest.raises(ParseError, match="at most one timing block"):
        parse(
            "module Bad { "
            "timing { latency 0 ii 1 } "
            "timing { latency 0 ii 1 } "
            "}"
        )
