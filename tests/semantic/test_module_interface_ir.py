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
