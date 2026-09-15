from zlang.backend.systemverilog.sequential import (
    effective_reset_signal,
    reset_conditioner_lines,
)
from zlang.ir.cdc import ClockDomain, ResetMode, ResetReleaseMode
from zlang.ir.expressions import Constant
from zlang.ir.module import Module, Register
from zlang.ir.types import UIntType


def _identifier(value: str) -> str:
    return value


def test_safe_async_release_has_one_conditioner_per_domain() -> None:
    domains = (
        ClockDomain(
            "clk_a",
            "rst_a",
            reset_mode=ResetMode.ASYNCHRONOUS,
            reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
            reset_release_cycles=2,
        ),
        ClockDomain(
            "clk_b",
            "rst_b",
            reset_mode=ResetMode.ASYNCHRONOUS,
            reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
            reset_release_cycles=2,
        ),
    )
    type_ = UIntType(8)
    module = Module(
        "DualReset",
        (),
        (),
        registers=(
            Register("a", type_, Constant(0, type_), "clk_a"),
            Register("b", type_, Constant(0, type_), "clk_b"),
        ),
        clock_domains=domains,
    )

    lines = reset_conditioner_lines(module, _identifier)
    text = "\n".join(lines)

    assert text.count('(* ASYNC_REG = "TRUE" *)') == 2
    assert "posedge clk_a or posedge rst_a" in text
    assert "posedge clk_b or posedge rst_b" in text
    assert effective_reset_signal(module, _identifier, "clk_a") in text
    assert effective_reset_signal(module, _identifier, "clk_b") in text
    assert (
        effective_reset_signal(module, _identifier, "clk_a")
        != effective_reset_signal(module, _identifier, "clk_b")
    )
