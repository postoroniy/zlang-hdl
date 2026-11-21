# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Elaborated clock provenance and typed native simulation boundaries."""

from copy import deepcopy
from dataclasses import replace
import json
import os
import re
import subprocess

import pytest

from zlang import sim, simulation_compare
from zlang.compiler import compile_source
from zlang.ir.runtime_values import TaggedUnionValue
from zlang.semantic import SemanticError
from zlang.simulation_values import SimulationRuntimeError


_CDC_PREFIX = """
module Pass { in x:bit out y:bit=x }
module Pair { in lo,hi:bit out y:bits<2>=concat(hi,lo) }
module Two { in hi,lo:bit out left:bit=hi out right:bit=lo }
module Top {
  clock ca reset ra @ca
  clock cb reset rb @cb
  in a:bit @ca in b:bit @cb
  reg held:bit @ca=0 out reg q:bit @cb=0
  held <- a
"""

_CLOCKED_CHILDREN = {
    "reg": "module State {clock ca reset ra @ca out reg value:bit @ca=0 value<-1}",
    "ram": """
module State {
    clock ca reset ra @ca
    out value:bit @ca
    memory m:mem<bit,2> @ca {read_latency 1 collision read_first}
    m.read_address=0 m.write_address=0 m.write_enable=1 m.write_data=1
    value=m.read_data
}
""",
}

_TWO_DOMAINS = "clock ca reset ra @ca clock cb reset rb @cb"


@pytest.mark.parametrize("via_child", [False, True])
def test_multiclock_delay_requires_a_single_domain_at_the_source(via_child):
    base = "module Pass {in x:bit out y:bit=x} module Top {"
    binding = "p:Pass {x=a}" if via_child else ""
    operand = "p.y" if via_child else "a"
    body = f"in a:bit @ca {binding} out q:bit @ca=delay<1>({operand})"
    with pytest.raises(SemanticError, match="delay<N> requires a single clock domain") as error:
        compile_source(f"{base} {_TWO_DOMAINS} {body} }}", top="Top")
    assert error.value.code == "ZL-DELAY-DOMAIN"
    assert error.value.primary is not None
    compile_source(f"{base} clock ca reset ra @ca {body} }}", top="Top")


@pytest.mark.parametrize("consumer_domain", ["ca", "cb"])
def test_credit_send_in_child_binding_fails_at_the_source(consumer_domain):
    alias = "sent=rx.send" if consumer_domain == "cb" else ""
    operand = "sent" if alias else "rx.send"
    with pytest.raises(SemanticError, match="credit interface fields cannot drive") as error:
        compile_source(f"""
            module Pass {{in x:bit out y:bit=x}}
            module Top {{
                {_TWO_DOMAINS}
                in rx:credit<u8,2> @ca rx.return=0
                {alias}
                p:Pass {{x={operand}}}
                out reg q:bit @{consumer_domain}=0 q<-p.y
            }}
        """, top="Top")
    assert error.value.code == "ZL-HIERARCHY-CREDIT-BINDING"
    assert error.value.primary is not None


def _checked_rtl_ports(tmp_path, rtl, top="Top"):
    (tmp_path / "design.sv").write_text(rtl)
    run = subprocess.run(
        ("yosys", "-Q", "-q", "-p",
         f"read_verilog -sv design.sv; hierarchy -check -top {top}; "
         "proc; flatten; opt; check -assert; write_json netlist.json"),
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    ports = json.loads((tmp_path / "netlist.json").read_text())["modules"][top]["ports"]
    return {name: (port["direction"], len(port["bits"])) for name, port in ports.items()}


@pytest.mark.parametrize("crossing", [False, True])
@pytest.mark.parametrize("stateful", [False, True])
@pytest.mark.parametrize(("expression", "reference"), [
    ("d+e", lambda d, e: d + e),
    ("d*e", lambda d, e: d * e),
    ("(d+e)*(d-e)", lambda d, e: (d + e) * (d - e)),
])
def test_local_expression_drives_both_outputs_beside_cdc(
    tmp_path, monkeypatch, crossing, stateful, expression, reference,
):
    cdc = (
        "in a:bit @ca out synced:bit @cb connect a -> synced {crossing sync_level}"
        if crossing else ""
    )
    state = "out reg q:u8 @cb=0 q<-z" if stateful else ""
    source = tmp_path / "top.zhl"
    source.write_text(
        f"module Top {{ {_TWO_DOMAINS} {cdc} in d,e:u8 @cb "
        f"z=truncate<8>({expression}) out result:u8 @cb=z out inverted:u8 @cb=~z {state} }}"
    )
    program = sim.compile(source, top="Top")
    vectors = [(0, 0), (3, 5), (201, 57)]
    events = [
        {"reset": {"ra": index == 0, "rb": index == 0},
         "set": {"d": d, "e": e}, "edges": ["ca", "cb"]}
        for index, (d, e) in enumerate(vectors)
    ]
    expected = []
    for d, e in vectors:
        value = reference(d, e) & 255
        row = {"result": value, "inverted": 255 ^ value}
        if crossing:
            row["synced"] = 0
        if stateful:
            row["q"] = value
        expected.append(row)
    rv = simulation_compare.compare_program(
        program, events, simulator="iverilog", artifact_directory=tmp_path / "icarus",
        timeout=15, build_timeout=15,
    )
    assert rv.native_trace == rv.rtl_trace == tuple(expected)
    rtl = (tmp_path / "icarus" / "design.sv").read_text()
    _checked_rtl_ports(tmp_path, rtl)
    if crossing and stateful and expression == "d+e":
        # This three-event trace needs a quick cold build of the C++ simulator.
        monkeypatch.setenv("CXXFLAGS", (os.environ.get("CXXFLAGS", "") + " -O0").strip())
        verilator = simulation_compare.compare_program(
            program, events, simulator="verilator",
            artifact_directory=tmp_path / "verilator", timeout=15, build_timeout=15,
        )
        assert verilator.native_trace == verilator.rtl_trace == tuple(expected)


@pytest.mark.parametrize("crossing", ["sync_level", "pulse_toggle", "handshake", "async_fifo(4)"])
def test_cdc_keeps_unused_named_port_and_protocol_abi(tmp_path, crossing):
    scalar = crossing in {"sync_level", "pulse_toggle"}
    payload = "bit" if scalar else "rv<u8>"
    collisions = {
        "handshake": ("zlang_held_payload",),
        "async_fifo(4)": ("zlang_full", "zlang_fifo_write_address", "fifo_storage_cells"),
    }.get(crossing, ())
    extra = " ".join(f"in {name}:bit @ca" for name in collisions)
    source = tmp_path / "top.zhl"
    source.write_text(
        f"module Top {{ {_TWO_DOMAINS} in a:{payload} @ca out y:{payload} @cb "
        f"in spare:u3 @ca {extra} connect a -> y {{crossing {crossing}}} }}"
    )
    drives = {"a": 0} if scalar else {"a": {"payload": 0, "valid": 0}, "y": {"ready": 1}}
    program = sim.compile(source, top="Top")
    trace = simulation_compare.compare_program(
        program, [{"reset": {"ra": True, "rb": True}, "set": {**drives, "spare": 5},
                   "edges": ["ca", "cb"]}],
        simulator="iverilog", artifact_directory=tmp_path / "icarus",
        timeout=15, build_timeout=15,
    )
    assert trace.native_trace == trace.rtl_trace
    rtl = (tmp_path / "icarus" / "design.sv").read_text()
    ports = _checked_rtl_ports(tmp_path, rtl)
    expected = {name: ("input", 1) for name in ("ca", "ra", "cb", "rb")}
    expected["spare"] = ("input", 3)
    expected.update((name, ("input", 1)) for name in collisions)
    if scalar:
        expected.update(a=("input", 1), y=("output", 1))
    else:
        expected.update(a_payload=("input", 8), a_valid=("input", 1),
                        a_ready=("output", 1), y_payload=("output", 8),
                        y_valid=("output", 1), y_ready=("input", 1))
    assert ports == expected


def test_cdc_leaf_keeps_unused_named_child_input(tmp_path):
    source = tmp_path / "top.zhl"
    source.write_text(f"""
    module Leaf {{ {_TWO_DOMAINS}
      in a:bit @ca out y:bit @cb in spare:u3 @ca
      connect a -> y {{crossing sync_level}}
    }}
    module Top {{ {_TWO_DOMAINS}
      in a:bit @ca in spare:u3 @ca out y:bit @cb
      leaf:Leaf {{a=a spare=spare}} y=leaf.y
    }}
    """)
    program = sim.compile(source, top="Top")
    result = simulation_compare.compare_program(
        program, [
            {"reset": {"ra": True, "rb": True}, "edges": ["ca", "cb"]},
            {"reset": {"ra": False, "rb": False}, "set": {"a": 1, "spare": 5}, "edges": ["cb"]},
            {"edges": ["cb"]},
        ],
        simulator="iverilog", artifact_directory=tmp_path / "icarus",
        timeout=15, build_timeout=15,
    )
    assert result.native_trace == result.rtl_trace == ({"y": 0}, {"y": 0}, {"y": 1})
    _checked_rtl_ports(tmp_path, (tmp_path / "icarus" / "design.sv").read_text())


@pytest.mark.parametrize("destination_collision", [False, True])
@pytest.mark.parametrize(("crossing", "child"), [
    ("sync_level", False), ("sync_level", True), ("pulse_toggle", False),
])
def test_cdc_private_signal_names_yield_to_source_ports(tmp_path, crossing, child, destination_collision):
    preferred = "zlang_sync_stage1" if crossing == "sync_level" else "zlang_previous_toggle"
    input_name, output_name = ("a", preferred) if destination_collision else (preferred, "y")
    instances = f"p:Pass {{x={output_name}}} out tap:bit @cb=p.y" if child else ""
    source_text = (
        "module Pass {in x:bit out y:bit=x} "
        f"module Top {{ {_TWO_DOMAINS} in {input_name}:bit @ca out {output_name}:bit @cb "
        f"connect {input_name} -> {output_name} {{crossing {crossing}}} {instances} }}"
    )
    source = tmp_path / "top.zhl"
    source.write_text(source_text)
    program = sim.compile(source, top="Top")
    trace = simulation_compare.compare_program(
        program, [
            {"reset": {"ra": True, "rb": True}, "edges": ["ca", "cb"]},
            {"reset": {"ra": False, "rb": False}, "set": {input_name: 1}, "edges": ["ca"]},
            {"edges": ["cb"]}, {"edges": ["cb"]}, {"edges": ["cb"]},
            {"reset": {"ra": True, "rb": True}, "edges": ["ca", "cb"]},
        ],
        simulator="iverilog", artifact_directory=tmp_path / "icarus",
        timeout=15, build_timeout=15,
    )
    values = (0, 0, 0, 1, int(crossing == "sync_level"), 0)
    expected = tuple({output_name: v, **({"tap": v} if child else {})} for v in values)
    assert trace.native_trace == trace.rtl_trace == expected
    rtl = (tmp_path / "icarus" / "design.sv").read_text()
    synchronized = re.findall(r'\(\* ASYNC_REG = "TRUE" \*\) logic ([^;]+);', rtl)
    assert len(synchronized) == 1 and len(synchronized[0].split(",")) == 2
    assert re.search(rf"\b{preferred}_[0-9a-f]{{8}}\b", rtl)
    ports = _checked_rtl_ports(tmp_path, rtl)
    assert ports[input_name] == ("input", 1)
    assert ports[output_name] == ("output", 1)


@pytest.mark.parametrize("storage", tuple(_CLOCKED_CHILDREN))
def test_clocked_child_output_keeps_its_domain_through_a_pass(storage):
    child = _CLOCKED_CHILDREN[storage]
    body = "s:State p:Pass {x=s.value} q<-p.y}"
    source = child + _CDC_PREFIX + body
    with pytest.raises(SemanticError, match=r"through 'p.y'.*from 'ca' to 'cb'") as error:
        compile_source(source, top="Top")
    assert error.value.code == "ZL-DOMAIN-CROSSING"
    compile_source(source.replace("reg q:bit @cb", "reg q:bit @ca"), top="Top")


@pytest.mark.parametrize("simulator", ["iverilog", "verilator"])
@pytest.mark.parametrize("placement", ["leaf", "same_module"])
def test_explicit_synchronizer_domain_keeps_the_hierarchical_read_legal(
    tmp_path, simulator, placement,
):
    leaf = """
    module Crossing {
      clock ca reset ra @ca
      clock cb reset rb @cb
      in source:bit @ca out synced:bit @cb
      connect source -> synced {crossing sync_level}
    }
    """
    leaf_body = """
      c:Crossing {source=a}
      p:Pass {x=c.synced}
      out synchronized:bit @cb=c.synced
    """
    local_body = """
      out synchronized:bit @cb
      connect a -> synchronized {crossing sync_level}
      p:Pass {x=synchronized}
    """
    source = tmp_path / "top.zhl"
    crossing = leaf_body if placement == "leaf" else local_body
    source.write_text((leaf if placement == "leaf" else "") + _CDC_PREFIX + crossing + "q<-p.y}")
    events = [
        {"reset": {"ra": True, "rb": True}, "edges": ["ca", "cb"]},
        {"reset": {"ra": False, "rb": False}, "set": {"a": 1}, "edges": ["ca", "cb"]},
        {"edges": ["cb"]},
        {"edges": ["cb"]},
        {"reset": {"rb": True}, "edges": ["cb"]},
    ]
    program = sim.compile(source, top="Top")
    result = simulation_compare.compare_program(program, events, simulator=simulator)
    assert result.native_trace == result.rtl_trace == (
        {"synchronized": 0, "q": 0}, {"synchronized": 0, "q": 0},
        {"synchronized": 1, "q": 0}, {"synchronized": 1, "q": 1},
        {"synchronized": 0, "q": 0},
    )


@pytest.mark.parametrize("body", [
    "q <- held",
    "u:Pass {x=held} when b {when u.y {q<-1}}",
    "u:Pass {x=held} out crossing:bit @cb=u.y q<-b",
    "u:Pass {x=held} v:Pass {x=u.y} q<-v.y",
    "u:Pass {x=b} v:Pass {x=held} q<-v.y",
    "u:Pair {hi=held lo=b} q<-bitcast<bit>(u.y[1])",
    "u:Two {hi=held lo=b} q<-u.left",
])
def test_hierarchical_foreign_clock_is_rejected_at_its_use(body):
    with pytest.raises(SemanticError) as error:
        compile_source(_CDC_PREFIX + body + "}", top="Top", source_unit="clock_test.zhl")
    assert error.value.code == "ZL-DOMAIN-CROSSING"
    assert "'ca'" in str(error.value) and "'cb'" in str(error.value)
    assert error.value.primary is not None


@pytest.mark.parametrize(("body", "expected"), [
    ("u:Pass {x=held} v:Pass {x=b} q<-v.y", [0, 0, 1, 0]),
    ("u:Pair {hi=held lo=b} q<-bitcast<bit>(u.y[0])", [0, 0, 1, 0]),
    ("u:Two {hi=held lo=b} q<-u.right", [0, 0, 1, 0]),
    ("u:Pass {x=1} q<-u.y", [0, 0, 1, 1]),
])
def test_hierarchical_clock_check_keeps_exact_bound_outputs(tmp_path, body, expected):
    source = tmp_path / "top.zhl"
    source.write_text(_CDC_PREFIX + body + "}")
    events = [
        {"reset": {"ra": True, "rb": True}, "edges": ["ca", "cb"]},
        {"reset": {"ra": False, "rb": False}, "set": {"a": 1, "b": 1}, "edges": ["ca"]},
        {"edges": ["cb"]},
        {"set": {"b": 0}, "edges": ["cb"]},
    ]
    result = simulation_compare.compare_program(sim.compile(source, top="Top"), events, simulator="iverilog")
    assert result.native_trace == result.rtl_trace == tuple({"q": q} for q in expected)


_RR = "{max_outstanding 2 ordering in_order}"


def test_local_request_response_needs_one_owning_domain(tmp_path):
    source = tmp_path / "client.zhl"
    source.write_text(
        "module Client {\n"
        "clock a reset ra @a clock b reset rb @b\n"
        f"interface bus:request_response<u8,u8>{_RR}\n"
        "bus.request.payload=0 bus.request.valid=0 bus.response.ready=1\n"
        "out reg x:u8 @a=0 out reg y:u8 @b=0\n"
        "x<-bus.response.payload y<-bus.response.payload }\n"
    )
    for compile_design in (lambda: compile_source(source.read_text()), lambda: sim.compile(source)):
        with pytest.raises(SemanticError, match="requires one owning clock and reset") as caught:
            compile_design()
        assert caught.value.code == "ZL-DOMAIN-CROSSING"
        assert caught.value.primary.span.start_line == 3


_ENDPOINTS = {
    "rv": (
        "in m:rv<u8> m.ready=1 seen=m.payload gate=m.valid captured<-m.payload",
        {"payload": 3, "valid": 1}, {"payload": 13, "valid": 2}, (1, 3, 3),
    ),
    "requester": (
        f"interface m:request_response<u8,u8>{_RR} "
        "m.request.payload=0 m.request.valid=0 m.response.ready=0 "
        "seen=m.response.payload gate=m.request.ready captured<-m.response.payload",
        {"request": {"ready": 0}, "response": {"payload": 3, "valid": 0}},
        {"request": {"ready": 1}, "response": {"payload": 13, "valid": 2}}, (0, 3, 3),
    ),
    "responder": (
        f"interface m:request_response<u8,u8>{_RR} "
        "m.request.ready=0 m.response.payload=0 m.response.valid=0 "
        "seen=m.request.payload gate=m.response.ready captured<-m.request.payload",
        {"request": {"payload": 3, "valid": 0}, "response": {"ready": 0}},
        {"request": {"payload": 13, "valid": 2}, "response": {"ready": 1}}, (0, 3, 3),
    ),
}


@pytest.mark.parametrize("logic_state", [False, True])
@pytest.mark.parametrize("endpoint", list(_ENDPOINTS))
@pytest.mark.parametrize("use_events", [False, True])
def test_protocol_update_is_atomic_on_a_bad_field(tmp_path, logic_state, endpoint, use_events):
    body, good, bad, expected = _ENDPOINTS[endpoint]
    source = tmp_path / "top.zhl"
    source.write_text(
        "module Top { clock clk reset rst out seen:u8 out gate:bit "
        "out reg captured:u8=0 " + body + "}"
    )
    with sim.load(source, logic_state=logic_state) as instance:
        instance.set("m", good)
        instance.edge("clk")
        wider = deepcopy(good)
        unowned = deepcopy(good)
        incoming = "response" if endpoint == "requester" else "request"
        payload = wider if endpoint == "rv" else wider[incoming]
        payload["payload"] = 256
        output = unowned if endpoint == "rv" else unowned[incoming]
        output["ready"] = 1
        for value, diagnostic in (
            (bad, "value 2 does not fit bit"), (wider, "256 does not fit"),
            (unowned, "requires fields"), ([], "mapping"),
        ):
            with pytest.raises(SimulationRuntimeError, match=diagnostic):
                if use_events:
                    instance.run_events([{"set": {"m": value}}])
                else:
                    instance.set("m", value)
            instance.edge("clk")
            assert tuple(instance.get(name) for name in ("gate", "seen", "captured")) == expected


@pytest.mark.parametrize("logic_state", [False, True])
def test_protocol_input_rejects_same_width_wrong_nominal_union(tmp_path, logic_state):
    source = tmp_path / "top.zhl"
    source.write_text("""
union Command { Idle Put { data:u8 } }
union Other { Idle Put { data:u8 } }
module Top {
    clock clk reset rst
    in m:rv<Command> in unrelated:Other
    out captured:Command
    m.ready=1
    captured=m.payload
}
""")
    program = sim.compile(source, logic_state=logic_state)
    ports = {port.name: port for port in program.module.ports}
    good = TaggedUnionValue(ports["m"].type, "Put", (("data", 3),))
    wrong = TaggedUnionValue(ports["unrelated"].type, "Put", (("data", 13),))
    with program.create() as instance:
        instance.set("m", {"valid": 1, "payload": good})
        for use_events in (False, True):
            value = {"valid": 0, "payload": wrong}
            with pytest.raises(SimulationRuntimeError, match="nominal"):
                if use_events:
                    instance.run_events([{"set": {"m": value}}])
                else:
                    instance.set("m", value)
            instance.edge("clk")
            assert instance.get_packed("captured") == 259
            assert instance.get("m")["transfer"] == 1


@pytest.mark.parametrize("logic_state", [False, True])
def test_streamed_events_commit_before_pulling_the_next_item(tmp_path, logic_state):
    source = tmp_path / "top.zhl"
    source.write_text("module Top {clock clk reset rst in d:u8 out reg q:u8=0 q<-d}")
    with sim.load(source, logic_state=logic_state) as instance:
        instance.run_events([{"set": {"d": 37}, "edges": ["clk"]}])

        def events():
            yield {"set": {"d": 41}, "edges": ["clk"]}
            yield {"set": {"d": instance.get("q") + 1}, "edges": ["clk"]}

        instance.run_events(events())
        assert instance.get("q") == 42

        failure = ValueError("source stopped")

        def interrupted():
            yield {"set": {"d": 41}, "edges": ["clk"]}
            raise failure

        with pytest.raises(ValueError) as error:
            instance.run_events(interrupted())
        assert error.value is failure
        assert instance.get("q") == 41

        def malformed():
            yield {"set": {"d": 43}, "edges": ["clk"]}
            yield {"set": {"d": 99}, "edges": ["missing"]}
            pytest.fail("pulled a new event after the rejected event")

        with pytest.raises(SimulationRuntimeError, match="unknown clock 'missing'"):
            instance.run_events(malformed())
        assert instance.get("q") == 43


@pytest.mark.parametrize(("channel", "types", "other"), [
    ("request", "Msg,u8", "m.response.ready=1"),
    ("response", "u8,Msg", "m.request.ready=1"),
])
@pytest.mark.parametrize("simulator", ["iverilog", "verilator"])
def test_union_payload_compares_as_abi_bits(tmp_path, simulator, channel, types, other):
    source = tmp_path / "top.zhl"
    source.write_text(
        "union Msg { Idle Put { data:s8 } Short { code:bits<2> } } "
        "module Top { clock clk reset rst in select:u2 "
        f"interface m:request_response<{types}>{_RR} {other} "
        f"m.{channel}.payload=select == 1 ? Msg.Put {{data=37}} : "
        "(select == 2 ? Msg.Short {code=2} : (select == 3 ? Msg.Put {data=-7} : Msg.Idle)) "
        f"m.{channel}.valid=select != 0 }}"
    )
    program = sim.compile(source)
    events = _union_events(channel)
    result = simulation_compare.compare_program(program, events, simulator=simulator)
    for trace in (result.native_trace, result.rtl_trace):
        assert [(row[f"m_{channel}_payload"], row[f"m_{channel}_valid"]) for row in trace] == [
            (0, 0), (293, 1), (640, 1), (505, 1),
        ]


def _union_events(channel):
    endpoint = (
        {"request": {"ready": 1}, "response": {"payload": 0, "valid": 0}}
        if channel == "request"
        else {"request": {"payload": 0, "valid": 1}, "response": {"ready": 1}}
    )
    return [
        {"set": {"select": 0, "m": endpoint}, "reset": {"rst": True}, "edges": ["clk"]},
        {"set": {"select": 1}, "reset": {"rst": False}},
        {"set": {"select": 2}},
        {"set": {"select": 3}},
    ]


def test_valid_union_payload_corruption_is_detected(tmp_path, monkeypatch):
    source = tmp_path / "top.zhl"
    source.write_text(
        "union Msg { Idle Put { data:s8 } } module Top { clock clk reset rst in select:u2 "
        f"interface m:request_response<Msg,u8>{_RR} m.response.ready=1 "
        "m.request.payload=select != 0 ? Msg.Put {data=37} : Msg.Idle "
        "m.request.valid=select != 0 }"
    )
    emit = simulation_compare.emit_artifact

    def mutated(*args, **kwargs):
        artifact = emit(*args, **kwargs)
        text, count = re.subn(
            r"(?m)^(\s*assign m_request_payload = )([^;]+);",
            r"\1(\2) ^ (m_request_valid ? 9'd1 : 9'd0);",
            artifact.text,
        )
        assert count == 1
        return replace(artifact, text=text)

    monkeypatch.setattr(simulation_compare, "emit_artifact", mutated)
    with pytest.raises(simulation_compare.SimulationComparisonError, match="m_request_payload"):
        simulation_compare.compare_program(sim.compile(source), _union_events("request"), simulator="iverilog")
