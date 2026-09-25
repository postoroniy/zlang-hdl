from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

import zlang
import zlang.simulate as reference_simulator
import zlang.simulation_plan as simulation_plan_module
from zlang.compiler import compile_file, create_file_compilation_session
from zlang.simulate import simulate, simulate_cycles
from zlang.simulation_plan import (
    JitUnsupportedFeatureError,
    MAX_PLAN_BYTES,
    MAX_PLAN_NODES,
    SimulationPlan,
    SimulationPlanError,
)
from tools.benchmark_frontend_scalability import mixer_source


ROOT = Path(__file__).resolve().parents[2]


def _source(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / f"{name}.zhl"
    path.write_text(text, encoding="utf-8")
    return path


def _resign(payload: dict[str, object]) -> bytes:
    payload["identity"] = ""
    unsigned = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    payload["identity"] = hashlib.sha256(unsigned).hexdigest()
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def test_simulation_plan_is_canonical_and_round_trips(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "comb",
        "module Comb { in a,b:u8 in select:bit "
        "out y:u9=select ? (a+b) : extend<9>(a) }",
    )
    first = zlang.sim.compile(source, top="Comb", engine="jit").plan
    second = zlang.sim.compile(source, top="Comb", engine="jit").plan

    assert first.to_bytes() == second.to_bytes()
    assert first.identity == second.identity
    assert SimulationPlan.from_bytes(first.to_bytes()) == first
    assert b"/home/" not in first.to_bytes()
    assert b"module Comb" not in first.to_bytes()


def test_native_plan_and_rust_surface_are_language_neutral(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "primitive_boundary",
        "module PrimitiveBoundary { in a:u8 out y:u8=truncate<8>(a + 1) }",
    )
    payload = zlang.sim.compile(
        source, top="PrimitiveBoundary", engine="jit"
    ).plan.payload
    assert "transitions" not in payload
    assert "direct_next" not in payload
    assert "edge_programs" in payload
    primitive_ops = {
        "constant",
        "load_input",
        "load_state",
        "load_event",
        "load_memory",
        "add",
        "sub",
        "mul",
        "and",
        "or",
        "xor",
        "not",
        "shl",
        "lshr",
        "ashr",
        "eq",
        "ult",
        "ule",
        "slt",
        "sle",
        "select",
        "extract_bits",
        "insert_bits",
        "concat_bits",
    }
    assert {node["op"] for node in payload["nodes"]} <= primitive_ops
    for forbidden in (
        "fixed_convert",
        "enum_decode",
        "enum_valid",
        "union_construct",
        "struct_construct",
        "tuple_construct",
        "vector_index",
        "reduce",
        "transition",
        "register_write",
    ):
        assert forbidden not in json.dumps(payload, sort_keys=True)


def test_simulation_plan_is_hash_seed_independent(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "seeded",
        "module Seeded { in a,b:u8 out y:u9=a+b }",
    )
    script = (
        "from zlang.compiler import create_file_compilation_session as make;"
        f"p=make({str(source)!r},top='Seeded').simulation_plan;"
        "print(p.identity);print(p.to_json())"
    )
    outputs = []
    for seed in ("0", "15", "63"):
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1] == outputs[2]


def test_exact_plan_reuses_process_owned_native_program(tmp_path: Path) -> None:
    source = _source(tmp_path, "cached", "module Cached { in a:u8 out y:u8=a }")
    first = zlang.sim.compile(source, top="Cached", engine="jit")
    second = zlang.sim.compile(source, top="Cached", engine="jit")
    assert first.identity == second.identity
    assert first._native is second._native


def test_distinct_instances_execute_safely_from_parallel_threads(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "parallel_counter",
        """
        module ParallelCounter {
            clock clk reset rst
            in step:u16
            out y:u32
            reg value:u32=0
            value <- truncate<32>(value + extend<32>(step))
            y=value
        }
        """,
    )
    program = zlang.sim.compile(source, top="ParallelCounter", engine="jit")

    def run(step: int, cycles: int) -> int:
        instance = program.create()
        with instance:
            instance.set("step", step)
            return int(instance.run_cycles("clk", cycles)["y"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, 3, 20_000)
        second = pool.submit(run, 7, 12_000)
        assert first.result() == 60_000
        assert second.result() == 84_000


def test_persistent_api_defaults_to_native_without_fallback(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path, "explicit", "module Explicit { out y:bit y=0 }")
    default = zlang.sim.compile(source, top="Explicit")
    assert type(default._native).__module__ == "_zlang_native_sim"  # noqa: SLF001
    with default.create() as instance:
        assert instance.get("y") == 0
    assert zlang.sim.load(source, top="Explicit", engine="reference").get("y") == 0
    assert zlang.sim.load(source, top="Explicit", engine="python").get("y") == 0


def test_reference_executor_consumes_the_same_primitive_plan(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "reference_counter",
        """
        module ReferenceCounter {
            clock clk reset rst
            in step:u9
            out y:u17
            reg value:u17=3
            value <- truncate<17>(value + extend<17>(step))
            y=value
        }
        """,
    )
    reference = zlang.sim.load(source, top="ReferenceCounter", engine="reference")
    native = zlang.sim.load(source, top="ReferenceCounter", engine="native")
    for instance in (reference, native):
        instance.set("step", 257)
    for _ in range(25):
        assert reference.edge("clk") == native.edge("clk")
    assert reference.get_packed("value") == native.get_packed("value")


def test_simulation_plan_rejects_noncanonical_and_corrupt_payloads(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path, "comb", "module Comb { in a:u8 out y:u8=a }")
    plan = zlang.sim.compile(source, top="Comb", engine="jit").plan
    decoded = json.loads(plan.to_bytes())
    decoded["schema"] = "unknown"
    with pytest.raises(SimulationPlanError, match="canonical|schema"):
        SimulationPlan.from_bytes(json.dumps(decoded).encode())
    with pytest.raises(SimulationPlanError, match="canonical UTF-8 JSON"):
        SimulationPlan.from_bytes(plan.to_bytes()[:-1])


def test_python_and_native_decoders_reject_resource_exhaustion_before_parsing(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path, "bounded", "module Bounded { out y:bit=0 }")
    plan = zlang.sim.compile(source, top="Bounded", engine="jit").plan
    oversized = b" " * (MAX_PLAN_BYTES + 1)
    with pytest.raises(SimulationPlanError, match="encoded bytes"):
        SimulationPlan.from_bytes(oversized)

    payload = json.loads(plan.to_bytes())
    seed = payload["nodes"][0]
    payload["nodes"] = [
        {**seed, "id": identifier}
        for identifier in range(MAX_PLAN_NODES + 1)
    ]
    bounded_nodes = _resign(payload)
    with pytest.raises(SimulationPlanError, match="node table"):
        SimulationPlan.from_bytes(bounded_nodes)

    import _zlang_native_sim

    with pytest.raises(ValueError, match="encoded bytes"):
        _zlang_native_sim.compile_plan_bytes(oversized)
    with pytest.raises(ValueError, match=f"exceeds {MAX_PLAN_NODES} nodes"):
        _zlang_native_sim.compile_plan_bytes(bounded_nodes)


def test_python_and_native_reject_excessive_dynamic_region_work(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path, "bounded_region", "module BoundedRegion { out y:bit=0 }")
    payload = json.loads(
        zlang.sim.compile(source, top="BoundedRegion", engine="reference").plan.to_bytes()
    )
    body = [
        {"id": 0, "op": "load_index", "width": 64, "operands": [],
         "attributes": {}, "origins": []},
        {"id": 1, "op": "constant", "width": 64, "operands": [],
         "attributes": {"limbs": [1]}, "origins": []},
    ]
    for identifier in range(2, 1026):
        body.append({
            "id": identifier, "op": "add", "width": 64,
            "operands": [identifier - 1 if identifier > 2 else 0, 1],
            "attributes": {}, "origins": [],
        })
    body.append({
        "id": len(body), "op": "eq", "width": 1,
        "operands": [len(body) - 1, 1], "attributes": {}, "origins": [],
    })
    payload["regions"] = [{
        "start": 0, "stop": 8192, "element_width": 1, "width": 8192,
        "capture_widths": [], "nodes": body, "root": len(body) - 1,
    }]
    payload["nodes"].append({
        "id": len(payload["nodes"]), "op": "loop_region", "width": 8192,
        "operands": [], "attributes": {"region": 0}, "origins": [],
    })
    encoded = _resign(payload)
    with pytest.raises(SimulationPlanError, match="dynamic node work"):
        SimulationPlan.from_bytes(encoded)
    import _zlang_native_sim

    with pytest.raises(ValueError, match="dynamic node work"):
        _zlang_native_sim.compile_plan_bytes(encoded)


def test_primitive_lowering_stops_at_node_bound_before_plan_serialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(
        tmp_path,
        "primitive_bound",
        "module PrimitiveBound { in a,b:u8 out y:u9=a+b }",
    )
    session = create_file_compilation_session(source, top="PrimitiveBound")
    # The semantic DAG has three nodes.  Exact-width primitive lowering needs
    # additional resize nodes, so this specifically exercises the expansion
    # guard rather than the earlier semantic-DAG guard.
    monkeypatch.setattr(simulation_plan_module, "MAX_PLAN_NODES", 3)
    with pytest.raises(SimulationPlanError, match="primitive simulation plan"):
        simulation_plan_module.build_simulation_plan(session.planning.module)


def test_64_step_shared_dag_remains_compact_and_matches_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = ROOT / "benchmarks/native_jit/large_combinational.zhl"
    top = "NativeJitLargeCombinationalBenchmark"
    program = zlang.sim.compile(source, top=top, engine="jit")

    assert len(program.plan.payload["nodes"]) < 64 * 12
    assert len(program.plan.to_bytes()) < 128 * 1024
    constants = (
        0x9E3779B9,
        0x7F4A7C15,
        0x94D049BB,
        0xED5AD4BB,
        0xAC4C1B51,
        0x31848BAB,
        0x4CF5AD43,
        0x1B873593,
        0x85EBCA6B,
        0xC2B2AE35,
        0x27D4EB2F,
        0x165667B1,
        0xD3A2646C,
        0xFD7046C5,
        0xB55A4F09,
        0x6C8E9CF5,
    )
    shifts = ((False, 7), (True, 9), (False, 11), (True, 5))

    def reference(seed: int) -> int:
        value = seed
        for index in range(64):
            left, amount = shifts[index % len(shifts)]
            shifted = (value << amount) if left else (value >> amount)
            value = ((value ^ (shifted & 0xFFFFFFFF)) + constants[index % 16])
            value &= 0xFFFFFFFF
        return value

    reference_visits: dict[int, int] = {}
    evaluate_uncached = reference_simulator._evaluate_uncached

    def track_reference_visit(*args: object, **kwargs: object) -> object:
        expression = args[0]
        identity = id(expression)
        reference_visits[identity] = reference_visits.get(identity, 0) + 1
        return evaluate_uncached(*args, **kwargs)

    monkeypatch.setattr(
        reference_simulator,
        "_evaluate_uncached",
        track_reference_visit,
    )

    with program.create() as instance:
        for seed in (0, 1, 0x12345678, 0xFFFFFFFF):
            reference_visits.clear()
            instance.set("seed", seed)
            expected = {"result": reference(seed)}
            assert instance.eval() == expected
            assert simulate(program.module, seed=seed) == expected
            assert reference_visits
            assert max(reference_visits.values()) == 1


@pytest.mark.parametrize("steps", [12, 16, 24, 32, 48, 64])
def test_shared_dag_scalability_is_linear_in_unique_nodes(
    tmp_path: Path, steps: int
) -> None:
    source = _source(tmp_path, f"shared_dag_{steps}", mixer_source(steps))
    session = create_file_compilation_session(
        source, top="FrontendSharedDagScalability"
    )
    plan = session.simulation_plan

    assert len(session.high_level_ir.expressions) <= 5 * steps + 5
    assert len(session.optimization_ir.expressions) <= 5 * steps + 5
    assert len(plan.payload["nodes"]) <= 10 * steps + 5
    assert len(plan.to_bytes()) <= 2048 * steps

@pytest.mark.parametrize(
    "mutation", ["cycle", "wide", "unknown", "bad_slot", "bad_width"]
)
def test_native_decoder_rejects_mutated_plan_before_execution(
    tmp_path: Path,
    mutation: str,
) -> None:
    source = _source(tmp_path, "decode", "module Decode { in a:u8 out y:u9=a + 1 }")
    plan = zlang.sim.compile(source, top="Decode", engine="jit").plan
    payload = json.loads(plan.to_bytes())
    if mutation == "cycle":
        payload["nodes"][0]["operands"] = [0]
    elif mutation == "wide":
        payload["nodes"][0]["width"] = 513
    elif mutation == "bad_slot":
        payload["nodes"][0]["attributes"]["name"] = "missing"
    elif mutation == "bad_width":
        add = next(node for node in payload["nodes"] if node["op"] == "add")
        payload["nodes"][add["operands"][0]]["width"] -= 1
    else:
        payload["unexpected"] = True
    import _zlang_native_sim

    with pytest.raises(
        ValueError,
        match=(
            "topological|through 512|unknown field|packed value requires|"
            "matching slot|incompatible widths|invalid widths"
        ),
    ):
        _zlang_native_sim.compile_plan_bytes(_resign(payload))


@pytest.mark.parametrize(
    "inputs",
    [
        {"a": 0, "b": 0, "shift": 0},
        {"a": 255, "b": 3, "shift": 2},
        {"a": 0x81, "b": 0x7F, "shift": 7},
    ],
)
def test_native_combinational_matches_python_oracle(
    tmp_path: Path,
    inputs: dict[str, int],
) -> None:
    source = _source(
        tmp_path,
        "ops",
        """
        module Ops {
            in a,b:u8
            in shift:u3
            out add:u9=a+b
            out sub:u8=a-b
            out mul:u16=a*b
            out band:u8=a & b
            out bor:u8=a | b
            out bxor:u8=a ^ b
            out left:u8=a << shift
            out right:u8=a >> shift
            out equal:bit=a == b
            out unequal:bit=a != b
            out less:bit=a < b
            out less_equal:bit=a <= b
            out greater:bit=a > b
            out greater_equal:bit=a >= b
        }
        """,
    )
    module = compile_file(source, top="Ops").ir
    oracle = simulate(module, **inputs)
    instance = zlang.sim.load(source, top="Ops", engine="jit")
    with instance:
        for name, value in inputs.items():
            instance.set(name, value)
        assert instance.eval() == oracle


def test_native_aggregate_packing_and_runtime_index(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "aggregate",
        """
        struct Pair { high:u8 low:u8 }
        module Aggregate {
            in pair:Pair
            in values:vec<4,u8>
            in index:u2
            out high:u8=pair.high
            out low:u8=pair.low
            out selected:u8=values[index]
        }
        """,
    )
    instance = zlang.sim.load(source, top="Aggregate", engine="jit")
    with instance:
        instance.set("pair", {"high": 0xA5, "low": 0x3C})
        instance.set("values", [11, 22, 33, 44])
        instance.set("index", 2)
        assert instance.eval() == {"high": 0xA5, "low": 0x3C, "selected": 33}

    corrupted = json.loads(
        zlang.sim.compile(source, top="Aggregate", engine="jit").plan.to_bytes()
    )
    vector_type = next(
        port["api_type"]
        for port in corrupted["ports"]
        if port["api_type"]["kind"] == "vec"
    )
    vector_type["length"] += 1
    with pytest.raises(SimulationPlanError, match="vector packed width"):
        SimulationPlan.from_bytes(_resign(corrupted))
    import _zlang_native_sim

    # Public aggregate packing metadata is intentionally opaque to Rust.  The
    # compiler/Python boundary rejects it; the primitive executor only checks
    # the packed port width needed for safe execution.
    _zlang_native_sim.compile_plan_bytes(_resign(corrupted))


def test_native_switch_enum_decode_and_tagged_union_match_python_oracle(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "native_union",
        """
        enum Mode : bits<2> { Idle=0 Work=1 }
        union Message { Empty Data { value:s8 } }
        module NativeUnion {
            in raw_mode:bits<2>
            in kind:bits<1>
            in data:s8
            out selected:bits<8>
            out valid:bit=enum_valid<Mode>(raw_mode)
            decoded:Mode=enum_decode<Mode>(raw_mode,Mode.Idle)
            message:Message=switch kind {
                0=>Message.Empty
                else=>Message.Data { value=data }
            }
            value:s8=match message {
                Message.Empty=>0
                Message.Data { value }=>value
            }
            selected=bitcast<bits<8>>(switch decoded {
                Mode.Idle=>value
                Mode.Work=>-1
            })
        }
        """,
    )
    module = compile_file(source, top="NativeUnion").ir
    instance = zlang.sim.load(source, top="NativeUnion", engine="jit")
    vectors = (
        {"raw_mode": 0, "kind": 0, "data": -7},
        {"raw_mode": 0, "kind": 1, "data": -7},
        {"raw_mode": 1, "kind": 1, "data": -7},
        {"raw_mode": 3, "kind": 1, "data": 12},
    )
    with instance:
        for inputs in vectors:
            for name, value in inputs.items():
                instance.set(name, value)
            assert instance.eval() == simulate(module, **inputs)


def test_native_generated_collections_concat_reshape_dot_and_reduce(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "native_collections",
        """
        module NativeCollections {
            in a,b:vec<4,u8>
            out generated:vec<4,u8>
            out mapped:vec<4,u9>
            out joined:vec<8,u8>
            out shaped:vec<2,vec<2,u8>>
            out total:u10
            out all_bits:u8
            out any_bits:u8
            out parity_bits:u8
            out product_total:u18
            generated=generate(i in 0..4) i
            mapped=map(i in 0..4) { a[i] + a[i] }
            joined=concat(a,b)
            shaped=reshape(a)
            total=reduce(+,a)
            all_bits=reduce(&,a)
            any_bits=reduce(|,a)
            parity_bits=reduce(^,a)
            product_total=dot(a,b)
        }
        """,
    )
    module = compile_file(source, top="NativeCollections").ir
    instance = zlang.sim.load(source, top="NativeCollections", engine="jit")
    vectors = (
        {"a": [1, 2, 3, 4], "b": [5, 6, 7, 8]},
        {"a": [255, 0, 17, 31], "b": [2, 9, 4, 3]},
    )
    with instance:
        for inputs in vectors:
            for name, value in inputs.items():
                instance.set(name, value)
            assert instance.eval() == simulate(module, **inputs)


def test_nested_functional_regions_match_typed_reference_in_both_engines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(
        tmp_path,
        "nested_regions",
        """
        module NestedRegions {
            in enables:vec<4,vec<2,bit>>
            in addresses:vec<4,vec<2,u6>>
            in values:vec<4,vec<2,u8>>
            out result:vec<32,u8>
            result=generate(dst in 0..32) {
                reduce(|,generate(lane in 0..4) {
                    reduce(|,generate(byte in 0..2) {
                        (enables[lane][byte] & (addresses[lane][byte] == dst)) ?
                            values[lane][byte] : 0
                    })
                })
            }
        }
        """,
    )
    module = compile_file(source, top="NestedRegions").ir
    native = zlang.sim.load(source, top="NestedRegions", engine="native")
    reference = zlang.sim.load(source, top="NestedRegions", engine="reference")
    assert native.program.plan.payload["regions"]
    assert SimulationPlan.from_bytes(native.program.plan.to_bytes()) == native.program.plan
    assert any(
        node["op"] == "loop_region"
        for region in native.program.plan.payload["regions"]
        for node in region["nodes"]
    )
    assert max(
        len(region["nodes"]) for region in native.program.plan.payload["regions"]
    ) < 128
    vectors = (
        {
            "enables": [[1, 1], [1, 0], [1, 1], [0, 1]],
            "addresses": [[0, 3], [3, 5], [5, 7], [7, 31]],
            "values": [[1, 2], [4, 8], [16, 32], [64, 128]],
        },
        {
            "enables": [[0, 0], [1, 1], [0, 1], [1, 0]],
            "addresses": [[7, 5], [10, 10], [20, 12], [12, 0]],
            "values": [[255, 254], [3, 4], [5, 6], [7, 8]],
        },
    )
    with native, reference:
        for inputs in vectors:
            for name, value in inputs.items():
                native.set(name, value)
                reference.set(name, value)
            expected = simulate(module, **inputs)
            assert reference.eval() == expected
            assert native.eval() == expected

    damaged = json.loads(native.program.plan.to_bytes())
    damaged["regions"][0]["root"] = len(damaged["regions"][0]["nodes"])
    with pytest.raises(SimulationPlanError, match="simulation region shape"):
        SimulationPlan.from_bytes(_resign(damaged))
    import _zlang_native_sim

    with pytest.raises(ValueError, match="functional region"):
        _zlang_native_sim.compile_plan_bytes(_resign(damaged))
    monkeypatch.setattr(simulation_plan_module, "MAX_PLAN_DYNAMIC_NODE_WORK", 1)
    with pytest.raises(SimulationPlanError, match="dynamic node work"):
        SimulationPlan.from_bytes(native.program.plan.to_bytes())


def test_functional_region_in_edge_program_matches_typed_cycles(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "region_state",
        """
        module RegionState {
            clock clk reset rst
            in data:vec<64,u8>
            in bias:u8
            out result:vec<64,u8>
            reg state:vec<64,u8>=repeat(0)
            state <- generate(i in 0..64) { data[i] ^ bias }
            result=state
        }
        """,
    )
    module = compile_file(source, top="RegionState").ir
    vectors = (
        {"data": [index for index in range(64)], "bias": 17},
        {"data": [(index * 3) & 255 for index in range(64)], "bias": 83},
        {"data": [(255 - index) for index in range(64)], "bias": 7},
    )
    expected = simulate_cycles(module, vectors, [True, False, False])
    for engine in ("reference", "native"):
        instance = zlang.sim.load(source, top="RegionState", engine=engine)
        assert instance.program.plan.payload["regions"]
        with instance:
            for cycle, inputs in enumerate(vectors):
                instance.reset("rst", asserted=cycle == 0)
                for name, value in inputs.items():
                    instance.set(name, value)
                assert instance.eval() == expected[cycle]
                instance.edge("clk")


def test_functional_region_bit_packing_crosses_limb_boundaries(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "bit_region",
        "module BitRegion { in mask:vec<192,bit> out result:vec<192,bit> "
        "result=generate(i in 0..192) mask[i] }",
    )
    module = compile_file(source, top="BitRegion").ir
    mask = [int(index in {0, 63, 64, 127, 128, 191}) for index in range(192)]
    expected = simulate(module, mask=mask)
    for engine in ("reference", "native"):
        instance = zlang.sim.load(source, top="BitRegion", engine=engine)
        assert instance.program.plan.payload["regions"]
        with instance:
            instance.set("mask", mask)
            assert instance.eval() == expected


def test_native_signed_shift_compare_and_api_round_trip(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "signed",
        """
        module Signed {
            in value:s8
            in other:s8
            in shift:u3
            out sum:s9=value + other
            out shifted:s8=value >> shift
            out negative:bit=value < 0
        }
        """,
    )
    module = compile_file(source, top="Signed").ir
    instance = zlang.sim.load(source, top="Signed", engine="jit")
    with instance:
        instance.set("value", -4)
        instance.set("other", -127)
        instance.set("shift", 1)
        assert instance.eval() == simulate(module, value=-4, other=-127, shift=1)


def test_native_fixed_conversion_rounding_and_saturation_match_python_oracle(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "fixed_conversion",
        """
        module FixedConversion {
            in value:fixed<8,4>
            out floor_value:fixed<5,2>
            out nearest_value:fixed<5,2>
            out away_value:fixed<5,2>
            out truncate_value:fixed<5,2>
            out saturated_value:fixed<5,2>
            floor_value=quantize<fixed<5,2>>(value) {
                round floor overflow wrap
            }
            nearest_value=quantize<fixed<5,2>>(value) {
                round nearest_even overflow wrap
            }
            away_value=quantize<fixed<5,2>>(value) {
                round away_zero overflow wrap
            }
            truncate_value=quantize<fixed<5,2>>(value) {
                round toward_zero overflow wrap
            }
            saturated_value=quantize<fixed<5,2>>(value) {
                round toward_zero overflow saturate
            }
        }
        """,
    )
    module = compile_file(source, top="FixedConversion").ir
    instance = zlang.sim.load(source, top="FixedConversion", engine="jit")
    with instance:
        # Exhaust all raw encodings.  This covers negative values, exact values,
        # discarded fractions, ties, wraparound and both saturation bounds.
        for value in range(-128, 128):
            instance.set("value", value)
            assert instance.eval() == simulate(module, value=value)


def test_native_fixed_raw_conversions_are_representation_only(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "fixed_raw_conversion",
        """
        module FixedRawConversion {
            in raw:sint<129>
            out value:fixed<129,65>
            out restored:sint<129>
            converted:fixed<129,65>=fixed_raw(raw)
            value=converted
            restored=fixed_to_raw(converted)
        }
        """,
    )
    module = compile_file(source, top="FixedRawConversion").ir
    instance = zlang.sim.load(source, top="FixedRawConversion", engine="jit")
    with instance:
        for raw in (0, 1, -1, (1 << 127) + 7, -(1 << 127) + 9):
            instance.set("raw", raw)
            assert instance.eval() == simulate(module, raw=raw)


@pytest.mark.parametrize(
    "source_type,target_type",
    (
        ("fixed<129,65>", "fixed<67,3>"),
        ("ufixed<129,65>", "ufixed<67,3>"),
    ),
)
def test_native_multi_limb_fixed_rescale_matches_python_oracle(
    tmp_path: Path,
    source_type: str,
    target_type: str,
) -> None:
    source = _source(
        tmp_path,
        "wide_fixed_conversion",
        f"""
        module WideFixedConversion {{
            in value:{source_type}
            out result:{target_type}
            result=quantize<{target_type}>(value) {{
                round nearest_even overflow saturate
            }}
        }}
        """,
    )
    module = compile_file(source, top="WideFixedConversion").ir
    values = [0, 1, (1 << 64) - 1, (1 << 66) + (1 << 61)]
    if source_type.startswith("fixed"):
        values.extend((-1, -(1 << 64) + 3, -(1 << 127) + 7))
    instance = zlang.sim.load(source, top="WideFixedConversion", engine="jit")
    with instance:
        for value in values:
            instance.set("value", value)
            assert instance.eval() == simulate(module, value=value)


def test_native_atomic_register_commit_matches_cycle_oracle(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "swap",
        """
        module Swap {
            clock clk reset rst
            in enable:bit
            out left:u8
            out right:u8
            reg a:u8=1
            reg b:u8=2
            rule swap when enable { a <- b b <- a }
            left=a
            right=b
        }
        """,
    )
    cycles = [{"enable": 1}, {"enable": 0}, {"enable": 1}]
    oracle = simulate_cycles(compile_file(source, top="Swap").ir, cycles)
    instance = zlang.sim.load(source, top="Swap", engine="jit")
    observed = []
    with instance:
        for inputs in cycles:
            instance.set("enable", inputs["enable"])
            observed.append(instance.eval())
            instance.edge("clk")
    assert observed == oracle
    assert observed == [
        {"left": 1, "right": 2},
        {"left": 2, "right": 1},
        {"left": 2, "right": 1},
    ]


def test_native_priority_and_synchronous_reset(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "priority",
        """
        module Priority {
            clock clk reset rst
            in high,low:bit
            out y:u8
            reg value:u8=7
            rule hi when high { value <- 1 }
            rule lo when low { value <- 2 }
            priority hi > lo
            y=value
        }
        """,
    )
    instance = zlang.sim.load(source, top="Priority", engine="jit")
    with instance:
        instance.set("high", 1)
        instance.set("low", 1)
        assert instance.edge("clk") == {"y": 1}
        assert instance.reset("rst", asserted=True) == {"y": 1}
        assert instance.edge("clk") == {"y": 7}
        instance.reset("rst", asserted=False)
        assert instance.edge("clk") == {"y": 1}


def test_native_async_assertion_and_synchronized_release(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "async_reset",
        """
        module AsyncReset {
            clock clk
            async reset arst @clk
            out y:u8
            reg value:u8=7
            value <- 9
            y=value
        }
        """,
    )
    instance = zlang.sim.load(source, top="AsyncReset", engine="jit")
    with instance:
        assert instance.edge("clk") == {"y": 9}
        assert instance.reset("arst", asserted=True) == {"y": 7}
        assert instance.reset("arst", asserted=False) == {"y": 7}
        assert instance.edge("clk") == {"y": 7}
        assert instance.edge("clk") == {"y": 7}
        assert instance.edge("clk") == {"y": 9}


def test_native_batching_and_trace_are_persistent(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "counter",
        """
        module Counter {
            clock clk reset rst
            out y:u8
            reg value:u8=0
            value <- truncate<8>(value+1)
            y=value
        }
        """,
    )
    instance = zlang.sim.load(source, top="Counter", engine="jit")
    with instance:
        instance.enable_trace(["y", "value"])
        assert instance.run_cycles("clk", 4) == {"y": 4}
        assert instance.run_cycles("clk", 3) == {"y": 7}
        trace = instance.drain_trace()
    assert [item["y"] for item in trace] == list(range(1, 8))
    assert [item["$event"] for item in trace] == list(range(1, 8))


def test_native_event_batch_crosses_ffi_once_and_preserves_order(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "event_counter",
        """
        module EventCounter {
            clock clk reset rst
            in step:u8
            out y:u8
            reg value:u8=0
            value <- truncate<8>(value+step)
            y=value
        }
        """,
    )
    instance = zlang.sim.load(source, top="EventCounter", engine="jit")
    with instance:
        assert instance.run_events(
            [
                {"set": {"step": 2}, "edges": ["clk"]},
                {"set": {"step": 3}, "edges": ["clk"]},
                {"set": {"step": 0}},
            ]
        ) == [{"y": 2}, {"y": 5}, {"y": 5}]


def test_native_coincident_clocks_commit_from_one_snapshot(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "multi_clock",
        """
        module MultiClock {
            clock left_clk
            reset left_rst @left_clk
            clock right_clk
            reset right_rst @right_clk
            in left_step:u8 @left_clk
            in right_step:u8 @right_clk
            out left:u8 @left_clk
            out right:u8 @right_clk
            reg left_state:u8 @left_clk=1
            reg right_state:u8 @right_clk=10
            rule StepLeft @left_clk when 1 {
                left_state <- truncate<8>(left_state + left_step)
            }
            rule StepRight @right_clk when 1 {
                right_state <- truncate<8>(right_state + right_step)
            }
            left=left_state
            right=right_state
        }
        """,
    )
    instances = [
        zlang.sim.load(source, top="MultiClock", engine=engine)
        for engine in ("native", "reference")
    ]
    for instance in instances:
        instance.set("left_step", 2)
        instance.set("right_step", 3)
    try:
        assert {tuple(sorted(instance.eval().items())) for instance in instances} == {
            (("left", 1), ("right", 10))
        }
        first = [
            instance.edge_many(["left_clk", "right_clk"])
            for instance in instances
        ]
        assert first[0] == first[1] == {
            "left": 3,
            "right": 13,
        }
        second = [
            instance.edge_many(["right_clk", "left_clk"])
            for instance in instances
        ]
        assert second[0] == second[1] == {
            "left": 5,
            "right": 16,
        }
    finally:
        for instance in instances:
            instance.close()


@pytest.mark.parametrize("width", [65, 127, 128])
def test_native_wide_bitwise_shift_and_api_round_trip(
    tmp_path: Path, width: int
) -> None:
    source = _source(
        tmp_path,
        f"wide_{width}",
        f"""
        module Wide{width} {{
            in left,right:bits<{width}>
            in shift:u7
            out band:bits<{width}>=left & right
            out bxor:bits<{width}>=left ^ right
            out shifted:bits<{width}>=left << shift
        }}
        """,
    )
    mask = (1 << width) - 1
    left = (1 << (width - 1)) | (1 << 64) | 0xA5
    right = (1 << (width - 1)) | 0x5A
    instance = zlang.sim.load(source, top=f"Wide{width}", engine="jit")
    with instance:
        instance.set("left", left)
        instance.set("right", right)
        instance.set("shift", 3)
        assert instance.eval() == {
            "band": left & right,
            "bxor": left ^ right,
            "shifted": (left << 3) & mask,
        }


def test_native_u127_addition_reaches_u128(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "wide_add",
        "module WideAdd { in a,b:uint<127> out y:uint<128>=a+b }",
    )
    a = (1 << 126) | 7
    b = (1 << 126) | 9
    instance = zlang.sim.load(source, top="WideAdd", engine="jit")
    with instance:
        instance.set("a", a)
        instance.set("b", b)
        assert instance.eval() == {"y": a + b}


def test_native_u128_register_initial_and_atomic_commit(tmp_path: Path) -> None:
    initial = (1 << 100) | 0x1234
    replacement = (1 << 127) | (1 << 65) | 0x55
    source = _source(
        tmp_path,
        "wide_state",
        f"""
        module WideState {{
            clock clk reset rst
            in next:uint<128>
            out current:uint<128>
            reg value:uint<128>={initial}
            value <- next
            current=value
        }}
        """,
    )
    instance = zlang.sim.load(source, top="WideState", engine="jit")
    with instance:
        assert instance.eval() == {"current": initial}
        instance.set("next", replacement)
        assert instance.edge("clk") == {"current": replacement}


@pytest.mark.parametrize("width", [129, 257, 512])
def test_native_multi_limb_bitwise_shift_and_compare(
    tmp_path: Path, width: int
) -> None:
    source = _source(
        tmp_path,
        f"multi_limb_{width}",
        f"""
        module MultiLimb{width} {{
            in left,right:uint<{width}>
            in shift:u10
            out band:uint<{width}>=left & right
            out bxor:uint<{width}>=left ^ right
            out shifted_left:uint<{width}>=left << shift
            out shifted_right:uint<{width}>=left >> shift
            out less:bit=left < right
            out equal:bit=left == right
        }}
        """,
    )
    mask = (1 << width) - 1
    left = (1 << (width - 1)) | (1 << 128) | (1 << 64) | 0xA55A
    right = (1 << (width - 2)) | (1 << min(129, width - 3)) | 0x5AA5
    instance = zlang.sim.load(source, top=f"MultiLimb{width}", engine="jit")
    with instance:
        instance.set("left", left)
        instance.set("right", right)
        for shift in (0, 1, 63, 64, 65, width - 1, width, width + 1):
            instance.set("shift", shift)
            assert instance.eval() == {
                "band": left & right,
                "bxor": left ^ right,
                "shifted_left": (left << shift) & mask,
                "shifted_right": left >> shift,
                "less": int(left < right),
                "equal": 0,
            }


def test_native_multi_limb_add_subtract_and_multiply(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "multi_limb_arithmetic",
        """
        module MultiLimbArithmetic {
            in add_left,add_right:uint<255>
            in mul_left,mul_right:uint<192>
            out sum:uint<256>=add_left+add_right
            out difference:uint<255>=add_left-add_right
            out product:uint<384>=mul_left*mul_right
        }
        """,
    )
    add_left = (1 << 254) | (1 << 192) | 7
    add_right = (1 << 254) | (1 << 128) | 11
    mul_left = (1 << 191) | (1 << 127) | 3
    mul_right = (1 << 190) | (1 << 65) | 5
    instance = zlang.sim.load(source, top="MultiLimbArithmetic", engine="jit")
    with instance:
        instance.set("add_left", add_left)
        instance.set("add_right", add_right)
        instance.set("mul_left", mul_left)
        instance.set("mul_right", mul_right)
        assert instance.eval() == {
            "sum": add_left + add_right,
            "difference": (add_left - add_right) & ((1 << 255) - 1),
            "product": mul_left * mul_right,
        }


def test_native_multi_limb_signed_shift_and_compare(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "multi_limb_signed",
        """
        module MultiLimbSigned {
            in value:sint<257>
            in shift:u10
            out shifted:sint<257>=value >> shift
            out negative:bit=value < 0
        }
        """,
    )
    value = -(1 << 220) + 12345
    instance = zlang.sim.load(source, top="MultiLimbSigned", engine="jit")
    with instance:
        instance.set("value", value)
        for shift in (1, 63, 64, 129, 256, 257):
            instance.set("shift", shift)
            expected = -1 if shift >= 257 else value >> shift
            assert instance.eval() == {"shifted": expected, "negative": 1}


def test_native_512_bit_vector_runtime_index(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "wide_vector_index",
        """
        module WideVectorIndex {
            in values:vec<64,u8>
            in index:u6
            out selected:u8=values[index]
        }
        """,
    )
    values = [((index * 37) + 11) & 0xFF for index in range(64)]
    instance = zlang.sim.load(source, top="WideVectorIndex", engine="jit")
    with instance:
        instance.set("values", values)
        for index in (0, 1, 7, 31, 32, 63):
            instance.set("index", index)
            assert instance.eval() == {"selected": values[index]}


def test_native_257_bit_register_and_trace(tmp_path: Path) -> None:
    initial = (1 << 256) | (1 << 129) | 0x1234
    replacement = (1 << 255) | (1 << 130) | 0x55
    source = _source(
        tmp_path,
        "wide_limb_state",
        f"""
        module WideLimbState {{
            clock clk reset rst
            in next:uint<257>
            out current:uint<257>
            reg value:uint<257>={initial}
            value <- next
            current=value
        }}
        """,
    )
    instance = zlang.sim.load(source, top="WideLimbState", engine="jit")
    with instance:
        instance.enable_trace(["current", "value"])
        assert instance.eval() == {"current": initial}
        instance.set("next", replacement)
        assert instance.edge("clk") == {"current": replacement}
        assert instance.drain_trace() == [
            {"$event": 1, "current": replacement, "value": replacement}
        ]


@pytest.mark.parametrize(
    ("name", "expression", "latency"),
    [
        ("NativeDelay", "delay<2>(extend<17>(a))", 2),
        ("NativePipeline", "pipeline(3){a * b + c}", 3),
    ],
)
def test_native_fixed_sequential_expression_matches_python_oracle(
    tmp_path: Path,
    name: str,
    expression: str,
    latency: int,
) -> None:
    source = _source(
        tmp_path,
        name,
        f"""
        module {name} {{
            clock clk reset rst
            in a,b:u8
            in c:u16
            out y:u17={expression}
        }}
        """,
    )
    inputs = [{"a": index + 1, "b": 3, "c": index + 9} for index in range(10)]
    module = create_file_compilation_session(source, top=name).planning.module
    expected = simulate_cycles(module, inputs)
    program = zlang.sim.compile(source, top=name, engine="jit")
    instance = program.create()
    actual = []
    with instance:
        for values in inputs:
            for signal, value in values.items():
                instance.set(signal, value)
            actual.append(instance.eval())
            instance.edge("clk")
        assert actual == expected
        assert actual[:latency] == [{"y": 0}] * latency

        instance.reset("rst", asserted=True)
        instance.edge("clk")
        assert instance.get("y") == 0

    staged_names = {
        register["name"]
        for register in program.plan.payload["registers"]
        if register["name"].startswith("$zlang_jit_stage_")
    }
    assert staged_names


def test_native_initialized_rom_matches_one_cycle_python_oracle(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "native_rom",
        """
        module NativeRom {
            clock clk reset rst
            in address:u3
            out data:u16
            rom table:rom<u16,8> {
                read_latency 1
                init generate(i in 0..8) i * 257
            }
            table.read_address=address
            data=table.read_data
        }
        """,
    )
    inputs = [{"address": value} for value in (7, 1, 6, 2, 5, 3, 4, 0)]
    module = create_file_compilation_session(source, top="NativeRom").planning.module
    expected = simulate_cycles(module, inputs)
    program = zlang.sim.compile(source, top="NativeRom", engine="jit")
    instance = program.create()
    actual = []
    with instance:
        for values in inputs:
            instance.set("address", values["address"])
            actual.append(instance.eval())
            instance.edge("clk")
        assert actual == expected
        assert actual == [
            {"data": 0},
            *({"data": address * 257} for address in (7, 1, 6, 2, 5, 3, 4)),
        ]

        instance.reset("rst", asserted=True)
        instance.edge("clk")
        assert instance.get("data") == 0

    assert {node["op"] for node in program.plan.payload["nodes"]} <= {
        "constant",
        "load_input",
        "load_state",
        "load_event",
        "load_memory",
        "add",
        "sub",
        "mul",
        "and",
        "or",
        "xor",
        "not",
        "shl",
        "lshr",
        "ashr",
        "eq",
        "ult",
        "ule",
        "slt",
        "sle",
        "select",
        "extract_bits",
        "insert_bits",
        "concat_bits",
    }

    corrupted = json.loads(program.plan.to_bytes())
    corrupted["outputs"][0]["node"] = len(corrupted["nodes"])
    with pytest.raises(SimulationPlanError, match="output"):
        SimulationPlan.from_bytes(_resign(corrupted))
    import _zlang_native_sim

    with pytest.raises(ValueError, match="output"):
        _zlang_native_sim.compile_plan_bytes(_resign(corrupted))


@pytest.mark.parametrize(
    ("collision", "collision_result"),
    (("old", 5), ("new", 9), ("no_change", 3)),
)
def test_native_synchronous_memory_matches_python_collision_and_reset_semantics(
    tmp_path: Path,
    collision: str,
    collision_result: int,
) -> None:
    source = _source(
        tmp_path,
        f"native_memory_{collision}",
        f"""
        module NativeMemory {{
            clock clk reset rst
            in read_address:u2
            in write_enable:bit
            in write_address:u2
            in write_data:u8
            out read_data:u8
            memory table:mem<u8,4> {{
                init 5
                read_latency 1
                collision {collision}
                reset {{ contents clear read_data clear }}
            }}
            table.read_address=read_address
            table.write_enable=write_enable
            table.write_address=write_address
            table.write_data=write_data
            read_data=table.read_data
        }}
        """,
    )
    inputs = [
        {
            "read_address": 0,
            "write_enable": 1,
            "write_address": 0,
            "write_data": 3,
        },
        {
            "read_address": 0,
            "write_enable": 0,
            "write_address": 0,
            "write_data": 0,
        },
        {
            "read_address": 1,
            "write_enable": 1,
            "write_address": 1,
            "write_data": 9,
        },
        {
            "read_address": 1,
            "write_enable": 0,
            "write_address": 0,
            "write_data": 0,
        },
    ]
    module = create_file_compilation_session(source, top="NativeMemory").planning.module
    expected = simulate_cycles(module, inputs)
    program = zlang.sim.compile(source, top="NativeMemory", engine="native")
    actual_by_engine = []
    for engine in ("native", "reference"):
        instance = zlang.sim.load(source, top="NativeMemory", engine=engine)
        actual = []
        with instance:
            for values in inputs:
                for name, value in values.items():
                    instance.set(name, value)
                actual.append(instance.eval())
                instance.edge("clk")
            assert actual == expected
            assert actual[-1] == {"read_data": collision_result}

            instance.reset("rst", asserted=True)
            instance.edge("clk")
            instance.reset("rst", asserted=False)
            instance.set("read_address", 0)
            instance.set("write_enable", 0)
            instance.edge("clk")
            assert instance.get("read_data") == 5
        actual_by_engine.append(actual)
    assert actual_by_engine[0] == actual_by_engine[1]

    assert len(program.plan.payload["memories"]) == 1
    corrupted = json.loads(program.plan.to_bytes())
    store = next(
        effect
        for program in corrupted["edge_programs"]
        for effect in program["effects"]
        if effect["op"] == "store_memory"
    )
    store["address"] = len(corrupted["nodes"])
    with pytest.raises(SimulationPlanError, match="memory store"):
        SimulationPlan.from_bytes(_resign(corrupted))
    import _zlang_native_sim

    with pytest.raises(ValueError, match="memory store"):
        _zlang_native_sim.compile_plan_bytes(_resign(corrupted))


@pytest.mark.parametrize("latency", [2, 3])
def test_native_synchronous_memory_preserves_exact_read_latency(
    tmp_path: Path, latency: int
) -> None:
    source = _source(
        tmp_path,
        f"memory_latency_{latency}",
        f"""
        module MemoryLatency {{
            clock clk reset rst
            in ra:u2 in we:bit in wa:u2 in wd:u8 out q:u8
            memory table:mem<u8,4> {{
                init 6 read_latency {latency} collision old
            }}
            table.read_address=ra table.write_enable=we
            table.write_address=wa table.write_data=wd q=table.read_data
        }}
        """,
    )
    inputs = [
        {"ra": address, "we": 0, "wa": 0, "wd": 0} for address in (0, 1, 2, 3, 0, 1)
    ]
    module = create_file_compilation_session(
        source, top="MemoryLatency"
    ).planning.module
    expected = simulate_cycles(module, inputs)
    instance = zlang.sim.load(source, top="MemoryLatency", engine="jit")
    actual = []
    with instance:
        for values in inputs:
            for name, value in values.items():
                instance.set(name, value)
            actual.append(instance.eval())
            instance.edge("clk")
    assert actual == expected


def test_native_synchronous_memory_preserves_arbitrary_width_byte_masks(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "masked_memory",
        """
        module MaskedMemory {
            clock clk reset rst
            in ra:u2 in we:bit in wa:u2
            in wd:bits<80> in wm:bits<10>
            out q:bits<80>
            memory table:mem<bits<80>,4> {
                init 0x112233445566778899aa
                read_latency 1 collision new
            }
            table.read_address=ra table.write_enable=we
            table.write_address=wa table.write_data=wd table.write_mask=wm
            q=table.read_data
        }
        """,
    )
    inputs = [
        {
            "ra": 2,
            "we": 1,
            "wa": 2,
            "wd": 0xFFEEDDCCBBAA99887766,
            "wm": 0b10_0101_0011,
        },
        {"ra": 2, "we": 0, "wa": 0, "wd": 0, "wm": 0},
        {"ra": 0, "we": 0, "wa": 0, "wd": 0, "wm": 0},
    ]
    module = create_file_compilation_session(source, top="MaskedMemory").planning.module
    expected = simulate_cycles(module, inputs)
    instance = zlang.sim.load(source, top="MaskedMemory", engine="jit")
    actual = []
    with instance:
        for values in inputs:
            for name, value in values.items():
                instance.set(name, value)
            actual.append(instance.eval())
            instance.edge("clk")
    assert actual == expected


def test_native_memory_plan_is_depth_independent(
    tmp_path: Path,
) -> None:
    large = _source(
        tmp_path,
        "large_memory",
        """
        module LargeMemory {
            clock clk reset rst
            in ra:u10 in we:bit in wa:u10 in wd:bits<9> out q:bits<9>
            memory table:mem<bits<9>,1024> {
                read_latency 1 collision old
            }
            table.read_address=ra table.write_enable=we
            table.write_address=wa table.write_data=wd q=table.read_data
        }
        """,
    )
    program = zlang.sim.compile(large, top="LargeMemory", engine="jit")
    assert len(program.plan.payload["nodes"]) < 64
    assert len(program.plan.payload["memories"]) == 1


def test_native_jit_accepts_wide_packed_values_but_keeps_a_storage_bound(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "wide",
        "module Wide { in value:bits<513> out result:bits<513>=value }",
    )
    with zlang.sim.load(source, top="Wide", engine="jit") as instance:
        value = (1 << 512) | (1 << 63) | 7
        instance.set("value", value)
        assert instance.eval() == {"result": value}

    oversized = _source(
        tmp_path,
        "oversized",
        "module Oversized { in value:bits<8193> "
        "out result:bits<8193>=value }",
    )
    with pytest.raises(JitUnsupportedFeatureError, match="through 8192 bits"):
        zlang.sim.load(oversized, top="Oversized", engine="jit")


def test_native_jit_keeps_wide_arithmetic_fail_closed(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "wide_arithmetic",
        "module WideArithmetic { in a,b:uint<513> "
        "out y:uint<514>=a+b }",
    )
    with pytest.raises(SimulationPlanError, match="512-bit arithmetic bound"):
        zlang.sim.load(source, top="WideArithmetic", engine="jit")


def test_cli_sim_preserves_compile_cli_and_emits_json(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "cli",
        "module Cli { in a,b:u8 out y:u9=a+b }",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from zlang.cli import main; raise SystemExit(main())",
            "sim",
            str(source),
            "--top",
            "Cli",
            "--engine",
            "jit",
            "--set",
            "a=40",
            "--set",
            "b=2",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == '{"y":42}\n'
