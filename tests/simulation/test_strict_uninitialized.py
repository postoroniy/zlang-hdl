"""Opt-in definite-initialization checks for the native two-state simulator."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

import zlang
from zlang.backend.systemverilog import emit_artifact_with_source_map
from zlang.sim import SimulationRuntimeError
from zlang.simulation_compare import compare_program


IVERILOG = shutil.which("iverilog")


def _source(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / f"{name}.zhl"
    path.write_text(text, encoding="utf-8")
    return path


def test_default_two_state_seed_is_unchanged_and_strict_mode_is_opt_in(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "unreset",
        """
        module Unreset {
          clock clk reset rst
          out y:u8
          reg q:u8
          y=q
        }
        """,
    )

    with zlang.sim.load(source, top="Unreset") as compatible:
        assert compatible.eval() == {"y": 0}
    with zlang.sim.load(
        source, top="Unreset", strict_uninitialized=True
    ) as strict:
        with pytest.raises(
            SimulationRuntimeError,
            match=r"observed 'y'.*U \(never refreshed\)",
        ):
            strict.eval()
        with pytest.raises(SimulationRuntimeError, match="observed 'q'"):
            strict.get("q")
        with pytest.raises(SimulationRuntimeError, match="observed 'y'"):
            strict.reset("rst", asserted=True)


def test_conditional_rule_write_initializes_only_when_it_fires(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "conditional_write",
        """
        module ConditionalWrite {
          clock clk reset rst
          in load:bit in value:u8 out y:u8
          reg q:u8
          when load { q <- value }
          y=q
        }
        """,
    )

    with zlang.sim.load(
        source, top="ConditionalWrite", strict_uninitialized=True
    ) as instance:
        instance.set("load", 0)
        instance.set("value", 19)
        with pytest.raises(SimulationRuntimeError, match="observed 'y'"):
            instance.edge("clk")
        instance.set("load", 1)
        assert instance.edge("clk") == {"y": 19}
        instance.set("load", 0)
        instance.set("value", 99)
        assert instance.edge("clk") == {"y": 19}


def test_poison_propagates_through_state_until_a_definite_write(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "poison_state",
        """
        module PoisonState {
          clock clk reset rst
          in repair:bit in value:u8 out y:u8
          reg source:u8
          reg sink:u8=7
          when repair { source <- value }
          sink <- source
          y=sink
        }
        """,
    )

    with zlang.sim.load(
        source, top="PoisonState", strict_uninitialized=True
    ) as instance:
        assert instance.eval() == {"y": 7}
        instance.set("repair", 0)
        instance.set("value", 31)
        with pytest.raises(SimulationRuntimeError, match="observed 'y'"):
            instance.edge("clk")
        instance.set("repair", 1)
        # The source becomes initialized on this edge, while sink still
        # receives the pre-edge poison.  One following edge transfers the
        # definitely initialized source value into sink.
        with pytest.raises(SimulationRuntimeError, match="observed 'y'"):
            instance.edge("clk")
        assert instance.edge("clk") == {"y": 31}


def test_registered_output_without_reset_uses_the_same_state_tracking(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "registered_output",
        """
        module RegisteredOutput {
          clock clk reset rst
          in load:bit in value:u8
          out reg held:u8
          when load { held <- value }
        }
        """,
    )

    with zlang.sim.load(
        source, top="RegisteredOutput", strict_uninitialized=True
    ) as instance:
        with pytest.raises(SimulationRuntimeError, match="observed 'held'"):
            instance.eval()
        instance.set("load", 1)
        instance.set("value", 0xA5)
        assert instance.edge("clk") == {"held": 0xA5}


def test_initialized_state_remains_immediately_readable_in_strict_mode(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "initialized",
        """
        module Initialized {
          clock clk reset rst
          out y:u8
          reg q:u8=5
          y=q
        }
        """,
    )

    with zlang.sim.load(
        source, top="Initialized", strict_uninitialized=True
    ) as instance:
        assert instance.eval() == {"y": 5}
        assert instance.reset("rst", asserted=True) == {"y": 5}


def test_scalar_hierarchy_propagates_child_initializedness(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "hierarchical_unreset",
        """
        module Child {
          clock clk reset rst
          in load:bit in value:u8 out y:u8
          reg q:u8
          when load { q <- value }
          y=q
        }
        module Top {
          clock clk reset rst
          in load:bit in value:u8 out y:u8
          inst child:Child
          child.load=load
          child.value=value
          y=child.y
        }
        """,
    )

    with zlang.sim.load(source, top="Top", strict_uninitialized=True) as instance:
        with pytest.raises(SimulationRuntimeError, match="observed 'y'"):
            instance.eval()
        instance.set("load", 1)
        instance.set("value", 42)
        assert instance.edge("clk") == {"y": 42}


def test_strict_plan_is_deterministic_and_does_not_change_default_plan(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "plan_identity",
        """
        module PlanIdentity {
          clock clk reset rst out y:u8 reg q:u8 y=q
        }
        """,
    )

    ordinary = zlang.sim.compile(source, top="PlanIdentity")
    repeated = zlang.sim.compile(source, top="PlanIdentity")
    strict = zlang.sim.compile(
        source, top="PlanIdentity", strict_uninitialized=True
    )
    strict_repeated = zlang.sim.compile(
        source, top="PlanIdentity", strict_uninitialized=True
    )
    assert ordinary.plan.to_bytes() == repeated.plan.to_bytes()
    assert ordinary.plan.identity == repeated.plan.identity
    assert strict.plan.to_bytes() == strict_repeated.plan.to_bytes()
    assert strict.plan.identity == strict_repeated.plan.identity
    # Observation policy no longer creates a parallel instrumented plan.
    assert strict.plan.identity == ordinary.plan.identity
    assert strict.plan.to_bytes() == ordinary.plan.to_bytes()
    ordinary_rtl, ordinary_source_map = emit_artifact_with_source_map(
        ordinary.module
    )
    strict_rtl, strict_source_map = emit_artifact_with_source_map(strict.module)
    assert strict_rtl.text == ordinary_rtl.text
    assert strict_rtl.artifact_hash == ordinary_rtl.artifact_hash
    assert strict_source_map == ordinary_source_map
    assert not any(
        str(item["name"]).startswith("$zlang_strict_")
        for table in (ordinary.plan.payload["ports"], ordinary.plan.payload["registers"])
        for item in table
    )


@pytest.mark.skipif(IVERILOG is None, reason="Icarus Verilog is required")
def test_initialized_strict_execution_matches_unchanged_direct_sv(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "strict_rtl_parity",
        """
        module StrictRtlParity {
          clock clk reset rst
          in load:bit in value:u8 out y:u8
          reg q:u8=0
          when load { q <- value }
          y=q
        }
        """,
    )
    program = zlang.sim.compile(
        source,
        top="StrictRtlParity",
        strict_uninitialized=True,
    )
    comparison = compare_program(
        program,
        (
            {"set": {"load": 1, "value": 11}, "edges": ("clk",)},
            {"set": {"load": 0, "value": 99}, "edges": ("clk",)},
        ),
        simulator="iverilog",
        artifact_directory=tmp_path / "comparison",
    )
    assert comparison.native_trace == comparison.rtl_trace
    assert comparison.native_outputs[-1] == {"y": 11}
