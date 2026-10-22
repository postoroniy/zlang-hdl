"""Native plans retain state-write controls for resettable source registers."""

import pytest

from zlang import sim


SOURCE = """module Top {
 clock clk reset rst
 in load:bit in value:u2
 out done:bit
 reg count:u2=0
 when load { count <- value }
 done=count==2
}
"""


@pytest.mark.parametrize("strict", (False, True))
@pytest.mark.parametrize("tag", ("U", "X"))
def test_resettable_unknown_write_enable_rolls_back_even_when_output_is_known(
    tmp_path, strict, tag
):
    path = tmp_path / "top.zhl"
    path.write_text(SOURCE)
    with sim.compile(
        path, logic_state=True, strict_uninitialized=strict
    ).create() as machine:
        machine.set("load", 0)
        machine.set("value", 1)
        machine.reset("rst", asserted=True)
        machine.tick("clk")
        machine.reset("rst", asserted=False)
        machine.enable_trace(("count",))
        machine.drain_logic_trace()
        machine.set_logic("load", tag)
        with pytest.raises(
            sim.SimulationRuntimeError,
            match="write condition.*stateful control must be binary",
        ):
            machine.tick("clk")
        assert machine.get_logic("count").require_binary() == 0
        assert machine.get("done") == 0
        assert machine.drain_logic_trace() == []
        machine.set("load", 1)
        machine.tick("clk")
        assert machine.get("count") == 1


@pytest.mark.parametrize("mode", ("logic", "strict", "binary"))
def test_reset_overrides_unknown_write_control_and_false_guard_holds(tmp_path, mode):
    path = tmp_path / "top.zhl"
    path.write_text(SOURCE)
    with sim.compile(
        path, logic_state=mode != "binary", strict_uninitialized=mode == "strict"
    ).create() as machine:
        machine.set("value", 3)
        if mode == "binary":
            machine.set("load", 1)
        else:
            machine.set_logic("load", "U")
        machine.reset("rst", asserted=True)
        machine.tick("clk")
        assert machine.get("count") == 0
        machine.set("load", 0)
        machine.reset("rst", asserted=False)
        machine.tick("clk")
        assert machine.get("count") == 0
        machine.set("load", 1)
        machine.tick("clk")
        assert machine.get("count") == 3


def test_resettable_initial_override_is_not_refreshed_by_a_non_write(tmp_path):
    path = tmp_path / "top.zhl"
    path.write_text(SOURCE)
    with sim.compile(path, logic_state=True).create(
        initial_registers={"count": "UU"}
    ) as machine:
        machine.set("value", 1)
        machine.set("load", 0)
        machine.tick("clk")
        assert machine.get_logic("count").to_bits() == "uu"
        machine.set("load", 1)
        machine.tick("clk")
        assert machine.get("count") == 1


def test_hierarchical_resettable_guard_is_not_lost(tmp_path):
    path = tmp_path / "top.zhl"
    path.write_text(
        SOURCE.replace("module Top", "module Cell")
        + """
module Top {
 clock clk reset rst in load:bit in value:u2 out done:bit
 child:Cell child.load=load child.value=value done=child.done
}
"""
    )
    with sim.compile(path, top="Top", logic_state=True).create() as machine:
        machine.set("load", 0)
        machine.set("value", 1)
        machine.reset("rst", asserted=True)
        machine.tick("clk")
        machine.reset("rst", asserted=False)
        machine.set_logic("load", "X")
        with pytest.raises(sim.SimulationRuntimeError, match="write condition"):
            machine.tick("clk")
        assert machine.get_logic("child.count").require_binary() == 0


def test_resettable_known_controls_match_actual_direct_sv(tmp_path):
    from tests.simulation.differential import run_differential

    path = tmp_path / "top.zhl"
    path.write_text(SOURCE)
    events = [
        {
            "edges": ["clk"],
            "reset": {"rst": i in (0, 11, 23)},
            "set": {"load": int(i % 3 != 0), "value": i % 4},
        }
        for i in range(36)
    ]
    traces = run_differential(path, top="Top", directory=tmp_path, events=events)
    assert traces.native == traces.direct_sv
