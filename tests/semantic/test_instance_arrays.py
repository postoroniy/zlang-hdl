from __future__ import annotations

import pytest

from zlang.ir import expressions as expr
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.opt import lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


SOURCE = """
module Lane<W=8> {
    in x:uint<W>
    out y:uint<W + 1>
    y = x + 1
}
module Top<N=2, LAST=1> {
    in values:vec<N,uint<8>>
    out y:u9
    inst lane[N]:Lane<8>
    generate(i in 0..N) {
        lane[i].x = values[i]
    }
    y = lane[N - 1].y
}
"""


SEQUENTIAL_SOURCE = """
module StateLane {
    clock clk
    reset rst
    in enable:bit
    in step:u8
    out value:u8
    reg count:u8=0
    when enable { count <- truncate<8>(count + step) }
    value=count
}
module StateLaneArray {
    clock clk
    reset rst
    in enables:vec<2,bit>
    in steps:vec<2,u8>
    out values:vec<2,u8>
    inst lane[2]:StateLane
    generate(i in 0..2) {
        lane[i].enable=enables[i]
        lane[i].step=steps[i]
    }
    values=generate(i in 0..2) lane[i].value
}
"""


def test_compile_time_array_elaborates_physical_instances_and_bindings() -> None:
    module = analyze(parse(SOURCE))

    assert [item.name for item in module.instances] == ["lane[0]", "lane[1]"]
    assert [(item.instance, item.port) for item in module.instance_bindings] == [
        ("lane[0]", "x"),
        ("lane[1]", "x"),
    ]
    assert len({item.instance_identity for item in module.elaborated_instances}) == 2
    assert len({item.specialization_identity for item in module.elaborated_instances}) == 1
    output = module.assignments[0].expression
    assert isinstance(output, expr.InstanceOutputRef)
    assert (output.instance, output.port) == ("lane[1]", "y")


def test_runtime_array_output_projection_is_a_typed_mux_over_physical_children() -> None:
    source = """
module Lane { in x:u8 out y:u9 y=x+1 }
module Top {
    in values:vec<4,u8> in select:u2 out y:u9
    inst lane[4]:Lane
    generate(i in 0..4) { lane[i].x=values[i] }
    y=lane[select].y
}
"""
    module = analyze(parse(source))
    output = module.assignments[0].expression
    assert isinstance(output, expr.RuntimeIndex)
    assert output.index_range == expr.ValueRange(0, 3, "static_type")
    assert isinstance(output.expression, expr.Generate)
    assert [
        (item.instance, item.port) for item in output.expression.elements
    ] == [(f"lane[{index}]", "y") for index in range(4)]
    assert restore(lower(module)) == module


def test_runtime_array_projection_matches_explicit_generate_and_index() -> None:
    prefix = """
module Lane { in x:u8 out y:u8 y=x }
module Top {
    in values:vec<2,u8> in select:u1 out y:u8
    inst lane[2]:Lane
    generate(i in 0..2) { lane[i].x=values[i] }
"""
    direct = analyze(parse(prefix + "y=lane[select].y }"))
    expanded = analyze(
        parse(prefix + "y=(generate(i in 0..2) lane[i].y)[select] }")
    )
    assert direct.assignments[0].expression == expanded.assignments[0].expression
    assert expression_semantic_identity(
        direct.assignments[0].expression
    ) == expression_semantic_identity(expanded.assignments[0].expression)


def test_runtime_array_projection_rejects_unsafe_range_and_protocol_port() -> None:
    unsafe = """
module Lane { in x:u8 out y:u8 y=x }
module Top { in x:u8 in select:u2 out y:u8 inst lane[2]:Lane
    generate(i in 0..2) { lane[i].x=x } y=lane[select].y }
"""
    with pytest.raises(
        SemanticError, match=r"selector range 0\.\.3.*array length 2"
    ):
        analyze(parse(unsafe))

    protocol = """
module Lane { out y:rv<u8> y.valid=0 y.payload=0 }
module Top { clock clk reset rst in select:u1 out y:u8
    out sink0:rv<u8> out sink1:rv<u8>
    inst lane[2]:Lane
    connect lane[0].y -> sink0
    connect lane[1].y -> sink1
    y=lane[select].y }
"""
    with pytest.raises(SemanticError, match="protocol endpoint"):
        analyze(parse(protocol))


@pytest.mark.parametrize(
    "body",
    (
        "selected:u8=lane[select].y y=selected",
        "sink.x=lane[select].y y=sink.y",
    ),
)
def test_runtime_array_selection_is_not_an_internal_or_selected_input_route(
    body: str,
) -> None:
    source = f"""
module Lane {{ in x:u8 out y:u8 y=x }}
module Sink {{ in x:u8 out y:u8 y=x }}
module Top {{
    in values:vec<2,u8> in select:u1 out y:u8
    inst lane[2]:Lane
    inst sink:Sink
    generate(i in 0..2) {{ lane[i].x=values[i] }}
    {body}
}}
"""
    with pytest.raises(
        SemanticError, match="only while driving a module wire output"
    ):
        analyze(parse(source))


def test_instance_array_canonical_round_trip_preserves_physical_identity() -> None:
    module = analyze(parse(SOURCE))
    restored = restore(lower(module))
    assert restored == module
    assert [item.instance for item in restored.instance_bindings] == [
        "lane[0]",
        "lane[1]",
    ]


@pytest.mark.parametrize(
    ("body", "diagnostic"),
    (
        (
            "inst lane[2]:Lane { x } y=0",
            "cannot use an inline binding block",
        ),
        (
            "inst lane[2]:Lane generate(i in 0..2) { lane[i].x=x } y=lane.y",
            "requires a compile-time index",
        ),
        (
            "inst lane[2]:Lane generate(i in 0..2) { lane[i].x=x } y=lane[2].y",
            "index 2 is out of range",
        ),
        (
            "inst lane[2]:Lane generate(i in 0..1) { lane[i].x=x } y=lane[0].y",
            "lane\\[1\\].*has no compile-time indexed binding",
        ),
        (
            "inst lane[2]:Lane "
            "generate(i in 0..2) { lane[i].x=x } "
            "generate(i in 0..2) { lane[i].x=x } y=lane[0].y",
            "assigned more than once",
        ),
    ),
)
def test_invalid_indexed_instance_bindings_are_precise(
    body: str, diagnostic: str
) -> None:
    source = (
        "module Lane { in x:u8 out y:u8 y=x } "
        f"module Top {{ in x:u8 out y:u8 {body} }}"
    )
    with pytest.raises(SemanticError, match=diagnostic):
        analyze(parse(source))


def test_non_ready_valid_protocol_instance_arrays_remain_explicitly_bounded() -> None:
    protocol = (
        "module Child { clock clk reset rst out tx:credit<u8,2> "
        "tx.payload=0 tx.send=0 } "
        "module Top { out y:u8 inst child[2]:Child y=0 }"
    )
    with pytest.raises(SemanticError, match="primitive ready-valid"):
        analyze(parse(protocol))


@pytest.mark.parametrize(
    "child_ports,parent_ports,binding,result",
    (
        (
            "in x:vec<2,u8> out y:u8 y=x[0]",
            "in x:vec<2,vec<2,u8>> out y:vec<2,u8>",
            "child[i].x=x[i]",
            "generate(i in 0..2) child[i].y",
        ),
        (
            "in x:u8 out y:vec<2,u8> y=generate(i in 0..2) x",
            "in x:vec<2,u8> out y:vec<2,vec<2,u8>>",
            "child[i].x=x[i]",
            "generate(i in 0..2) child[i].y",
        ),
        (
            "in x:u8 out y:Pair y=Pair { left=x right=x }",
            "in x:vec<2,u8> out y:vec<2,Pair>",
            "child[i].x=x[i]",
            "generate(i in 0..2) child[i].y",
        ),
    ),
)
def test_aggregate_wire_instance_arrays_use_the_typed_wire_abi(
    child_ports: str,
    parent_ports: str,
    binding: str,
    result: str,
) -> None:
    source = (
        "struct Pair { left:u8 right:u8 } "
        f"module Child {{ {child_ports} }} "
        f"module Top {{ {parent_ports} inst child[2]:Child "
        f"generate(i in 0..2) {{ {binding} }} y={result} }}"
    )
    module = analyze(parse(source))
    assert len(module.elaborated_instances) == 2
    assert restore(lower(module)) == module


@pytest.mark.parametrize(
    "child",
    (
        (
            "module Child { clock clk reset rst in data:u8 in push:bit in pop:bit "
            "out y:u8 reg seen:u8=0 when push { seen <- data } "
            "fifo q:fifo<u8,2> q.data=data q.push=push q.pop=pop y=q.front }"
        ),
        (
            "module Child { clock clk reset rst out y:u8 reg seen:u8=0 "
            "when 0 { seen <- 1 } memory m:mem<u8,2>{ read_latency 1 "
            "collision read_first } m.read_address=0 m.write_enable=0 "
            "m.write_address=0 m.write_data=0 y=m.read_data }"
        ),
        (
            "module Child { clock clk reset rst in rx:rv<u8> out tx:rv<u8> "
            "reg seen:u8=0 when rx.transfer { seen <- rx.payload } "
            "fifo q:fifo<u8,2> q.data=rx.payload q.push=rx.transfer "
            "q.pop=tx.transfer rx.ready=q.ready tx.payload=q.front tx.valid=q.valid }"
        ),
    ),
)
def test_storage_instance_arrays_reject_combined_user_state(child: str) -> None:
    source = child + " module Top { clock clk reset rst inst lane[2]:Child }"
    with pytest.raises(
        SemanticError,
        match=(
            "storage-owning child combined with user registers|"
            "memory resources cannot yet be mixed with user registers"
        ),
    ):
        analyze(parse(source))



def test_sequential_scalar_array_preserves_specialization_and_instance_identity() -> None:
    module = analyze(parse(SEQUENTIAL_SOURCE))

    assert [item.name for item in module.instances] == ["lane[0]", "lane[1]"]
    assert len({item.instance_identity for item in module.elaborated_instances}) == 2
    assert len({item.specialization_identity for item in module.elaborated_instances}) == 1
    assert all(item.is_sequential for item in module.children)
    assert [(item.instance, item.port) for item in module.instance_bindings] == [
        ("lane[0]", "enable"),
        ("lane[0]", "step"),
        ("lane[1]", "enable"),
        ("lane[1]", "step"),
    ]


def test_sequential_scalar_array_canonical_round_trip_is_lossless() -> None:
    module = analyze(parse(SEQUENTIAL_SOURCE))
    restored = restore(lower(module))

    assert restored == module
    assert [item.instance_identity for item in restored.elaborated_instances] == [
        item.instance_identity for item in module.elaborated_instances
    ]
    generated = restored.assignments[0].expression
    assert isinstance(generated, expr.Generate)
    references = [
        item for item in generated.elements
        if isinstance(item, expr.InstanceOutputRef)
    ]
    assert [(item.instance, item.port) for item in references] == [
        ("lane[0]", "value"),
        ("lane[1]", "value"),
    ]


def test_instance_array_accepts_mixed_scalar_and_ready_valid_child_ports() -> None:
    source = """
module MixedLane {
    clock clk reset rst
    in bias:u8 in input:rv<u8> out output:rv<u8>
    input.ready=output.ready
    output.valid=input.valid
    output.payload=truncate<8>(input.payload + bias)
}
module MixedLaneArray {
    clock clk reset rst
    in input0:rv<u8> in input1:rv<u8>
    out output0:rv<u8> out output1:rv<u8>
    inst lane[2]:MixedLane
    generate(i in 0..2) { lane[i].bias=1 }
    connect input0 -> lane[0].input
    connect lane[0].output -> output0
    connect input1 -> lane[1].input
    connect lane[1].output -> output1
}
"""
    module = analyze(parse(source))
    assert [(item.instance, item.port) for item in module.instance_bindings] == [
        ("lane[0]", "bias"),
        ("lane[1]", "bias"),
    ]
    assert restore(lower(module)) == module


def test_instance_array_accepts_scheduled_storage_with_user_state() -> None:
    source = """
module ScheduledLane {
    clock clk reset rst
    in data:u8 in push:bit in pop:bit
    out front:u8 out seen:u8
    fifo q:fifo<u8,2>
    reg last:u8=0
    rule enqueue when push { q.push(data) last <- data }
    rule dequeue when pop { q.pop() }
    front=q.front seen=last
}
module ScheduledLaneArray {
    clock clk reset rst
    in data:vec<2,u8> in push:vec<2,bit> in pop:vec<2,bit>
    out front:vec<2,u8> out seen:vec<2,u8>
    inst lane[2]:ScheduledLane
    generate(i in 0..2) {
        lane[i].data=data[i]
        lane[i].push=push[i]
        lane[i].pop=pop[i]
    }
    front=generate(i in 0..2) lane[i].front
    seen=generate(i in 0..2) lane[i].seen
}
"""
    module = analyze(parse(source))
    assert all(child.resolved_transition is not None for child in module.children)
    assert all(child.fifos[0].scheduled for child in module.children)
    assert restore(lower(module)) == module


@pytest.mark.parametrize(
    ("child", "diagnostic"),
    (
        (
            "clock clk reset rst in d:u8 in push:bit in pop:bit out y:u8 "
            "fifo a:fifo<u8,2> fifo b:fifo<u8,2> "
            "a.data=d a.push=push a.pop=pop "
            "b.data=d b.push=push b.pop=pop y=a.front",
            "supports exactly one FIFO, synchronous memory, or initialized ROM",
        ),
        (
            "clock a reset ar @a clock b reset br @b "
            "out y:u8 @a reg q:u8=0 @a y=q",
            "must share exactly one synchronous clock/reset domain",
        ),
    ),
)
def test_sequential_array_rejects_multiple_storage_and_cdc_children(
    child: str, diagnostic: str
) -> None:
    source = (
        f"module Child {{ {child} }} "
        "module Top { clock clk reset rst out y:u8 inst child[2]:Child y=0 }"
    )
    with pytest.raises(SemanticError, match=diagnostic):
        analyze(parse(source))


def test_sequential_array_accepts_one_storage_resource_and_round_trips() -> None:
    source = """
module MemoryLane {
    clock clk reset rst
    in address:u1 in write_enable:bit in data:u8
    out value:u8
    memory table:mem<u8,2> { read_latency 1 collision read_first }
    table.read_address=address
    table.write_enable=write_enable
    table.write_address=address
    table.write_data=data
    value=table.read_data
}
module MemoryLaneArray {
    clock clk reset rst
    in address:vec<2,u1> in write_enable:vec<2,bit> in data:vec<2,u8>
    out value:vec<2,u8>
    inst lane[2]:MemoryLane
    generate(i in 0..2) {
        lane[i].address=address[i]
        lane[i].write_enable=write_enable[i]
        lane[i].data=data[i]
    }
    value=generate(i in 0..2) lane[i].value
}
"""
    module = analyze(parse(source))
    assert len(module.children) == 2
    assert all(len(child.memories) == 1 for child in module.children)
    assert len({item.instance_identity for item in module.elaborated_instances}) == 2
    assert len({item.specialization_identity for item in module.elaborated_instances}) == 1
    assert restore(lower(module)) == module


def test_sequential_array_still_rejects_csr_children_explicitly() -> None:
    source = """
module CsrLane {
    clock clk reset rst
    out value:u8
    value=0
    csr bank @0 { CONTROL @0 { enable bit rw = 0 } }
}
module CsrLaneArray {
    clock clk reset rst
    out value:u8
    inst lane[2]:CsrLane
    value=0
}
"""
    with pytest.raises(SemanticError, match="does not support CSR children"):
        analyze(parse(source))
