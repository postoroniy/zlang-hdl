from dataclasses import replace
import shutil

import pytest

from zlang.backend.systemverilog import emit_formal_artifact
from zlang.compiler import compile_source
from zlang.formal import (
    build_recursive_formal_design,
    connect_formal_design,
    emit_cover_harness,
    run_verilog_cover,
)
from zlang.ir.formal import (
    CoverProperty,
    CoverStatus,
    cover_harness_top,
)
from zlang.ir.formal_predicates import (
    Binary,
    Constant,
    FormalBinaryOperator,
    FormalSignedness,
    ObservationRef,
)


TOOLS = ("yosys", "sby", "yosys-smtbmc", "z3")


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in TOOLS),
    reason="Yosys, SymbiYosys, yosys-smtbmc, and Z3 are required",
)
def test_real_sby_cover_reached_and_bounded_unreached(tmp_path) -> None:
    reached_work = tmp_path / "reached-work"
    reached = run_verilog_cover(
        """module CoverReached(input wire clock);
  reg [1:0] count = 0;
  always @(posedge clock) begin
    count <= count + 1;
    cover(count == 2);
  end
endmodule
""",
        top="CoverReached",
        property_id="cover.reached",
        depth=5,
        work_directory=reached_work,
    )
    assert reached.status is CoverStatus.WITNESSED
    assert reached.witness is not None
    assert reached.witness.cycle == 3
    assert reached.witness.raw_trace
    assert (reached_work / "solver.stdout.log").is_file()
    assert tuple(reached_work.glob("CoverReached/engine_0/trace*.vcd"))

    unreached_work = tmp_path / "unreached-work"
    unreached = run_verilog_cover(
        """module CoverUnreached(input wire clock);
  always @(posedge clock) cover(1'b0);
endmodule
""",
        top="CoverUnreached",
        property_id="cover.unreached",
        depth=4,
        work_directory=unreached_work,
    )
    assert unreached.status is CoverStatus.BOUNDED_UNREACHED
    assert unreached.witness is None
    assert "depth 4" in (unreached.reason or "")
    assert (unreached_work / "solver.stdout.log").is_file()


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in TOOLS),
    reason="Yosys, SymbiYosys, yosys-smtbmc, and Z3 are required",
)
def test_cover_only_observation_connects_through_backend_artifact() -> None:
    compiled = compile_source("""
module ConnectedCoverCounter {
    clock clk
    reset rst
    out y : u8
    reg count : u8 = 0
    count <- truncate<8>(count + 1)
    y = count
}
""")
    predicate = Binary(
        FormalBinaryOperator.EQUAL,
        ObservationRef("register:count", 8, FormalSignedness.UNSIGNED),
        Constant(2, 8, FormalSignedness.UNSIGNED),
        1,
        FormalSignedness.BIT,
    )
    cover = CoverProperty(
        "cover.connected.count.two", "clk", "rst", "count == 2", predicate,
    )
    semantic = replace(compiled.formal_design, covers=(cover,))
    artifact = emit_formal_artifact(
        compiled.ir, build_recursive_formal_design(compiled.ir)
    )
    connected = connect_formal_design(semantic, artifact)
    connected_cover = connected.covers[0]
    assert connected_cover.non_executable_reason is None
    assert any(
        item.semantic_signal_id == "register:count"
        and item.rtl_name.startswith("zlang_formal_obs_")
        for item in connected.bindings
    )
    top = cover_harness_top(connected, cover.id)
    result = run_verilog_cover(
        emit_cover_harness(connected, cover_id=cover.id, depth=6),
        top=top,
        property_id=cover.id,
        depth=6,
        systemverilog=True,
    )
    assert result.status is CoverStatus.WITNESSED
    assert result.witness is not None
    assert result.witness.cycle >= 2
