from __future__ import annotations

from zlang.compiler import compile_source
from zlang.opt import lower, restore


def test_module_signature_round_trips_through_both_canonical_stages() -> None:
    result = compile_source(
        """
interface TimedIfc {
    clock clk
    reset rst
    in x:u8 @clk
    out y:u8 @clk
    timing { latency 2 ii 1 }
}
module Timed : TimedIfc {
    clock clk
    reset rst
    in x:u8 @clk
    out y:u8 @clk
    y=delay<2>(x)
    timing { latency 2 ii 1 }
}
""",
        include_clash=False,
    )
    signature = result.ir.module_signature
    assert signature is not None
    assert result.high_level_ir.module_signature == signature
    assert result.optimization_ir.module_signature == signature
    assert restore(lower(result.ir)).module_signature == signature
    assert signature.to_data()["timing_contract"] == {
        "clock_domain": "clk",
        "ii": 1,
        "latency": 2,
        "reset_domain": "rst",
    }


def test_named_interface_does_not_change_generated_clash_text() -> None:
    plain = compile_source("module Pass { in a:u8 out y:u8 y=a }")
    named = compile_source(
        """
interface PassIfc { in a:u8 out y:u8 }
module Pass : PassIfc { in a:u8 out y:u8 y=a }
"""
    )
    assert named.clash == plain.clash
