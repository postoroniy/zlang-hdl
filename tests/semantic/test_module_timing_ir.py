from __future__ import annotations

import pytest

from zlang.ir.module import Module
from zlang.ir.timing import (
    InstanceOutputTiming,
    ModuleTimingContract,
    OutputTiming,
    TimingKnowledge,
    ValueTiming,
)
from zlang.opt.lowering import lower, restore
from zlang.opt.render import render
from zlang.source import SourceOrigin, SourceSpan


def test_timing_knowledge_has_unambiguous_constructors() -> None:
    assert ValueTiming.timeless() == ValueTiming(TimingKnowledge.TIMELESS)
    assert ValueTiming.known(0) == ValueTiming(TimingKnowledge.KNOWN, latency=0)
    assert ValueTiming.known(7).latency == 7
    assert ValueTiming.unknown("state") == ValueTiming(
        TimingKnowledge.UNKNOWN,
        reason="state",
    )


def test_value_timing_rejects_ambiguous_shapes() -> None:
    with pytest.raises(ValueError, match="known value timing"):
        ValueTiming(TimingKnowledge.KNOWN)
    with pytest.raises(ValueError, match="must not carry latency"):
        ValueTiming(TimingKnowledge.TIMELESS, latency=0)
    with pytest.raises(ValueError, match="requires a reason"):
        ValueTiming(TimingKnowledge.UNKNOWN)


def test_module_timing_metadata_lowers_restores_and_renders_losslessly() -> None:
    origin = SourceOrigin(
        SourceSpan(5, 3, 8, 4),
        "module-timing:Timed",
        "timed.zl",
        "a" * 64,
    )
    contract = ModuleTimingContract(4, 1, "clk", "rst", origin)
    module = Module(
        "Timed",
        (),
        (),
        clock="clk",
        reset="rst",
        timing_contract=contract,
        output_timings=(
            OutputTiming("y", ValueTiming.known(4)),
            OutputTiming("constant", ValueTiming.timeless()),
        ),
        instance_output_timings=(
            InstanceOutputTiming("child", "y", ValueTiming.known(3)),
        ),
    )

    canonical = lower(module)
    assert canonical.timing_contract == contract
    assert canonical.timing_contract is not None
    assert canonical.timing_contract.source_origin is origin
    assert canonical.output_timings == module.output_timings
    assert canonical.instance_output_timings == module.instance_output_timings

    restored = restore(canonical)
    assert restored == module
    assert restored.timing_contract is not None
    assert restored.timing_contract.source_origin is origin

    text = render(canonical)
    assert (
        "timing latency=4 ii=1 clock=clk reset=rst "
        "origin=5:3-8:4:module-timing:Timed"
    ) in text
    assert "output-timing port=y value=known(4)" in text
    assert "output-timing port=constant value=timeless" in text
    assert "instance-output-timing instance=child port=y value=known(3)" in text


@pytest.mark.parametrize(
    ("latency", "ii", "message"),
    ((-1, 1, "latency"), (0, 0, "II"), (True, 1, "latency")),
)
def test_module_timing_contract_rejects_invalid_numbers(
    latency: int,
    ii: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ModuleTimingContract(latency, ii)
