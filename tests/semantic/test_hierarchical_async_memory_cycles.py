from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from zlang import sim
from zlang.compiler import compile_file, compile_source
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.simulation_compare import compare_program


ROOT = Path(__file__).resolve().parents[2]


def _memory_child(*, latency: int, collision: str) -> str:
    return f"""
module AsyncMem {{
    clock clk reset rst
    in ra:u2 in we:bit in wa:u2 in wd:u8 out q:u8
    memory m:mem<u8,4> {{
        read_latency {latency}
        collision {collision}
        reset {{ contents preserve read_data preserve }}
    }}
    m.read_address=ra
    m.write_enable=we
    m.write_address=wa
    m.write_data=wd
    q=m.read_data
}}
"""


def test_latency_zero_child_dependency_cycles_are_rejected_exactly() -> None:
    self_address = _memory_child(latency=0, collision="read_first") + """
module Loop {
    clock clk reset rst out q:u8
    c:AsyncMem { ra=truncate<2>(c.q) we=0 wa=0 wd=0 }
    q=c.q
}
"""
    with pytest.raises(
        SemanticError,
        match=r"combinational child dependency cycle: c\.q -> c\.q",
    ):
        compile_source(self_address, top="Loop")

    two_children = _memory_child(latency=0, collision="read_first") + """
module PairLoop {
    clock clk reset rst out q:u8
    a:AsyncMem { ra=truncate<2>(b.q) we=0 wa=0 wd=0 }
    b:AsyncMem { ra=truncate<2>(a.q) we=0 wa=0 wd=0 }
    q=a.q
}
"""
    with pytest.raises(SemanticError, match="combinational child dependency cycle"):
        compile_source(two_children, top="PairLoop")

    write_first = _memory_child(latency=0, collision="write_first") + """
module WriteLoop {
    clock clk reset rst out q:u8
    c:AsyncMem { ra=0 we=1 wa=0 wd=c.q }
    q=c.q
}
"""
    with pytest.raises(
        SemanticError,
        match=r"combinational child dependency cycle: c\.q -> c\.q",
    ):
        compile_source(write_first, top="WriteLoop")


def test_sequential_and_read_first_next_edge_feedback_remain_legal() -> None:
    read_first = _memory_child(latency=0, collision="read_first") + """
module ReadFirstFeedback {
    clock clk reset rst out q:u8
    c:AsyncMem { ra=0 we=1 wa=0 wd=c.q }
    q=c.q
}
"""
    compile_source(read_first, top="ReadFirstFeedback")

    latency_one = _memory_child(latency=1, collision="write_first") + """
module RegisteredReadFeedback {
    clock clk reset rst out q:u8
    c:AsyncMem { ra=truncate<2>(c.q) we=0 wa=0 wd=c.q }
    q=c.q
}
"""
    compile_source(latency_one, top="RegisteredReadFeedback")

    banked = (ROOT / "examples" / "ztpu_banked_memory.zhl").read_text()
    compile_source(banked, top="ZtpuBankedMemory")


def test_latency_zero_dependencies_are_transitive_through_nested_children() -> None:
    source = _memory_child(latency=0, collision="read_first") + """
module Mid {
    clock clk reset rst in address:u2 out q:u8
    leaf:AsyncMem { ra=address we=0 wa=0 wd=0 }
    q=leaf.q
}
module NestedLoop {
    clock clk reset rst out q:u8
    mid:Mid { address=truncate<2>(mid.q) }
    q=mid.q
}
"""
    with pytest.raises(
        SemanticError,
        match=r"combinational child dependency cycle: mid\.q -> mid\.q",
    ):
        compile_source(source, top="NestedLoop")


def test_rule_driven_child_output_dependencies_are_not_hidden() -> None:
    source = """
module RuleEcho {
    clock clk reset rst in x:bit out y:bit
    rule emit when x { drive y = x }
}
module RuleLoop {
    clock clk reset rst out y:bit
    child:RuleEcho { x=child.y }
    y=child.y
}
"""
    with pytest.raises(
        SemanticError,
        match=r"combinational child dependency cycle: child\.y -> child\.y",
    ):
        compile_source(source, top="RuleLoop")


def test_rule_output_dependencies_include_the_exact_resolved_schedule() -> None:
    conflicting = """
module ScheduledOutput {
    clock clk reset rst in x:bit out y:bit
    reg shared:bit=0
    rule high when x { shared <- 0 }
    rule emit when 1 { shared <- 1 drive y = 1 }
    priority high > emit
}
module ScheduledLoop {
    clock clk reset rst out y:bit
    child:ScheduledOutput { x=child.y }
    y=child.y
}
"""
    with pytest.raises(
        SemanticError,
        match=r"combinational child dependency cycle: child\.y -> child\.y",
    ):
        compile_source(conflicting, top="ScheduledLoop")

    unrelated = """
module IndependentOutput {
    clock clk reset rst in x:bit out y:bit
    reg left:bit=0 reg right:bit=0
    rule high when x { left <- 1 }
    rule emit when 1 { right <- 1 drive y = 1 }
    priority high > emit
}
module IndependentFeedback {
    clock clk reset rst out y:bit
    child:IndependentOutput { x=child.y }
    y=child.y
}
"""
    compile_source(unrelated, top="IndependentFeedback")


@pytest.mark.parametrize(
    ("signal", "push", "pop"),
    (
        ("ready", "1", "drive"),
        ("overflow", "drive", "0"),
        ("overflow", "1", "drive"),
        ("underflow", "0", "drive"),
    ),
)
def test_legacy_fifo_combinational_observation_dependencies_are_not_hidden(
    signal: str,
    push: str,
    pop: str,
) -> None:
    source = f"""
module LegacyFifoObservation {{
    clock clk reset rst in drive:bit out observed:bit
    fifo q:fifo<u8,1>
    q.data=0
    q.push={push}
    q.pop={pop}
    observed=q.{signal}
}}
module FifoLoop {{
    clock clk reset rst out ready:bit
    child:LegacyFifoObservation {{ drive=child.observed }}
    ready=child.observed
}}
"""
    with pytest.raises(
        SemanticError,
        match=(
            r"combinational child dependency cycle: "
            r"child\.observed -> child\.observed"
        ),
    ):
        compile_source(source, top="FifoLoop")


def test_fifo_state_and_scheduled_observations_remain_cycle_cuts(tmp_path: Path) -> None:
    legacy_state = """
module LegacyFifoValid {
    clock clk reset rst in pop:bit out valid:bit
    fifo q:fifo<u8,1>
    q.data=0
    q.push=1
    q.pop=pop
    valid=q.valid
}
module LegacyStateFeedback {
    clock clk reset rst out valid:bit
    child:LegacyFifoValid { pop=child.valid }
    valid=child.valid
}
"""
    compile_source(legacy_state, top="LegacyStateFeedback")

    scheduled = """
module ScheduledFifoReady {
    clock clk reset rst in push:bit out ready:bit
    fifo q:fifo<u8,1>
    rule fill when push { q.push(0) }
    ready=q.ready
}
module ScheduledStateFeedback {
    clock clk reset rst out ready:bit
    child:ScheduledFifoReady { push=child.ready }
    ready=child.ready
}
"""
    compile_source(scheduled, top="ScheduledStateFeedback")
    _check_parent_protocol_storage_dependencies(tmp_path)


@pytest.mark.parametrize(
    ("source", "cycle", "control", "location"),
    (
        (
            """
module Top {
    clock clk reset rst in pushing:bit out room:bit
    fifo f:fifo<u8,2>
    f.push=pushing f.data=1 f.pop=~f.ready
    room=f.ready
}
""",
            r"combinational FIFO dependency cycle: f\.ready -> f\.ready",
            ("~f.ready", "~f.valid"),
            "f.pop=~f.ready",
        ),
        (
            """
module Top {
    clock clk reset rst in wr:bit out seen:bit out room:bit
    fifo f:fifo<u8,2>
    memory m:mem<bit,2> {read_port r write_port w read_latency 0 collision write_first}
    f.push=1 f.data=1 f.pop=m.r.read_data
    m.r.address=0 m.w.address=0 m.w.enable=wr m.w.data=~f.ready
    seen=m.r.read_data room=f.ready
}
""",
            r"combinational storage dependency cycle: m\.r\.read_data -> f\.ready -> m\.r\.read_data",
            ("read_latency 0", "read_latency 1"),
            "f.pop=m.r.read_data",
        ),
        (
            """
module Stage<W> { in x:uint<W> out y:uint<W> out k:uint<W> y=~x k=0 }
module Top {
    clock clk reset rst in wr:bit out q:u8
    memory m:mem<u8,2> {read_port r write_port w read_latency 0 collision write_first}
    stage:Stage<8> {x=m.r.read_data}
    m.r.address=0 m.w.address=0 m.w.enable=wr m.w.data=stage.y
    q=m.r.read_data
}
""",
            r"combinational storage dependency cycle: stage\.y -> stage\.x -> m\.r\.read_data -> stage\.y",
            ("m.w.data=stage.y", "m.w.data=stage.k"),
            "m.w.data=stage.y",
        ),
        (
            """
module Bank {
    clock clk reset rst in a:u1 out q:u1
    memory m:mem<u1,2> {read_port r read_latency 0 collision read_first}
    m.r.address=a
    q=m.r.read_data
}
module Top {
    clock clk reset rst out q:u1
    bank:Bank {a=bank.q}
    q=bank.q
}
""",
            r"combinational child dependency cycle: bank\.q -> bank\.q",
            ("a=bank.q", "a=0"),
            "bank.q",
        ),
    ),
    ids=("fifo-ready", "fifo-ram", "ram-child", "named-ram-in-child"),
)
def test_combinational_storage_and_child_feedback_is_rejected_before_lowering(
    tmp_path: Path, source: str, cycle: str, control: tuple[str, str], location: str,
) -> None:
    path = tmp_path / "feedback.zhl"
    path.write_text(source)

    def analyze_source(path: Path) -> None:
        analyze(parse(path.read_text()), source_unit=path.name)

    for compile_design in (analyze_source, compile_file, sim.compile):
        with pytest.raises(SemanticError, match=cycle) as error:
            compile_design(path)
        origin = error.value.primary
        assert origin is not None
        assert origin.source_unit == path.name
        assert location in source.splitlines()[origin.span.start_line - 1]

    path.write_text(source.replace(*control))
    for compile_design in (analyze_source, compile_file, sim.compile):
        compile_design(path)


@pytest.mark.parametrize(
    ("child", "bindings", "write_data", "path_tail", "control"),
    (
        (
            "module Cell {in x:u8 out tx:rv<u8> tx.payload=~x tx.valid=1}",
            "stage:Cell {x=m.r.read_data} stage.tx.ready=1",
            "stage.tx.payload",
            "stage.tx.payload",
            "mux(stage.tx.valid, 1, 0)",
        ),
        (
            """
module Cell {
    clock clk reset rst in live:u8
    csr registers @0 { V @0 { value u8 @7:0 ro <- live } }
}
""",
            "stage:Cell {live=~m.r.read_data addr=0 read=1 write=0 wdata=0}",
            "truncate<8>(stage.rdata)",
            "stage.rdata",
            None,
        ),
        (
            """
module Cell {
    clock clk reset rst in live:u8
    csr registers @0 { V @0 { value u8 @7:0 ro <- live } }
}
""",
            "stage:Cell {live=~m.r.read_data addr=0 read=0 write=0 wdata=0}",
            "stage.registers.status.V.value",
            "stage.registers.status.V.value",
            None,
        ),
        (
            """
module Cell {
    clock clk reset rst
    csr registers @0 { V @0 { value u8 @7:0 ro = 255 } }
}
""",
            "stage:Cell {addr=extend<32>(m.r.read_data) read=1 write=0 wdata=0}",
            "truncate<8>(stage.rdata)",
            "stage.rdata",
            None,
        ),
        (
            """
module Cell {
    clock clk reset rst
    csr registers @0 { V @0 { value u8 @7:0 rw = 0 } }
}
""",
            "stage:Cell {addr=0 read=0 write=0 wdata=extend<32>(~m.r.read_data)}",
            "stage.registers.events.V.value.write_value",
            "stage.registers.events.V.value.write_value",
            None,
        ),
    ),
    ids=("rv-payload", "csr-rdata", "csr-status", "csr-address-decode", "csr-write-value"),
)
def test_child_protocol_and_csr_feedback_is_rejected_before_lowering(
    tmp_path: Path,
    child: str,
    bindings: str,
    write_data: str,
    path_tail: str,
    control: str | None,
) -> None:
    source = child + f"""
module Top {{
    clock clk reset rst in wr:bit out q:u8
    memory m:mem<u8,2> {{read_port r write_port w read_latency 0 collision write_first}}
    {bindings}
    m.r.address=0 m.w.address=0 m.w.enable=wr m.w.data={write_data}
    q=m.r.read_data
}}
"""
    path = tmp_path / "feedback.zhl"
    path.write_text(source)

    def analyze_source(path: Path) -> None:
        analyze(parse(path.read_text()), source_unit=path.name)

    for compile_design in (analyze_source, compile_file, sim.compile):
        with pytest.raises(SemanticError, match="combinational storage dependency cycle:") as caught:
            compile_design(path)
        error = caught.value
        assert path_tail in str(error)
        assert "m.r.read_data" in str(error)
        assert error.primary is not None
        assert error.primary.source_unit == path.name
        assert any(
            name in source.splitlines()[error.primary.span.start_line - 1]
            for name in ("stage:Cell", "m.w.data=")
        )

    legal = (
        source.replace(f"m.w.data={write_data}", f"m.w.data={control}")
        if control is not None else source.replace("read_latency 0", "read_latency 1")
    )
    path.write_text(legal)
    for compile_design in (analyze_source, compile_file, sim.compile):
        compile_design(path)


def test_child_ready_feedback_uses_the_input_endpoint_ready_leaf(tmp_path: Path) -> None:
    path = tmp_path / "ready_feedback.zhl"
    path.write_text("""
module Cell { in x:bit in rx:rv<u8> rx.ready=~x }
module Top {
    clock clk reset rst in wr:bit out q:bit
    memory m:mem<bit,2> {read_port r write_port w read_latency 0 collision write_first}
    stage:Cell {x=m.r.read_data} stage.rx.payload=0 stage.rx.valid=1
    m.r.address=0 m.w.address=0 m.w.enable=wr m.w.data=stage.rx.ready
    q=m.r.read_data
}
""")
    for compile_design in (compile_file, sim.compile):
        with pytest.raises(
            SemanticError,
            match=r"stage\.rx\.ready -> stage\.x -> m\.r\.read_data -> stage\.rx\.ready",
        ):
            compile_design(path)


@pytest.mark.parametrize(
    ("signal", "buffered", "offered", "ready", "data", "expected"),
    (
        ("payload", False, "~m.r.read_data", "1", "stage.tx.payload", 255),
        ("valid", False, "0", "1", "mux(stage.tx.valid,1,0)", 1),
        ("ready", False, "0", "~m.r.read_data[0]", "mux(stage.rx.ready,1,0)", 1),
        ("ready", True, "0", "~m.r.read_data[0]", "mux(stage.rx.ready,1,0)", 1),
    ),
    ids=("nested-payload", "nested-valid", "nested-ready", "buffer-ready"),
)
def test_ready_valid_links_preserve_feedback_and_registered_cuts(
    tmp_path: Path, signal: str, buffered: bool, offered: str, ready: str,
    data: str, expected: int,
) -> None:
    link = "rx -> tx { buffer 1 }" if buffered else "u:Leaf rx -> u.a u.b -> tx"
    valid = "~m.r.read_data[0]" if signal == "valid" else "1"
    source = f"""
module Leaf {{ in a:rv<u8> out b:rv<u8> a -> b }}
module Cell {{ clock clk reset rst in rx:rv<u8> out tx:rv<u8> {link} }}
module Top {{
    clock clk reset rst in wr:bit out q:u8
    memory m:mem<u8,2> {{read_port r write_port w read_latency 0 collision write_first}}
    stage:Cell stage.rx.payload={offered} stage.rx.valid={valid} stage.tx.ready={ready}
    m.r.address=0 m.w.address=0 m.w.enable=wr m.w.data={data}
    q=m.r.read_data
}}
"""
    path = tmp_path / "linked_feedback.zhl"
    path.write_text(source)

    def analyze_source(path: Path) -> None:
        analyze(parse(path.read_text()), source_unit=path.name)

    source_leaf, destination_leaf = (
        ("tx.ready", "rx.ready") if signal == "ready"
        else (f"rx.{signal}", f"tx.{signal}")
    )
    cycle = (
        f"stage.{destination_leaf} -> stage.{source_leaf} -> "
        f"m.r.read_data -> stage.{destination_leaf}"
    )
    for compile_design in (analyze_source, compile_file, sim.compile):
        with pytest.raises(SemanticError, match="combinational storage dependency cycle") as caught:
            compile_design(path)
        assert cycle in str(caught.value)
        origin = caught.value.primary
        assert origin is not None and origin.source_unit == path.name
        assert "m.w.data=" in source.splitlines()[origin.span.start_line - 1]

    path.write_text(source.replace("read_latency 0", "read_latency 1"))
    events = [
        {"reset": {"rst": rst}, "set": {"wr": wr}, "edges": ["clk"]}
        for rst, wr in ((True, 0), (False, 1), (False, 1), (False, 0))
    ]
    compared = compare_program(
        sim.compile(path), events, simulator="iverilog",
        artifact_directory=tmp_path / "comparison", timeout=30, build_timeout=30,
    )
    assert compared.native_trace == compared.rtl_trace == tuple(
        {"q": q} for q in (0, expected, 0, 0)
    )


@pytest.mark.parametrize(
    ("source", "path_leaves"),
    (
        (
            """
protocol Link {role master role slave member data:u8 master -> slave}
module A {clock clk reset rst in value:u8 interface bus:Link.master @clk bus.data=~value}
module B {clock clk reset rst interface bus:Link.slave @clk out observed:u8 observed=bus.data}

module Top {
 clock clk reset rst in wr:bit out q:u8
 memory m:mem<u8,2> {read_port r write_port w read_latency 0 collision write_first}
 src:A {value=m.r.read_data} snk:B src.bus -> snk.bus
 m.r.address=0 m.w.address=0 m.w.enable=wr m.w.data=snk.observed
 q=m.r.read_data
}
""",
            ("snk.observed", "snk.bus.data", "src.bus.data", "src.value"),
        ),
        (
            """
struct Request { data : u8 }
struct Response { data : u8 }

module Requester {
    clock clk
    reset rst
    in data : u8
    in issue : bit
    in consume : bit
    out response : u8
    interface mem : request_response<Request,Response> {
        max_outstanding 2
        ordering in_order
    }
    mem.request.payload = Request { data = data }
    mem.request.valid = issue
    mem.response.ready = consume
    response = mem.response.payload.data
}

module Responder {
    clock clk
    reset rst
    in accept : bit
    in produce : bit
    in response_data : u8
    out accepted:bit out observed:u8
    interface mem : request_response<Request,Response> {
        max_outstanding 2
        ordering in_order
    }
    mem.request.ready = accept
    mem.response.payload = Response { data = response_data }
    mem.response.valid = produce
    accepted = mem.request.transfer observed=mem.request.payload.data
}


module Top {
 clock clk reset rst in wr:bit out q:u8
 memory m:mem<u8,2> {read_port r write_port w read_latency 0 collision write_first}
 src:Requester {data=~m.r.read_data issue=1 consume=1} snk:Responder {accept=1 produce=0 response_data=0} src.mem -> snk.mem
 m.r.address=0 m.w.address=0 m.w.enable=wr m.w.data=snk.observed
 q=m.r.read_data
}
""",
            ("snk.observed", "snk.mem.request.payload", "src.mem.request.payload", "src.data"),
        ),
    ),
    ids=("scalar-member", "request-response-payload"),
)
def test_linked_scalar_and_request_response_feedback(
    tmp_path: Path, source: str, path_leaves: tuple[str, ...],
) -> None:
    path = tmp_path / "linked_feedback.zhl"
    path.write_text(source)

    def analyze_source(path: Path) -> None:
        analyze(parse(path.read_text()), source_unit=path.name)

    for compile_design in (analyze_source, compile_file, sim.compile):
        with pytest.raises(SemanticError, match="combinational storage dependency cycle") as caught:
            compile_design(path)
        expected = " -> ".join((*path_leaves, "m.r.read_data", path_leaves[0]))
        assert expected in str(caught.value)
        origin = caught.value.primary
        assert origin is not None and origin.source_unit == path.name
        assert "m.w.data=" in source.splitlines()[origin.span.start_line - 1]

    path.write_text(source.replace("read_latency 0", "read_latency 1"))
    events = [
        {"reset": {"rst": rst}, "set": {"wr": wr}, "edges": ["clk"]}
        for rst, wr in ((True, 0), (False, 1), (False, 1), (False, 0))
    ]
    compared = compare_program(
        sim.compile(path), events, simulator="iverilog",
        artifact_directory=tmp_path / "comparison", timeout=30, build_timeout=30,
    )
    assert compared.native_trace == compared.rtl_trace == tuple(
        {"q": q} for q in (0, 255, 0, 0)
    )


def _check_parent_protocol_storage_dependencies(tmp_path: Path) -> None:
    def check(name, source, expected, inputs):
        case = tmp_path / name
        case.mkdir()
        path = case / "dut.zhl"
        path.write_text(source)
        if expected is None:
            for entry in (lambda p: analyze(parse(p.read_text()), source_unit=p.name),
                          compile_file, sim.compile):
                with pytest.raises(SemanticError, match=r"combinational (memory|FIFO) dependency cycle") as caught:
                    entry(path)
                origin = caught.value.primary
                assert origin is not None and origin.source_unit == path.name
                assert any(control in source.splitlines()[origin.span.start_line - 1]
                           for control in ("ram.w.data=", "queue.pop="))
            return

        from zlang.backend.systemverilog import emit_artifact

        (case / "dut.sv").write_text(emit_artifact(compile_file(path).ir).text)
        subprocess.run(
            ("yosys", "-Q", "-T", "-p",
             "read_verilog -sv dut.sv; hierarchy -top Top; proc; flatten; opt; scc -expect 0"),
            cwd=case, capture_output=True, text=True, check=True, timeout=30,
        )
        events = [
            {"reset": {"rst": rst}, "set": {"push": push, **inputs}, "edges": ["clk"]}
            for rst, push in ((True, 0), (False, 1), (False, 1), (False, 0))
        ]
        compared = compare_program(
            sim.compile(path), events, simulator="iverilog",
            artifact_directory=case / "comparison", timeout=30, build_timeout=30,
        )
        for trace in (compared.native_trace, compared.rtl_trace):
            assert tuple(row["q"] for row in trace) == expected

    for latency in (0, 1):
        for shape, port, drive, observation, inputs in (
            ("rx-ready", "in rx:rv<u13>", "rx.ready=~ram.r.read_data[0]",
             "mux(rx.ready,1,0)", {"rx": {"valid": 1, "payload": 0}}),
            ("rx-transfer", "in rx:rv<u13>", "rx.ready=~ram.r.read_data[0]",
             "mux(rx.transfer,1,0)", {"rx": {"valid": 1, "payload": 0}}),
            ("tx-valid", "out tx:rv<u13>", "tx.payload=0 tx.valid=~ram.r.read_data[0]",
             "mux(tx.valid,1,0)", {"tx": {"ready": 1}}),
            ("tx-transfer", "out tx:rv<u13>", "tx.payload=0 tx.valid=~ram.r.read_data[0]",
             "mux(tx.transfer,1,0)", {"tx": {"ready": 1}}),
            ("tx-payload", "out tx:rv<u13>", "tx.valid=1 tx.payload=~ram.r.read_data",
             "tx.payload", {"tx": {"ready": 1}}),
        ):
            source = f"""
module Top {{
    clock clk reset rst in push:bit out q:u13
    {port}
    memory ram:mem<u13,4> {{read_port r write_port w read_latency {latency} collision write_first}}
    ram.r.address=2 ram.w.address=2 ram.w.enable=push
    {drive}
    ram.w.data={observation}
    q=ram.r.read_data
}}
"""
            expected = (0, 8191 if shape == "tx-payload" else 1, 0, 0) if latency else None
            check(f"{shape}-{latency}", source, expected, inputs)

    for signal in ("ready", "transfer"):
        for observation in ("ready", "valid"):
            ready = "~queue.ready" if observation == "ready" else "queue.valid"
            check(f"fifo-{signal}-{observation}", f"""
module Top {{
    clock clk reset rst in push:bit in rx:rv<u13> out q:u13
    fifo queue:fifo<u13,4>
    queue.push=push queue.data=211
    queue.pop=rx.{signal}
    rx.ready={ready}
    q=mux(queue.valid,queue.front,0)
}}
""", (0, 211, 211, 0) if observation == "valid" else None,
                {"rx": {"valid": 1, "payload": 0}})
