from zlang.formal import build_formal_design
from zlang.opt import lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


FIFO_BODY = (
    "clock clk reset rst in data:u8 in push:bit in pop:bit "
    "fifo q:fifo<u8,DEPTH> q.data=data q.push=push q.pop=pop"
)


def test_fifo_and_memory_depth_expressions_resolve_to_concrete_integers() -> None:
    fifo = analyze(parse(
        "module Delay<N=8>{clock c reset r in x:u8 "
        "fifo q:fifo<u8,N/2> q.data=x q.push=0 q.pop=0}"
    ))
    memory = analyze(parse(
        "module Store<LOG2N=9,STAGE=6>{clock c reset r in a:u2 in x:u8 "
        "memory m:mem<u8,1 << (LOG2N-STAGE-1)>{read_latency 1 collision read_first} "
        "m.read_address=a m.write_enable=0 m.write_address=a m.write_data=x}"
    ))
    assert fifo.fifos[0].depth == 4
    assert memory.memories[0].depth == 4


def test_multiple_and_nested_specializations_keep_distinct_identities() -> None:
    source = (
        f"module Delay<DEPTH=4>{{{FIFO_BODY}}} "
        "module Wrapper<DEPTH=8>{clock clk reset rst in data:u8 in push:bit in pop:bit "
        "inst nested:Delay<DEPTH> {data push pop}} "
        "module Top{clock clk reset rst in data:u8 in push:bit in pop:bit "
        "inst d4:Delay<4> {data push pop} inst d8:Delay<8> {data push pop} "
        "inst nested:Wrapper<16> {data push pop}}"
    )
    module = analyze(parse(source))
    direct = {item.instance.name: item for item in module.elaborated_instances}
    assert module.children[0].fifos[0].depth == 4
    assert module.children[1].fifos[0].depth == 8
    assert direct["d4"].specialization_identity != direct["d8"].specialization_identity
    wrapper = module.children[2]
    assert wrapper.children[0].fifos[0].depth == 16
    assert wrapper.elaborated_instances[0].specialization_identity != direct["d8"].specialization_identity


def test_fft512_delay_specializations_cover_all_nine_stage_depths() -> None:
    depths = (256, 128, 64, 32, 16, 8, 4, 2, 1)
    instances = " ".join(
        f"inst stage{index}:Delay<{depth}> {{data push pop}}"
        for index, depth in enumerate(depths)
    )
    source = (
        f"module Delay<DEPTH=4>{{{FIFO_BODY}}} "
        "module Top{clock clk reset rst in data:u8 in push:bit in pop:bit "
        f"{instances}}}"
    )
    module = analyze(parse(source))
    assert tuple(child.fifos[0].depth for child in module.children) == depths
    assert len({item.specialization_identity for item in module.elaborated_instances}) == 9


def test_parameterized_storage_canonical_round_trip_is_concrete() -> None:
    semantic = analyze(parse(
        f"module Delay<DEPTH=8>{{{FIFO_BODY}}}"
    ))
    canonical = lower(semantic)
    assert canonical.fifos[0].depth == 8
    assert restore(canonical) == semantic
    assert restore(canonical).fifos[0].depth == 8


def test_parameterized_fifo_m35_bounds_use_resolved_depth() -> None:
    module = analyze(parse(f"module Delay<DEPTH=8>{{{FIFO_BODY}}}"))
    design = build_formal_design(module)
    fifo_properties = [
        item for item in design.properties if item.generated_from == "fifo:q"
    ]
    assert fifo_properties
    assert any("<= 8" in item.expression for item in fifo_properties)
    assert all("DEPTH" not in item.expression for item in fifo_properties)


def test_illegal_depth_expressions_have_precise_diagnostics() -> None:
    cases = (
        ("module X<D>{clock c reset r in x:u8 fifo q:fifo<u8,D> q.data=x q.push=0 q.pop=0}",
         "unresolved compile-time parameter 'D'"),
        ("module X{clock c reset r in x:u8 in runtime:u8 fifo q:fifo<u8,runtime> q.data=x q.push=0 q.pop=0}",
         "unresolved compile-time parameter 'runtime'"),
        ("module X<D=4>{clock c reset r in x:u8 fifo q:fifo<u8,D-D> q.data=x q.push=0 q.pop=0}",
         "FIFO 'q' depth must be positive"),
        ("module X<D=4>{clock c reset r in x:u8 fifo q:fifo<u8,D-D-1> q.data=x q.push=0 q.pop=0}",
         "FIFO 'q' depth must be positive"),
        ("module X<N=8>{clock c reset r in x:u8 fifo q:fifo<u8,N/3> q.data=x q.push=0 q.pop=0}",
         "FIFO 'q' depth division must be exact"),
    )
    for source, message in cases:
        try:
            analyze(parse(source))
        except SemanticError as error:
            assert message in str(error)
        else:
            raise AssertionError(f"expected storage-depth diagnostic: {message}")


def test_specialization_producing_zero_depth_is_rejected() -> None:
    source = (
        f"module Delay<DEPTH=4>{{{FIFO_BODY}}} "
        "module Top{clock clk reset rst in data:u8 in push:bit in pop:bit "
        "inst bad:Delay<0> {data push pop}}"
    )
    try:
        analyze(parse(source))
    except SemanticError as error:
        assert "FIFO 'q' depth must be positive" in str(error)
    else:
        raise AssertionError("zero-depth specialization was accepted")
