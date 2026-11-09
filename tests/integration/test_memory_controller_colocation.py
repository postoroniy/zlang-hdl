# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Independent snapshot/memory oracles with actual-RTL proofs."""

from hashlib import sha256
import json
from pathlib import Path

import pytest

from tests.support.memory_controller_cases import PROFILES, controller_source
from zlang import sim
from zlang.backend.identifiers import rtl_memory_cells_identifier
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.formal import run_verilog_formal
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.simulation_compare import compare_program


def _events_and_reference(profile):
    latency, collision, contents, read_data = profile
    cells, stages = [0] * 4, [0] * latency
    cursor = seen = 0
    events, expected = [], []

    def merge(old, data, mask):
        for bit in range(9):
            if mask & (1 << (bit // 8)):
                old = (old & ~(1 << bit)) | (data & (1 << bit))
        return old

    def q(values, reset):
        if latency:
            return stages[-1]
        if reset and read_data == "clear":
            return 0
        if not reset and collision == "write_first" and values["write_enable"] and values["address"] == cursor:
            return merge(cells[cursor], values["data"], values["mask"])
        return cells[values["address"]]

    def append(values, reset, edges):
        nonlocal cursor, seen, cells, stages
        if edges:
            old_q = q(values, reset)
            old_cursor = cursor
            if reset:
                cursor = seen = 0
                if contents == "clear":
                    cells = [0] * 4
                if read_data == "clear":
                    stages = [0] * latency
            else:
                if values["sample"]:
                    seen = old_q
                if values["step"]:
                    cursor = (cursor + 1) & 3
                word = cells[values["address"]]
                merged = merge(cells[old_cursor], values["data"], values["mask"])
                if collision == "write_first" and values["write_enable"] and values["address"] == old_cursor:
                    word = merged
                if latency:
                    stages = [word, *stages[:-1]]
                if values["write_enable"]:
                    cells[old_cursor] = merged
        events.append({"set": values, "reset": {"rst": reset}, "edges": edges})
        expected.append({"q": q(values, reset), "cursor": cursor, "seen": seen})

    append({"address": 0, "write_enable": 0, "step": 0, "sample": 0, "data": 0, "mask": 0}, True, ["clk"])
    for cycle in range(80):
        values = {"address": (cycle // 2) & 3, "write_enable": int(cycle % 4 != 3),
                  "step": int(cycle % 3 != 1), "sample": int(cycle % 4 == 2),
                  "data": (0, 511, 256, 127, 384, 19, 201)[cycle % 7], "mask": (cycle // 3) & 3}
        reset = cycle in (13, 32, 59)
        append(values, reset, [])
        append(values, reset, ["clk"])
    return events, tuple(expected)


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("simulator", ("iverilog", "verilator"))
@pytest.mark.parametrize("logic_state", (False, True))
def test_global_memory_controller_native_and_rtl_match_independent_snapshot_oracle(
    tmp_path, profile, simulator, logic_state,
):
    path = tmp_path / "controller.zhl"
    path.write_text(controller_source(*profile))
    events, expected = _events_and_reference(profile)
    compared = compare_program(
        sim.compile(path, logic_state=logic_state), events, simulator=simulator,
        artifact_directory=tmp_path / "comparison", build_timeout=60, timeout=30,
    )
    assert compared.native_trace == compared.rtl_trace == expected
    assert all(isinstance(value, int) for row in compared.rtl_trace for value in row.values())


@pytest.mark.parametrize("ported,parent", ((True, False), (True, True), (False, True)))
@pytest.mark.parametrize("simulator", ("iverilog", "verilator"))
def test_composed_and_named_port_memory_keep_controller_state(tmp_path, ported, parent, simulator):
    profile = (1, "write_first", "preserve", "clear")
    path = tmp_path / "controller.zhl"
    path.write_text(controller_source(*profile, ported=ported, parent=parent))
    events, expected = _events_and_reference(profile)
    compared = compare_program(
        sim.compile(path), events, simulator=simulator,
        artifact_directory=tmp_path / "comparison", build_timeout=60, timeout=30,
    )
    assert compared.native_trace == compared.rtl_trace == expected
    assert all(isinstance(value, int) for row in compared.rtl_trace for value in row.values())


@pytest.mark.parametrize("controller", (False, True))
def test_native_reset_preserves_every_read_stage_and_u_x_tags(tmp_path, controller):
    source = controller_source(2, read_data="preserve")
    if not controller:
        source = source.replace("out reg cursor:u2=0 out reg seen:u9=0", "")
        source = source.replace("when step {cursor <- truncate<2>(cursor+1)}", "")
        source = source.replace("when sample {seen <- bank.read_data}", "")
        source = source.replace("bank.write_address=cursor", "bank.write_address=address")
    path = tmp_path / "read_pipeline.zhl"
    path.write_text(source)
    zero = sim.LogicVector.known(0, 9)
    # An explicit store consumes input U as X under the existing native
    # contract. Reset must retain those resulting tags, not shift other data.
    unknown = sim.LogicVector.parse("x0x101010", 9)
    with sim.compile(path, logic_state=True).create() as machine:
        for name, value in {"address": 0, "write_enable": 0, "step": 0, "sample": 0, "data": 0, "mask": 3}.items():
            machine.set(name, value)
        machine.reset("rst", asserted=True)
        machine.edge("clk")
        machine.reset("rst", asserted=False)
        machine.set_logic("data", "u0x101010")
        machine.set("write_enable", 1)
        machine.edge("clk")
        machine.set("write_enable", 0)
        machine.edge("clk")
        assert machine.get_logic("q") == zero
        machine.reset("rst", asserted=True)
        machine.edge("clk")
        machine.edge("clk")
        assert machine.get_logic("q") == zero
        machine.reset("rst", asserted=False)
        machine.edge("clk")
        assert machine.get_logic("q") == unknown
        machine.set("address", 1)
        machine.edge("clk")
        assert machine.get_logic("q") == unknown
        machine.reset("rst", asserted=True)
        machine.edge("clk")
        assert machine.get_logic("q") == unknown


def _reference(profile, cells):
    latency, collision, contents, read_data = profile
    declaration = f"reg [8:0] reference_pipe[0:{latency-1}];" if latency else ""
    initialize = "initial begin for(integer i=0;i<4;i=i+1) reference_cells[i]=0;"
    if latency:
        initialize += f"for(integer i=0;i<{latency};i=i+1) reference_pipe[i]=0;"
    initialize += "end"
    clear_contents = "for(integer i=0;i<4;i=i+1) reference_cells[i]<=0;" if contents == "clear" else ""
    clear_read = (f"for(integer i=0;i<{latency};i=i+1) reference_pipe[i]<=0;"
                  if latency and read_data == "clear" else "")
    read_word = " : ".join(f"address==2'd{i} ? reference_cells[{i}]" for i in range(3)) + " : reference_cells[3]"
    write_word = " : ".join(f"reference_cursor==2'd{i} ? reference_cells[{i}]" for i in range(3)) + " : reference_cells[3]"
    word = "reference_read_word"
    if collision == "write_first":
        word = "((write_enable && address==reference_cursor) ? reference_merged : reference_read_word)"
    q = f"reference_pipe[{latency-1}]" if latency else word
    if not latency:
        q = q.replace("write_enable &&", "!rst && write_enable &&")
        if read_data == "clear":
            q = f"(rst ? 9'd0 : {q})"
    read_update = (f"reference_pipe[0]<={word}; for(integer i=1;i<{latency};i=i+1) "
                   "reference_pipe[i]<=reference_pipe[i-1];" if latency else "")
    assertions = ["assert(cursor==reference_cursor);", "assert(seen==reference_seen);", "assert(q==reference_q);"]
    assertions += [f"assert({cells}[{i}]==reference_cells[{i}]);" for i in range(4)]
    for i in range(latency):
        actual = f"bank_read_stage_{i}" if i < latency-1 else "bank_read_data"
        assertions.append(f"assert({actual}==reference_pipe[{i}]);")
    return f"""
      reg reference_started=0;
      reg [1:0] reference_cursor;
      reg [8:0] reference_seen, reference_cells[0:3];
      {declaration}
      wire [8:0] reference_mask={{mask[1],{{8{{mask[0]}}}}}};
      wire [8:0] reference_read_word={read_word};
      wire [8:0] reference_write_word={write_word};
      wire [8:0] reference_merged=(reference_write_word&~reference_mask)|(data&reference_mask);
      wire [8:0] reference_q={q};
      {initialize}
      always @(posedge clk) begin
        reference_started<=1; if(!reference_started) assume(rst);
        if(rst) begin
          reference_cursor<=0; reference_seen<=0;
          {clear_contents} {clear_read}
        end else begin
          if(step) reference_cursor<=reference_cursor+2'd1;
          if(sample) reference_seen<=reference_q;
          if(write_enable) for(integer i=0;i<4;i=i+1) if(reference_cursor==i)
            reference_cells[i]<=(reference_cells[i]&~reference_mask)|(data&reference_mask);
          {read_update}
        end
      end
      always @* if(reference_started) begin {' '.join(assertions)} end
    """, len(assertions)


def _prove(tmp_path, profile, mutation=None):
    source = controller_source(*profile)
    changes = {
        "post-edge-address": ("bank.write_address=cursor", "bank.write_address=truncate<2>(cursor+1)"),
        "dropped-global-write": ("bank.write_enable=write_enable", "bank.write_enable=write_enable & sample"),
        "lost-mask": ("bank.write_mask=mask", "bank.write_mask=3"),
        "wrong-collision": ("collision write_first", "collision read_first"),
        "wrong-reset": ("out reg cursor:u2=0", "out reg cursor:u2=1"),
        "lost-snapshot": ("seen <- bank.read_data", "seen <- data"),
    }
    if mutation in changes:
        before, after = changes[mutation]
        assert source.count(before) == 1
        source = source.replace(before, after)
    compiled = compile_source(source)
    artifact = emit_artifact(compiled.ir, selected_ir_identity=compiled.selected_ir_identity)
    assert len(compiled.ir.memories) == 1
    memory = compiled.ir.memories[0]
    assert memory.element_type.width == 9 and memory.depth == 4
    cells = rtl_memory_cells_identifier(memory.name)
    assert artifact.text.count(f"logic [8:0] {cells} [0:3];") == 1
    reference, assertion_count = _reference(profile, cells)
    if mutation == "assert-to-assume":
        reference = reference.replace("assert(", "assume(")
    if reference.count("assert(") != assertion_count:
        raise ValueError("reference assertion inventory changed")
    assert artifact.text.count("endmodule") == 1
    rtl = artifact.text.replace("endmodule", reference+"\nendmodule")
    (tmp_path / "actual-rtl-and-reference.sv").write_text(rtl)
    result = run_verilog_formal(
        rtl, top=artifact.module, property_id=f"memory-controller.{profile}.{mutation or 'positive'}",
        mode=ProofMode.PROVE if mutation is None else ProofMode.BMC,
        depth=8, timeout_seconds=30, route="smtbmc", solver="z3",
        systemverilog=True, flatten=True, work_directory=tmp_path / "proof",
    )
    (tmp_path / "qualification.json").write_text(json.dumps({
        "selected_ir_identity": compiled.selected_ir_identity,
        "artifact_hash": artifact.artifact_hash, "profile": profile, "mutation": mutation,
        "source_sha256": sha256(rtl.encode()).hexdigest(),
        "checker_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "assertion_count": assertion_count, "depth": 8, "timeout_seconds": 30,
        "status": result.status.value, "reason": result.reason,
    }, indent=2)+"\n")
    return result


@pytest.mark.parametrize("profile", PROFILES)
def test_memory_controller_independent_full_state_relation_is_proven(tmp_path, profile):
    result = _prove(tmp_path, profile)
    assert result.status is FormalStatus.PROVEN, (result.status, result.reason)


@pytest.mark.parametrize("mutation", (
    "post-edge-address", "dropped-global-write", "lost-mask", "wrong-collision", "wrong-reset", "lost-snapshot",
))
def test_memory_controller_mutations_have_actual_counterexamples(tmp_path, mutation):
    result = _prove(tmp_path, (1, "write_first", "preserve", "clear"), mutation)
    assert result.status is FormalStatus.FAILED and result.counterexample is not None


def test_memory_reference_assertions_cannot_become_assumptions(tmp_path):
    with pytest.raises(ValueError, match="assertion inventory changed"):
        _prove(tmp_path, (1, "write_first", "preserve", "clear"), "assert-to-assume")
