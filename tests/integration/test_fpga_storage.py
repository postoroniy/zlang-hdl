# SPDX-License-Identifier: Apache-2.0
"""Independent RAM transition scoreboards for actual selected block-style RTL."""

from pathlib import Path
import pytest
from zlang.backend.systemverilog import emit_target_artifact
from zlang.backend.identifiers import rtl_memory_cells_identifier
from zlang.compiler import compile_source
from zlang.formal import run_verilog_formal
from zlang.ir.formal import ProofMode, FormalStatus
from zlang.target_catalog import TargetArchitectureError

ROOT = Path(__file__).resolve().parents[2]
FAMILIES = (("Gowin", "gw1nr_lv9qn88pc6"), ("Ecp5", "lfe5u_25f_8mg285c"))
SOURCE = ROOT / "examples/fpga/architectures/storage.zhl"


def ram_reference_source(family, kind):
    text = (
        SOURCE.read_text().replace("u8", "u2").replace("u16", "u8").replace("256", "4")
    )
    r = compile_source(
        text,
        top=kind,
        target=family[1],
        architecture=family[0] + "BRAM",
        architecture_mode="required",
    )
    artifact = emit_target_artifact(r.ir, r.implementation_graph)
    cell = rtl_memory_cells_identifier(r.ir.memories[0].name)
    monitor = """
reg [7:0] oracle[0:3];reg started=0;
initial begin for(integer i=0;i<4;i=i+1) oracle[i]=0;end
"""
    if "TrueDual" in kind:
        monitor += """reg [7:0] qa,qb;
wire bw=b_we && !(a_we && a_addr==b_addr);
wire ac=(a_we || (bw && b_addr==a_addr));
wire bc=(bw || (a_we && a_addr==b_addr));
always @(posedge clk) begin
 if(!started) assume(rst);started<=1;
 if(rst) begin qa<=0;qb<=0;end else begin
  if(a_we) oracle[a_addr]<=a_data;
  if(bw) oracle[b_addr]<=b_data;
"""
        if "NoChange" in kind:
            monitor += "if(a_re && !ac) qa<=oracle[a_addr];if(b_re && !bc) qb<=oracle[b_addr];\n"
        else:
            monitor += """if(a_re) qa<=a_we ? a_data : ((bw && b_addr==a_addr) ? b_data : oracle[a_addr]);
if(b_re) qb<=(a_we && a_addr==b_addr) ? a_data : (bw ? b_data : oracle[b_addr]);
"""
        monitor += "end\nif(started) begin assert(a_q==qa);assert(b_q==qb);end\nend\n"
    else:
        ra = "wa" if kind == "RamReadFirst" else "ra"
        value = "oracle[" + ra + "]"
        if kind == "RamWriteFirst":
            value = "((we && wa==ra) ? data : oracle[ra])"
        control = "if(!(we && wa==ra)) " if kind == "RamNoChange" else ""
        monitor += f"""reg [7:0] expected;
always @(posedge clk) begin
 if(!started) assume(rst);started<=1;
 if(rst) expected<=0;else begin
  if(we) oracle[wa]<=data;
  {control}expected<={value};
 end
 if(started) assert(q==expected);
end
"""
    monitor += f"always @* if(started) for(integer i=0;i<4;i=i+1) assert({cell}[i]==oracle[i]);\n"
    sv = artifact.text.replace("endmodule", monitor + "\nendmodule", 1)
    return sv, artifact


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize(
    "kind",
    (
        "RamReadFirst",
        "RamWriteFirst",
        "RamNoChange",
        "RamTrueDual",
        "RamTrueDualNoChange",
    ),
)
def test_selected_ram_matches_independent_transition_scoreboard(family, kind, tmp_path):
    sv, artifact = ram_reference_source(family, kind)
    proof = run_verilog_formal(
        sv,
        top=artifact.module,
        property_id=kind,
        mode=ProofMode.PROVE,
        depth=8,
        timeout_seconds=30,
        systemverilog=True,
        flatten=True,
        work_directory=tmp_path,
    )
    assert proof.status is FormalStatus.PROVEN, proof
    assert 'ram_style = "block"' in artifact.text


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("violation", ("clear", "cross_read_first", "mask", "async"))
def test_ram_unsupported_policies_fail_closed(family, violation):
    text = """module Top { clock clk reset rst in ra,wa:u2 in we:bit in data:u8 out q:u8
memory bank:mem<u8,4> { read_latency 1 collision write_first reset { contents preserve read_data clear } }
bank.read_address=ra bank.write_address=wa bank.write_enable=we bank.write_data=data q=bank.read_data }"""
    if violation == "clear":
        text = text.replace("contents preserve", "contents clear")
    if violation == "cross_read_first":
        text = text.replace("write_first", "read_first")
    if violation == "mask":
        text = text.replace("q=bank.read_data", "bank.write_mask=0 q=bank.read_data")
    if violation == "async":
        text = text.replace("read_latency 1", "read_latency 0")
    with pytest.raises((TargetArchitectureError, ValueError)):
        compile_source(
            text,
            target=family[1],
            architecture=family[0] + "BRAM",
            architecture_mode="required",
        )


def fifo_reference_source(family):
    from zlang.async_fifo import build_async_fifo_physical_plan

    text = SOURCE.read_text().replace("rv<u8>", "rv<u2>")
    result = compile_source(
        text,
        top="AsyncFifoStorage",
        target=family[1],
        architecture=family[0] + "AsyncFifoBRAM",
        architecture_mode="required",
    )
    artifact = emit_target_artifact(result.ir, result.implementation_graph)
    plan = build_async_fifo_physical_plan(result.ir, result.ir.connections[0])
    roles = {r.role: r.name for r in plan.registers}
    monitor = (ROOT / "tests/fixtures/fpga_fifo_reference.sv").read_text()
    mapping = {
        "OBS_W": "zlang_write_binary",
        "OBS_R": "zlang_read_binary",
        "OBS_WG": "zlang_write_gray",
        "OBS_RG": "zlang_read_gray",
        "OBS_RS1": "zlang_read_gray_sync1",
        "OBS_RS2": "zlang_read_gray_sync2",
        "OBS_WS1": "zlang_write_gray_sync1",
        "OBS_WS2": "zlang_write_gray_sync2",
        "OBS_FULL": "zlang_full",
        "OBS_VALID": "zlang_output_valid",
    }
    for token, role in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        monitor = monitor.replace(token, roles[role])
    monitor = monitor.replace(
        "OBS_CELLS", rtl_memory_cells_identifier(plan.memory.name)
    )
    writer, reader = plan.memory.ports
    write_enable, read_enable = writer.write_enable.name, reader.read_enable.name
    write_address, read_address = writer.address.name, reader.address.name
    for signal in (write_enable, read_enable, write_address, read_address):
        assert signal in artifact.text
    monitor += (
        f"always @* if(started && !source_reset && !destination_reset && "
        f"{write_enable} && {read_enable}) assert({write_address}!={read_address});\n"
    )
    return artifact.text.replace("endmodule", monitor + "\nendmodule", 1)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("independent", (False, True))
def test_fifo_selected_storage_queue_stalls_wrap_and_reset_epochs(
    family, independent, tmp_path
):
    source = fifo_reference_source(family)
    instance = """AsyncFifoStorage dut(.source_clock(sc),.destination_clock(dc),.source_reset(rst),.destination_reset(rst),
 .source_payload(data),.source_valid(valid),.source_ready(sr),.destination_payload(dp),.destination_valid(dv),.destination_ready(ready));"""
    wires = "wire sr,dv;wire[1:0] dp;"
    if not independent:
        source += (
            "module top(input clk,rst,input [1:0] data,input valid,ready);wire sc=clk,dc=clk;"
            + wires
            + instance
            + "endmodule\n"
        )
        proof = run_verilog_formal(
            source,
            top="top",
            property_id="fifo-queue",
            mode=ProofMode.PROVE,
            depth=8,
            timeout_seconds=30,
            systemverilog=True,
            flatten=True,
            work_directory=tmp_path,
        )
        assert proof.status is FormalStatus.PROVEN, proof
        return
    # Every requested reset epoch delivers a reset edge to both domains before
    # resuming arbitrary independent clocks/pauses. No unilateral-reset claim.
    source += (
        """module top(input gclk,sclk,dclk,request_reset,input [1:0] data,input valid,ready);
reg [1:0] phase=0;
always @(posedge gclk) if(phase!=3) phase<=phase+1;else if(request_reset) phase<=0;
wire rst=phase<2;
wire sc=phase==1 ? 1'b1 : phase==3 ? sclk : 1'b0;
wire dc=phase==1 ? 1'b1 : phase==3 ? dclk : 1'b0;
"""
        + wires
        + instance
        + "endmodule\n"
    )
    from zlang.formal import resolve_formal_toolchain
    from zlang.formal_execution_tools import execution_environment
    from zlang.common.subprocess import run_bounded_process, subprocess_text
    import subprocess

    tools = resolve_formal_toolchain(engine="sby", solver="z3")
    (tmp_path / "top.sv").write_text(source)
    (tmp_path / "top.sby").write_text("""[options]
mode prove
depth 8
multiclock on
[engines]
smtbmc z3
[script]
read_verilog -sv -formal top.sv
prep -top top -flatten
[files]
top.sv
""")
    with execution_environment(tools.execution_binding) as env:
        try:
            result = run_bounded_process(
                ["sby", "-f", "top.sby"],
                cwd=tmp_path,
                env=env,
                timeout=30,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as error:
            (tmp_path / "timeout.stdout.log").write_text(subprocess_text(error.stdout))
            (tmp_path / "timeout.stderr.log").write_text(subprocess_text(error.stderr))
            raise
    (tmp_path / "stdout.log").write_text(result.stdout)
    (tmp_path / "stderr.log").write_text(result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "top/status").read_text().split()[0] == "PASS"


@pytest.mark.parametrize("family", FAMILIES)
def test_legacy_no_change_native_selected_rtl_and_preserved_reset(
    family, tmp_path, monkeypatch
):
    from zlang import sim
    import zlang.simulation_compare as comparison

    source = """module Top { clock clk reset rst in ra,wa:u2 in we:bit in data:u8 out q:u8
memory bank:mem<u8,4> { read_latency 1 collision no_change reset { contents preserve read_data clear } }
bank.read_address=ra bank.write_address=wa bank.write_enable=we bank.write_data=data q=bank.read_data }"""
    path = tmp_path / "no_change.zhl"
    path.write_text(source)
    r = compile_source(
        source,
        target=family[1],
        architecture=family[0] + "BRAM",
        architecture_mode="required",
    )
    artifact = emit_target_artifact(r.ir, r.implementation_graph)
    monkeypatch.setattr(comparison, "emit_artifact", lambda *a, **kw: artifact)
    settings = (
        (0, 0, 0, 0, True),
        (0, 0, 1, 9, False),
        (0, 1, 1, 5, False),
        (1, 1, 1, 7, False),
        (1, 1, 0, 0, False),
        (1, 1, 1, 0, True),
        (1, 1, 0, 0, False),
    )
    events = [
        {
            "set": {"ra": ra, "wa": wa, "we": we, "data": data},
            "reset": {"rst": rst},
            "edges": ["clk"],
        }
        for ra, wa, we, data, rst in settings
    ]
    result = comparison.compare_program(
        sim.compile(path, top="Top"),
        events,
        simulator="iverilog",
        artifact_directory=tmp_path / "comparison",
        timeout=120,
    )
    assert result.native_trace == result.rtl_trace
    assert [x["q"] for x in result.rtl_trace] == [0, 0, 9, 9, 7, 0, 7]


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("mutation", ("pointer", "gray", "prefetch", "full"))
def test_fifo_actual_rtl_mutations_have_counterexamples(family, mutation, tmp_path):
    source = fifo_reference_source(family)
    before, after = {
        "pointer": ("3'(zlang_pop)", "3'd1"),
        "gray": (
            "((zlang_write_binary_next >> 1) ^ zlang_write_binary_next)",
            "zlang_write_binary_next",
        ),
        "prefetch": (
            "assign zlang_fifo_fetch_address = zlang_read_binary_next[1:0];",
            "assign zlang_fifo_fetch_address = zlang_read_binary[1:0];",
        ),
        "full": ("^ 3'd6", "^ 3'd2"),
    }[mutation]
    assert before in source
    source = (
        source.replace(before, after, 1)
        + """module top(input clk,rst,input [1:0] data,input valid,ready);
wire sr,dv;wire [1:0] dp;
AsyncFifoStorage dut(.source_clock(clk),.destination_clock(clk),.source_reset(rst),.destination_reset(rst),
 .source_payload(data),.source_valid(valid),.source_ready(sr),.destination_payload(dp),.destination_valid(dv),.destination_ready(ready));
endmodule
"""
    )
    result = run_verilog_formal(
        source,
        top="top",
        property_id=mutation,
        mode=ProofMode.PROVE,
        depth=8,
        timeout_seconds=30,
        systemverilog=True,
        flatten=True,
        work_directory=tmp_path,
    )
    assert result.status is FormalStatus.FAILED, result


@pytest.mark.parametrize("family", FAMILIES)
def test_ram_collision_mode_mutation_has_counterexample(family, tmp_path):
    source, artifact = ram_reference_source(family, "RamNoChange")
    before = "if (!(we && (ra == wa)))"
    assert before in source
    source = source.replace(before, "if (1'b1)", 1)
    proof = run_verilog_formal(
        source,
        top=artifact.module,
        property_id="collision-mode",
        mode=ProofMode.PROVE,
        depth=8,
        timeout_seconds=30,
        systemverilog=True,
        flatten=True,
        work_directory=tmp_path,
    )
    assert proof.status is FormalStatus.FAILED, proof
