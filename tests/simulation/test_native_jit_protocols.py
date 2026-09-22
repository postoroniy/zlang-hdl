"""Ready/valid is erased before the primitive simulation boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import zlang
from tests.simulation.differential import run_differential
from zlang.compiler import compile_source
from zlang.simulate import (
    ProtocolViolation,
    simulate_connection_cycles,
    simulate_credit_cycles,
    simulate_packet_arbiter_cycles,
    simulate_request_response_cycles,
    simulate_vc_credit_cycles,
)


ROOT = Path(__file__).resolve().parents[2]


def _source(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / f"{name}.zhl"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_ready_valid_passthrough_keeps_public_api_and_primitive_plan(
    engine: str,
) -> None:
    program = zlang.sim.compile(
        ROOT / "examples/rv_passthrough.zhl",
        top="RvPassthrough",
        engine=engine,
    )
    encoded = json.dumps(program.plan.payload, sort_keys=True)
    assert '"op": "ready_valid' not in encoded
    assert '"op": "transfer' not in encoded
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

    with program.create() as instance:
        instance.set("rx", {"payload": 42, "valid": 1})
        instance.set("tx", {"ready": 0})
        assert instance.eval() == {
            "rx": {"ready": 0, "transfer": 0},
            "tx": {"payload": 42, "valid": 1, "transfer": 0},
        }
        instance.set("tx", {"ready": 1})
        assert instance.eval() == {
            "rx": {"ready": 1, "transfer": 1},
            "tx": {"payload": 42, "valid": 1, "transfer": 1},
        }


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_transfer_is_compiler_lowered_into_atomic_state_effect(
    tmp_path: Path,
    engine: str,
) -> None:
    source = _source(
        tmp_path,
        "rv_state",
        """
        module RvState {
            clock clk reset rst
            in rx:rv<u8>
            out tx:rv<u8>
            reg seen:u8=0

            rx.ready=tx.ready
            tx.payload=seen
            tx.valid=rx.valid
            rule take when rx.transfer { seen <- rx.payload }
        }
        """,
    )
    with zlang.sim.load(source, top="RvState", engine=engine) as instance:
        instance.set("rx", {"payload": 9, "valid": 1})
        instance.set("tx", {"ready": 1})
        assert instance.eval()["tx"]["payload"] == 0
        result = instance.edge("clk")
        assert result == {
            "rx": {"ready": 1, "transfer": 1},
            "tx": {"payload": 9, "valid": 1, "transfer": 1},
        }


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_ready_valid_run_events_remains_batched_and_grouped(engine: str) -> None:
    with zlang.sim.load(
        ROOT / "examples/rv_passthrough.zhl",
        top="RvPassthrough",
        engine=engine,
    ) as instance:
        assert instance.run_events(
            (
                {
                    "set": {
                        "rx": {"payload": 7, "valid": 1},
                        "tx": {"ready": 0},
                    }
                },
                {"set": {"tx": {"ready": 1}}},
            )
        ) == [
            {
                "rx": {"ready": 0, "transfer": 0},
                "tx": {"payload": 7, "valid": 1, "transfer": 0},
            },
            {
                "rx": {"ready": 1, "transfer": 1},
                "tx": {"payload": 7, "valid": 1, "transfer": 1},
            },
        ]


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_direct_ready_valid_hierarchy_is_erased_before_runtime(
    tmp_path: Path,
    engine: str,
) -> None:
    source = _source(
        tmp_path,
        "rv_hierarchy",
        """
        module Pass {
            in rx:rv<u8>
            out tx:rv<u8>
            tx.payload=rx.payload
            tx.valid=rx.valid
            rx.ready=tx.ready
        }
        module Top {
            in source:rv<u8>
            out sink:rv<u8>
            inst pass:Pass
            connect source -> pass.rx
            connect pass.tx -> sink
        }
        """,
    )
    program = zlang.sim.compile(source, top="Top", engine=engine)
    assert program.plan.payload["canonical_ir_identity"].startswith("hierarchical:")
    encoded = json.dumps(program.plan.payload, sort_keys=True)
    assert '"instances"' not in encoded
    assert '"connections"' not in encoded
    with program.create() as instance:
        instance.set("source", {"payload": 23, "valid": 1})
        instance.set("sink", {"ready": 1})
        assert instance.eval() == {
            "source": {"ready": 1, "transfer": 1},
            "sink": {"payload": 23, "valid": 1, "transfer": 1},
        }


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_direct_leaf_connect_uses_the_same_primitive_lowering(engine: str) -> None:
    with zlang.sim.load(
        ROOT / "examples/rv_connect.zhl",
        top="RvConnect",
        engine=engine,
    ) as instance:
        instance.set("rx", {"payload": 5, "valid": 1})
        instance.set("tx", {"ready": 1})
        assert instance.eval()["tx"] == {
            "payload": 5,
            "valid": 1,
            "transfer": 1,
        }


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_buffered_protocol_connection_reuses_primitive_fifo_lowering(
    engine: str,
) -> None:
    program = zlang.sim.compile(
        ROOT / "examples/rv_buffer.zhl",
        top="RvBuffer",
        engine=engine,
    )
    encoded = json.dumps(program.plan.payload, sort_keys=True)
    assert '"op": "ready_valid' not in encoded
    assert '"connections"' not in encoded
    assert len(program.plan.payload["memories"]) == 1
    with program.create() as instance:
        instance.reset("rst", asserted=True)
        instance.edge("clk")
        instance.reset("rst", asserted=False)
        assert instance.run_events(
            (
                {
                    "set": {
                        "rx": {"payload": 5, "valid": 1},
                        "tx": {"ready": 0},
                    },
                    "edges": ["clk"],
                },
                {
                    "set": {"rx": {"payload": 6, "valid": 1}},
                    "edges": ["clk"],
                },
                {
                    "set": {
                        "rx": {"payload": 7, "valid": 1},
                        "tx": {"ready": 1},
                    },
                    "edges": ["clk"],
                },
            )
        ) == [
            {
                "rx": {"ready": 1, "transfer": 1},
                "tx": {"payload": 5, "valid": 1, "transfer": 0},
            },
            {
                "rx": {"ready": 0, "transfer": 0},
                "tx": {"payload": 5, "valid": 1, "transfer": 0},
            },
            {
                "rx": {"ready": 1, "transfer": 1},
                "tx": {"payload": 6, "valid": 1, "transfer": 1},
            },
        ]


def test_buffered_ready_valid_matches_direct_systemverilog(tmp_path: Path) -> None:
    trace = run_differential(
        ROOT / "examples/rv_buffer.zhl",
        top="RvBuffer",
        events=(
            {"reset": {"rst": True}, "edges": ["clk"]},
            {"reset": {"rst": False}},
            {
                "set": {
                    "rx": {"payload": 11, "valid": 1},
                    "tx": {"ready": 0},
                },
                "edges": ["clk"],
            },
            {
                "set": {"rx": {"payload": 12, "valid": 1}},
                "edges": ["clk"],
            },
            {
                "set": {"tx": {"ready": 1}},
                "edges": ["clk"],
            },
        ),
        directory=tmp_path,
    )
    assert trace.reference == trace.native == trace.direct_sv


def test_hierarchical_buffered_ready_valid_is_compiler_flattened(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "rv_hierarchical_buffer",
        """
        module Pass {
            in rx:rv<u8>
            out tx:rv<u8>
            tx.payload=rx.payload
            tx.valid=rx.valid
            rx.ready=tx.ready
        }
        module Top {
            clock clk reset rst
            in source:rv<u8>
            out sink:rv<u8>
            inst pass:Pass
            connect source -> pass.rx { buffer 2 }
            connect pass.tx -> sink
        }
        """,
    )
    events = (
        {"reset": {"rst": True}, "edges": ["clk"]},
        {"reset": {"rst": False}},
        {
            "set": {
                "source": {"payload": 31, "valid": 1},
                "sink": {"ready": 0},
            },
            "edges": ["clk"],
        },
        {"set": {"sink": {"ready": 1}}, "edges": ["clk"]},
    )
    trace = run_differential(source, top="Top", events=events, directory=tmp_path)
    assert trace.reference == trace.native == trace.direct_sv


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_credit_sender_is_lowered_to_primitive_counter_state(engine: str) -> None:
    source = (ROOT / "examples/credit_source.zhl").read_text(encoding="utf-8")
    module = compile_source(source).ir
    cycles = [
        {"payload_data": 10, "request": 1, "tx": {"return": 0}},
        {"payload_data": 11, "request": 1, "tx": {"return": 0}},
        {"payload_data": 12, "request": 1, "tx": {"return": 0}},
        {"payload_data": 13, "request": 1, "tx": {"return": 1}},
        {"payload_data": 14, "request": 1, "tx": {"return": 0}},
    ]
    expected = simulate_credit_cycles(module, cycles)
    program = zlang.sim.compile(
        ROOT / "examples/credit_source.zhl",
        top="CreditSource",
        engine=engine,
    )
    encoded = json.dumps(program.plan.payload, sort_keys=True)
    assert '"op": "credit' not in encoded
    assert '"protocol"' not in encoded
    with program.create() as instance:
        actual = []
        for values in cycles:
            instance.set("payload_data", values["payload_data"])
            instance.set("request", values["request"])
            instance.set("tx", values["tx"])
            actual.append(instance.eval())
            instance.edge("clk")
    assert actual == expected


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_credit_receiver_bounds_are_generic_runtime_checks(
    tmp_path: Path,
    engine: str,
) -> None:
    source = _source(
        tmp_path,
        "credit_receiver",
        """
        module CreditReceiver {
            clock clk reset rst
            in release:bit
            in rx:credit<u8,2>
            rx.return=release
        }
        """,
    )
    program = zlang.sim.compile(source, top="CreditReceiver", engine=engine)
    assert {
        event["metadata"]["category"] for event in program.plan.payload["events"]
    } == {"runtime_violation"}
    with program.create() as instance:
        instance.set("release", 1)
        instance.set("rx", {"payload": 0, "send": 0})
        with pytest.raises(ProtocolViolation, match="underflow"):
            instance.edge("clk")

    with program.create() as instance:
        instance.set("release", 0)
        for payload in (1, 2):
            instance.set("rx", {"payload": payload, "send": 1})
            instance.edge("clk")
        instance.set("rx", {"payload": 3, "send": 1})
        with pytest.raises(ProtocolViolation, match="maximum occupancy"):
            instance.edge("clk")


def test_credit_sender_matches_direct_systemverilog(tmp_path: Path) -> None:
    trace = run_differential(
        ROOT / "examples/credit_source.zhl",
        top="CreditSource",
        events=(
            {"reset": {"rst": True}, "edges": ["clk"]},
            {"reset": {"rst": False}},
            {
                "set": {
                    "payload_data": 9,
                    "request": 1,
                    "tx": {"return": 0},
                },
                "edges": ["clk"],
            },
            {
                "set": {
                    "payload_data": 10,
                    "request": 1,
                    "tx": {"return": 1},
                },
                "edges": ["clk"],
            },
        ),
        directory=tmp_path,
    )
    assert trace.reference == trace.native == trace.direct_sv


_HIERARCHICAL_CREDIT_SOURCE = """
module CreditProducer {
    clock clk reset rst
    in data:u8 in send:bit
    out tx:credit<u8,2>
    out sent:bit
    tx.payload=data
    tx.send=send
    sent=tx.transfer
}
module CreditConsumer {
    clock clk reset rst
    in release:bit
    in rx:credit<u8,2>
    out payload:u8
    out received:bit
    rx.return=release
    payload=rx.payload
    received=rx.transfer
}
module CreditHierarchy {
    clock clk reset rst
    in data:u8 in send:bit in release:bit
    out payload:u8 out sent:bit out received:bit
    inst producer:CreditProducer { data send }
    inst consumer:CreditConsumer { release }
    connect producer.tx -> consumer.rx
    payload=consumer.payload
    sent=producer.sent
    received=consumer.received
}
module NestedCreditHierarchy {
    clock clk reset rst
    in data:u8 in send:bit in release:bit
    out payload:u8 out sent:bit out received:bit
    inst inner:CreditHierarchy { data send release }
    payload=inner.payload
    sent=inner.sent
    received=inner.received
}
"""


@pytest.mark.parametrize("engine", ("reference", "native"))
@pytest.mark.parametrize("top", ("CreditHierarchy", "NestedCreditHierarchy"))
def test_hierarchical_credit_is_flattened_to_scalar_state(
    tmp_path: Path,
    engine: str,
    top: str,
) -> None:
    source = _source(
        tmp_path,
        f"hierarchical_credit_{top}_{engine}",
        _HIERARCHICAL_CREDIT_SOURCE,
    )
    program = zlang.sim.compile(source, top=top, engine=engine)
    encoded = json.dumps(program.plan.payload, sort_keys=True)
    assert program.plan.payload["canonical_ir_identity"].startswith(
        "hierarchical:"
    )
    assert '"instances"' not in encoded
    assert '"protocol"' not in encoded
    assert '"op": "credit' not in encoded
    assert len(program.plan.payload["registers"]) == 2

    with program.create() as instance:
        instance.reset("rst", asserted=True)
        instance.edge("clk")
        instance.reset("rst", asserted=False)
        results = instance.run_events(
            (
                {
                    "set": {"data": 7, "send": 1, "release": 0},
                    "edges": ["clk"],
                },
                {
                    "set": {"data": 8, "send": 1, "release": 0},
                    "edges": ["clk"],
                },
                {
                    "set": {"data": 9, "send": 1, "release": 0},
                    "edges": ["clk"],
                },
                {
                    "set": {"data": 10, "send": 1, "release": 1},
                    "edges": ["clk"],
                },
            )
        )
    assert results == [
        {"payload": 7, "received": 1, "sent": 1},
        {"payload": 8, "received": 0, "sent": 0},
        {"payload": 9, "received": 0, "sent": 0},
        {"payload": 10, "received": 1, "sent": 1},
    ]


_AGGREGATE_PROTOCOL_SOURCE = """
protocol TinyBus {
    role initiator role target
    channel req:rv<u8> initiator -> target
    channel rsp:rv<u8> target -> initiator
    member irq:bit target -> initiator
}
module AggregateProducer {
    clock clk reset rst
    in data:u8 in issue:bit in accept_response:bit
    interface bus:TinyBus.initiator
    out response_data:u8 out response_seen:bit out irq_seen:bit
    bus.req.payload=data
    bus.req.valid=issue
    bus.rsp.ready=accept_response
    response_data=bus.rsp.payload
    response_seen=bus.rsp.transfer
    irq_seen=bus.irq
}
module AggregateConsumer {
    clock clk reset rst
    in accept_request:bit in alarm:bit
    interface bus:TinyBus.target
    out request_seen:bit
    bus.req.ready=accept_request
    bus.rsp.payload=bus.req.payload
    bus.rsp.valid=bus.req.transfer
    bus.irq=alarm
    request_seen=bus.req.transfer
}
module AggregateTop {
    clock clk reset rst
    in data:u8 in issue:bit in accept_response:bit
    in accept_request:bit in alarm:bit
    out response_data:u8 out response_seen:bit
    out request_seen:bit out irq_seen:bit
    inst producer:AggregateProducer { data issue accept_response }
    inst consumer:AggregateConsumer { accept_request alarm }
    connect producer.bus -> consumer.bus
    response_data=producer.response_data
    response_seen=producer.response_seen
    request_seen=consumer.request_seen
    irq_seen=producer.irq_seen
}
module NestedAggregateTop {
    clock clk reset rst
    in data:u8 in issue:bit in accept_response:bit
    in accept_request:bit in alarm:bit
    out response_data:u8 out response_seen:bit
    out request_seen:bit out irq_seen:bit
    inst inner:AggregateTop {
        data issue accept_response accept_request alarm
    }
    response_data=inner.response_data
    response_seen=inner.response_seen
    request_seen=inner.request_seen
    irq_seen=inner.irq_seen
}
"""


@pytest.mark.parametrize("engine", ("reference", "native"))
@pytest.mark.parametrize("top", ("AggregateTop", "NestedAggregateTop"))
def test_aggregate_protocol_hierarchy_is_lowered_by_member(
    tmp_path: Path,
    engine: str,
    top: str,
) -> None:
    source = _source(
        tmp_path,
        f"aggregate_protocol_{top}_{engine}",
        _AGGREGATE_PROTOCOL_SOURCE,
    )
    program = zlang.sim.compile(source, top=top, engine=engine)
    encoded = json.dumps(program.plan.payload, sort_keys=True)
    assert '"aggregate_protocol"' not in encoded
    assert '"instances"' not in encoded
    assert '"protocol"' not in encoded
    with program.create() as instance:
        instance.reset("rst", asserted=True)
        instance.edge("clk")
        instance.reset("rst", asserted=False)
        instance.set("data", 19)
        instance.set("issue", 1)
        instance.set("accept_response", 1)
        instance.set("accept_request", 1)
        instance.set("alarm", 1)
        assert instance.eval() == {
            "response_data": 19,
            "response_seen": 1,
            "request_seen": 1,
            "irq_seen": 1,
        }


def test_aggregate_protocol_hierarchy_matches_direct_systemverilog(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "aggregate_protocol_differential",
        _AGGREGATE_PROTOCOL_SOURCE,
    )
    trace = run_differential(
        source,
        top="AggregateTop",
        events=(
            {"reset": {"rst": True}, "edges": ["clk"]},
            {"reset": {"rst": False}},
            {
                "set": {
                    "data": 19,
                    "issue": 1,
                    "accept_response": 1,
                    "accept_request": 1,
                    "alarm": 1,
                },
                "edges": ["clk"],
            },
        ),
        directory=tmp_path / "aggregate_protocol_differential",
    )
    assert trace.reference == trace.native == trace.direct_sv


_AGGREGATE_DELEGATION_SOURCE = """
struct AggregatePayload { addr:u8 data:u16 }
protocol DelegatedBus {
    role source role sink
    channel req:rv<AggregatePayload> source -> sink
    member irq:bit sink -> source
}
module AggregateSink {
    clock clk reset rst
    interface bus:DelegatedBus.sink
    bus.req.ready=1
    bus.irq=1
}
module AggregateBoundary {
    clock clk reset rst
    interface bus:DelegatedBus.sink
    inst child:AggregateSink
    connect bus -> child.bus
}
"""


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_top_aggregate_delegation_uses_expanded_public_fields(
    tmp_path: Path,
    engine: str,
) -> None:
    source = _source(
        tmp_path,
        f"aggregate_delegation_{engine}",
        _AGGREGATE_DELEGATION_SOURCE,
    )
    program = zlang.sim.compile(source, top="AggregateBoundary", engine=engine)
    encoded = json.dumps(program.plan.payload, sort_keys=True)
    assert '"aggregate_protocol"' not in encoded
    assert '"instances"' not in encoded
    with program.create() as instance:
        instance.set(
            "bus__req",
            {"payload": {"addr": 3, "data": 0x1234}, "valid": 1},
        )
        assert instance.eval() == {
            "bus__req": {"ready": 1, "transfer": 1},
            "bus__irq": 1,
        }


_AGGREGATE_SCALAR_CHILD_SOURCE = """
        protocol Bus {
            role source role sink
            channel req:rv<u8> source -> sink
        }
        module ScalarChild {
            clock clk reset rst
            in request:bit out accepted:bit
            accepted=request
        }
        module AggregateTop {
            clock clk reset rst
            interface bus:Bus.sink @clk
            child:ScalarChild
            child.request=bus.req.valid
            bus.req.ready=child.accepted
        }
"""


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_aggregate_top_with_scalar_child_needs_no_delegation(
    tmp_path: Path,
    engine: str,
) -> None:
    source = _source(
        tmp_path,
        f"aggregate_scalar_child_{engine}",
        _AGGREGATE_SCALAR_CHILD_SOURCE,
    )
    program = zlang.sim.compile(source, top="AggregateTop", engine=engine)
    assert not program.plan.payload.get("aggregate_protocol_endpoints")
    with program.create() as instance:
        instance.set("bus__req", {"payload": 7, "valid": 1})
        assert instance.eval() == {"bus__req": {"ready": 1, "transfer": 1}}


def test_aggregate_top_without_delegation_matches_direct_sv(tmp_path: Path) -> None:
    source = _source(
        tmp_path, "aggregate_scalar_child_sv", _AGGREGATE_SCALAR_CHILD_SOURCE
    )
    trace = run_differential(
        source,
        top="AggregateTop",
        events=(
            {"reset": {"rst": True}, "edges": ["clk"]},
            {"reset": {"rst": False}},
            {"set": {"bus__req": {"payload": 7, "valid": 1}},
             "edges": ["clk"]},
        ),
        directory=tmp_path / "aggregate_scalar_child_sv",
    )
    assert trace.reference == trace.native == trace.direct_sv


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_vc_credit_sender_is_compiler_lowered_per_channel(engine: str) -> None:
    path = ROOT / "examples/vc_credit_source.zhl"
    module = compile_source(path.read_text(encoding="utf-8")).ir
    cycles = (
        {"payload": 1, "channel": 0, "request": 1,
         "tx": {"return": 0, "return_vc": 0}},
        {"payload": 2, "channel": 0, "request": 1,
         "tx": {"return": 0, "return_vc": 0}},
        {"payload": 3, "channel": 0, "request": 1,
         "tx": {"return": 0, "return_vc": 0}},
        {"payload": 4, "channel": 1, "request": 1,
         "tx": {"return": 0, "return_vc": 0}},
        {"payload": 5, "channel": 0, "request": 1,
         "tx": {"return": 1, "return_vc": 0}},
        {"payload": 6, "channel": 0, "request": 1,
         "tx": {"return": 0, "return_vc": 0}},
    )
    expected = simulate_vc_credit_cycles(module, cycles)
    program = zlang.sim.compile(path, top="VcCreditSource", engine=engine)
    encoded = json.dumps(program.plan.payload, sort_keys=True)
    assert '"op": "vc_credit' not in encoded
    assert '"protocol"' not in encoded
    with program.create() as instance:
        actual = []
        for values in cycles:
            for name, value in values.items():
                instance.set(name, value)
            actual.append(instance.eval())
            instance.edge("clk")
    assert actual == expected


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_vc_credit_receiver_bounds_are_generic_runtime_checks(
    tmp_path: Path,
    engine: str,
) -> None:
    source = _source(
        tmp_path,
        "vc_credit_receiver",
        """
        module VcCreditReceiver {
            clock clk reset rst
            in release:bit in return_channel:u1
            in rx:vc_credit<u8,2,2>
            rx.return=release
            rx.return_vc=return_channel
        }
        """,
    )
    program = zlang.sim.compile(source, top="VcCreditReceiver", engine=engine)
    assert {
        event["metadata"]["category"] for event in program.plan.payload["events"]
    } == {"runtime_violation"}
    with program.create() as instance:
        instance.set("release", 1)
        instance.set("return_channel", 1)
        instance.set("rx", {"payload": 0, "vc": 0, "send": 0})
        with pytest.raises(ProtocolViolation, match="underflow on VC 1"):
            instance.edge("clk")

    with program.create() as instance:
        instance.set("release", 0)
        instance.set("return_channel", 0)
        for payload in (1, 2):
            instance.set("rx", {"payload": payload, "vc": 0, "send": 1})
            instance.edge("clk")
        instance.set("rx", {"payload": 3, "vc": 0, "send": 1})
        with pytest.raises(ProtocolViolation, match="overflow on VC 0"):
            instance.edge("clk")


def test_vc_credit_sender_matches_direct_systemverilog(tmp_path: Path) -> None:
    trace = run_differential(
        ROOT / "examples/vc_credit_source.zhl",
        top="VcCreditSource",
        events=(
            {"reset": {"rst": True}, "edges": ["clk"]},
            {"reset": {"rst": False}},
            {
                "set": {
                    "payload": 9,
                    "channel": 0,
                    "request": 1,
                    "tx": {"return": 0, "return_vc": 0},
                },
                "edges": ["clk"],
            },
            {
                "set": {
                    "payload": 10,
                    "channel": 1,
                    "request": 1,
                    "tx": {"return": 1, "return_vc": 0},
                },
                "edges": ["clk"],
            },
        ),
        directory=tmp_path,
    )
    assert trace.reference == trace.native == trace.direct_sv


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_vc_credit_run_events_preserves_vector_counts(engine: str) -> None:
    with zlang.sim.load(
        ROOT / "examples/vc_credit_source.zhl",
        top="VcCreditSource",
        engine=engine,
    ) as instance:
        results = instance.run_events((
            {
                "set": {
                    "payload": 7,
                    "channel": 1,
                    "request": 1,
                    "tx": {"return": 0, "return_vc": 0},
                },
            },
            {"edges": ["clk"]},
        ))
    assert results[0]["tx"] == {
        "payload": 7,
        "vc": 1,
        "send": 1,
        "transfer": 1,
        "credits": (2, 2),
    }
    assert results[1]["tx"]["credits"] == (2, 1)


def _packet(payload: int, *, valid: int = 1, last: int = 1):
    return {"payload": payload, "valid": valid, "last": last}


def _packet_cycle(
    a: dict[str, int],
    b: dict[str, int],
    *,
    ready: int = 1,
    c: dict[str, int] | None = None,
    d: dict[str, int] | None = None,
):
    return {
        "source_a": a,
        "source_b": b,
        "source_c": _packet(0, valid=0) if c is None else c,
        "source_d": _packet(0, valid=0) if d is None else d,
        "tx": {"ready": ready},
    }


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_packet_round_robin_is_compiler_lowered(engine: str) -> None:
    path = ROOT / "examples/packet_round_robin.zhl"
    module = compile_source(path.read_text(encoding="utf-8")).ir
    cycles = (
        _packet_cycle(_packet(10, last=0), _packet(20), ready=0),
        _packet_cycle(_packet(10, last=0), _packet(20)),
        _packet_cycle(_packet(11), _packet(20)),
        _packet_cycle(_packet(12), _packet(20)),
    )
    expected = simulate_packet_arbiter_cycles(module, cycles)
    program = zlang.sim.compile(path, top="PacketRoundRobin", engine=engine)
    encoded = json.dumps(program.plan.payload, sort_keys=True)
    assert '"arbiter"' not in encoded
    assert '"protocol"' not in encoded
    with program.create() as instance:
        actual = []
        for values in cycles:
            for name, value in values.items():
                instance.set(name, value)
            actual.append(instance.eval())
            instance.edge("clk")
    assert actual == expected


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_packet_round_robin_visits_all_sources(engine: str) -> None:
    values = _packet_cycle(
        _packet(30),
        _packet(31),
        c=_packet(32),
        d=_packet(33),
    )
    with zlang.sim.load(
        ROOT / "examples/packet_round_robin.zhl",
        top="PacketRoundRobin",
        engine=engine,
    ) as instance:
        grants = []
        for _ in range(4):
            for name, value in values.items():
                instance.set(name, value)
            grants.append(instance.eval()["tx"]["grant"])
            instance.edge("clk")
    assert grants == [0, 1, 2, 3]


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_packet_beat_grant_rotates_after_each_transfer(
    tmp_path: Path,
    engine: str,
) -> None:
    source = _source(
        tmp_path,
        "packet_beat",
        """
        module PacketBeat {
            clock clk reset rst
            in a:packet<u8> in b:packet<u8> out tx:packet<u8>
            arbiter [a,b] -> tx { policy round_robin grant beat }
        }
        """,
    )
    with zlang.sim.load(source, top="PacketBeat", engine=engine) as instance:
        instance.set("a", _packet(1, last=0))
        instance.set("b", _packet(2, last=0))
        instance.set("tx", {"ready": 1})
        grants = []
        for _ in range(4):
            grants.append(instance.eval()["tx"]["grant"])
            instance.edge("clk")
    assert grants == [0, 1, 0, 1]


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_packet_stall_violation_is_a_generic_precommit_check(engine: str) -> None:
    with zlang.sim.load(
        ROOT / "examples/packet_round_robin.zhl",
        top="PacketRoundRobin",
        engine=engine,
    ) as instance:
        values = _packet_cycle(_packet(1, last=0), _packet(2), ready=0)
        for name, value in values.items():
            instance.set(name, value)
        instance.edge("clk")
        changed = _packet_cycle(_packet(9), _packet(2), ready=0)
        for name, value in changed.items():
            instance.set(name, value)
        with pytest.raises(ProtocolViolation, match="changed while stalled"):
            instance.edge("clk")
        for name, value in values.items():
            instance.set(name, value)
        instance.edge("clk")
        assert instance.eval()["tx"]["grant"] == 0


@pytest.mark.parametrize(
    ("filename", "top", "events"),
    (
        (
            "packet_round_robin.zhl",
            "PacketRoundRobin",
            (
                {"reset": {"rst": True}, "edges": ["clk"]},
                {"reset": {"rst": False}},
                {
                    "set": _packet_cycle(
                        _packet(10, last=0),
                        _packet(20),
                        ready=0,
                    ),
                    "edges": ["clk"],
                },
                {
                    "set": _packet_cycle(
                        _packet(10, last=0),
                        _packet(20),
                    ),
                    "edges": ["clk"],
                },
            ),
        ),
        (
            "packet_fixed_arbiter.zhl",
            "PacketFixedArbiter",
            (
                {"reset": {"rst": True}, "edges": ["clk"]},
                {"reset": {"rst": False}},
                {
                    "set": {
                        "high_priority": _packet(3),
                        "low_priority": _packet(4),
                        "tx": {"ready": 1},
                    },
                    "edges": ["clk"],
                },
            ),
        ),
    ),
)
def test_packet_arbiters_match_direct_systemverilog(
    tmp_path: Path,
    filename: str,
    top: str,
    events: tuple[dict[str, object], ...],
) -> None:
    trace = run_differential(
        ROOT / "examples" / filename,
        top=top,
        events=events,
        directory=tmp_path / top,
    )
    assert trace.reference == trace.native == trace.direct_sv


_IN_ORDER_REQUEST_RESPONSE_SOURCE = """
struct Request { data:u8 }
struct Response { data:u8 }
module StandaloneRequester {
    clock clk reset rst
    in request_data:u8 in issue:bit in consume:bit out response_data:u8
    interface mem:request_response<Request,Response> {
        max_outstanding 2 ordering in_order
    }
    mem.request.payload=Request { data=request_data }
    mem.request.valid=issue
    mem.response.ready=consume
    response_data=mem.response.payload.data
}
module StandaloneResponder {
    clock clk reset rst
    in accept:bit in produce:bit in response_data:u8 out accepted:bit
    interface mem:request_response<Request,Response> {
        max_outstanding 2 ordering in_order
    }
    mem.request.ready=accept
    mem.response.payload=Response { data=response_data }
    mem.response.valid=produce
    accepted=mem.request.transfer
}
"""


@pytest.mark.parametrize("engine", ("reference", "native"))
@pytest.mark.parametrize(
    ("top", "cycles"),
    (
        (
            "StandaloneRequester",
            (
                {
                    "request_data": 1,
                    "issue": 1,
                    "consume": 1,
                    "mem": {
                        "request": {"ready": 1},
                        "response": {"payload": {"data": 90}, "valid": 0},
                    },
                },
                {
                    "request_data": 2,
                    "issue": 1,
                    "consume": 1,
                    "mem": {
                        "request": {"ready": 1},
                        "response": {"payload": {"data": 91}, "valid": 0},
                    },
                },
                {
                    "request_data": 3,
                    "issue": 1,
                    "consume": 1,
                    "mem": {
                        "request": {"ready": 1},
                        "response": {"payload": {"data": 92}, "valid": 0},
                    },
                },
                {
                    "request_data": 4,
                    "issue": 1,
                    "consume": 1,
                    "mem": {
                        "request": {"ready": 1},
                        "response": {"payload": {"data": 93}, "valid": 1},
                    },
                },
            ),
        ),
        (
            "StandaloneResponder",
            (
                {
                    "accept": 1,
                    "produce": 0,
                    "response_data": 10,
                    "mem": {
                        "request": {"payload": {"data": 1}, "valid": 1},
                        "response": {"ready": 1},
                    },
                },
                {
                    "accept": 1,
                    "produce": 1,
                    "response_data": 11,
                    "mem": {
                        "request": {"payload": {"data": 2}, "valid": 1},
                        "response": {"ready": 1},
                    },
                },
            ),
        ),
    ),
)
def test_in_order_request_response_is_compiler_lowered(
    tmp_path: Path,
    engine: str,
    top: str,
    cycles: tuple[dict[str, object], ...],
) -> None:
    source = _source(
        tmp_path,
        f"request_response_{top}_{engine}",
        _IN_ORDER_REQUEST_RESPONSE_SOURCE,
    )
    module = compile_source(_IN_ORDER_REQUEST_RESPONSE_SOURCE, top=top).ir
    expected = simulate_request_response_cycles(module, cycles)
    program = zlang.sim.compile(source, top=top, engine=engine)
    encoded = json.dumps(program.plan.payload, sort_keys=True)
    assert '"request_response"' not in encoded
    with program.create() as instance:
        actual = []
        for values in cycles:
            for name, value in values.items():
                instance.set(name, value)
            actual.append(instance.eval())
            instance.edge("clk")
    assert actual == expected


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_in_order_request_response_run_events_is_batched(
    tmp_path: Path,
    engine: str,
) -> None:
    source = _source(
        tmp_path,
        f"request_response_events_{engine}",
        _IN_ORDER_REQUEST_RESPONSE_SOURCE,
    )
    with zlang.sim.load(
        source,
        top="StandaloneRequester",
        engine=engine,
    ) as instance:
        result = instance.run_events(({
            "set": {
                "request_data": 1,
                "issue": 1,
                "consume": 1,
                "mem": {
                    "request": {"ready": 1},
                    "response": {"payload": {"data": 90}, "valid": 0},
                },
            },
            "edges": ["clk"],
        },))
    assert result[0]["mem"]["request"]["transfer"] == 1
    assert result[0]["mem"]["outstanding"] == 1


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_hierarchical_request_response_is_flattened_compiler_side(
    engine: str,
) -> None:
    program = zlang.sim.compile(
        ROOT / "examples/hierarchical_request_response.zhl",
        top="HierarchicalRequestResponse",
        engine=engine,
    )
    encoded = json.dumps(program.plan.payload, sort_keys=True)
    assert program.plan.payload["canonical_ir_identity"].startswith(
        "hierarchical:"
    )
    assert '"instances"' not in encoded
    assert '"connections"' not in encoded
    assert '"request_response"' not in encoded
    assert len(program.plan.payload["registers"]) == 2

    with program.create() as instance:
        instance.reset("rst", asserted=True)
        instance.edge("clk")
        instance.reset("rst", asserted=False)
        assert instance.run_events(
            (
                {
                    "set": {
                        "fire": 1,
                        "accept_request": 1,
                        "accept_response": 1,
                        "data": 42,
                    },
                    "edges": ["clk"],
                },
                {
                    "set": {"fire": 0, "data": 99},
                    "edges": ["clk"],
                },
            )
        ) == [{"response_seen": 1}, {"response_seen": 0}]


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_hierarchical_request_response_directional_buffers_use_storage(
    tmp_path: Path,
    engine: str,
) -> None:
    source = _source(
        tmp_path,
        f"hierarchical_request_response_buffered_{engine}",
        (ROOT / "examples/hierarchical_request_response.zhl")
        .read_text(encoding="utf-8")
        .replace(
            "    in accept_response: bit\n\n    bus.request.payload",
            "    in accept_response: bit\n"
            "    out request_transfer: bit\n"
            "    out response_transfer: bit\n\n"
            "    bus.request.payload",
        )
        .replace(
            "    bus.response.ready = accept_response\n}",
            "    bus.response.ready = accept_response\n"
            "    request_transfer = bus.request.transfer\n"
            "    response_transfer = bus.response.transfer\n}",
        )
        .replace(
            "    out response_seen: bit\n\n    bus.request.ready",
            "    out response_seen: bit\n"
            "    out request_transfer: bit\n\n"
            "    bus.request.ready",
        )
        .replace(
            "    response_seen = bus.response.valid\n}",
            "    response_seen = bus.response.valid\n"
            "    request_transfer = bus.request.transfer\n}",
        )
        .replace(
            "    out response_seen: bit\n\n    inst requester",
            "    out response_seen: bit\n"
            "    out requester_response_transfer: bit\n"
            "    out responder_request_transfer: bit\n\n"
            "    inst requester",
        )
        .replace(
            "    response_seen = responder.response_seen\n}",
            "    response_seen = responder.response_seen\n"
            "    requester_response_transfer = requester.response_transfer\n"
            "    responder_request_transfer = responder.request_transfer\n}",
        )
        .replace(
            "connect requester.bus -> responder.bus",
            "connect requester.bus -> responder.bus { "
            "request_buffer 2 response_buffer 1 }",
        ),
    )
    program = zlang.sim.compile(
        source,
        top="HierarchicalRequestResponse",
        engine=engine,
    )
    encoded = json.dumps(program.plan.payload, sort_keys=True)
    assert '"request_response"' not in encoded
    assert len(program.plan.payload["memories"]) == 2
    assert len(program.plan.payload["registers"]) == 8

    with program.create() as instance:
        instance.reset("rst", asserted=True)
        instance.edge("clk")
        instance.reset("rst", asserted=False)
        results = instance.run_events(
            (
                {
                    "set": {
                        "fire": 1,
                        "accept_request": 1,
                        "accept_response": 1,
                        "data": 17,
                    },
                    "edges": ["clk"],
                },
                {
                    "set": {"fire": 0},
                    "edges": ["clk"],
                },
                {"edges": ["clk"]},
            )
        )
    assert results == [
        {
            "requester_response_transfer": 0,
            "responder_request_transfer": 1,
            "response_seen": 1,
        },
        {
            "requester_response_transfer": 1,
            "responder_request_transfer": 0,
            "response_seen": 0,
        },
        {
            "requester_response_transfer": 0,
            "responder_request_transfer": 0,
            "response_seen": 0,
        },
    ]


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_nested_request_response_hierarchy_is_lowered_transitively(
    tmp_path: Path,
    engine: str,
) -> None:
    source = _source(
        tmp_path,
        f"nested_hierarchical_request_response_{engine}",
        (ROOT / "examples/hierarchical_request_response.zhl").read_text(
            encoding="utf-8"
        )
        + """
        module OuterRequestResponse {
            clock clk
            reset rst
            in fire:bit
            in accept_request:bit
            in accept_response:bit
            in data:u8
            out response_seen:bit

            inst inner:HierarchicalRequestResponse {
                fire accept_request accept_response data
            }
            response_seen=inner.response_seen
        }
        """,
    )
    program = zlang.sim.compile(
        source,
        top="OuterRequestResponse",
        engine=engine,
    )
    encoded = json.dumps(program.plan.payload, sort_keys=True)
    assert '"instances"' not in encoded
    assert '"request_response"' not in encoded
    with program.create() as instance:
        instance.reset("rst", asserted=True)
        instance.edge("clk")
        instance.reset("rst", asserted=False)
        instance.set("fire", 1)
        instance.set("accept_request", 1)
        instance.set("accept_response", 1)
        instance.set("data", 63)
        assert instance.edge("clk") == {"response_seen": 1}


def _ooo_cycle(
    request_id: int,
    *,
    issue: int = 1,
    response_id: int = 0,
    response_valid: int = 0,
) -> dict[str, object]:
    return {
        "request_payload": {"id": request_id, "data": request_id + 10},
        "issue": issue,
        "accept_response": 1,
        "mem": {
            "request": {"ready": 1},
            "response": {
                "payload": {"id": response_id, "data": response_id + 20},
                "valid": response_valid,
            },
        },
    }


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_out_of_order_request_response_uses_primitive_id_slots(engine: str) -> None:
    path = ROOT / "examples/request_client.zhl"
    module = compile_source(path.read_text(encoding="utf-8")).ir
    cycles = (
        _ooo_cycle(1),
        _ooo_cycle(2),
        _ooo_cycle(0, issue=0, response_id=2, response_valid=1),
        _ooo_cycle(0, issue=0, response_id=1, response_valid=1),
    )
    expected = simulate_request_response_cycles(module, cycles)
    program = zlang.sim.compile(path, top="RequestClient", engine=engine)
    encoded = json.dumps(program.plan.payload, sort_keys=True)
    assert '"request_response"' not in encoded
    assert '"ordering"' not in encoded
    with program.create() as instance:
        actual = []
        for values in cycles:
            for name, value in values.items():
                instance.set(name, value)
            actual.append(instance.eval())
            instance.edge("clk")
    assert actual == expected


@pytest.mark.parametrize("engine", ("reference", "native"))
@pytest.mark.parametrize(
    ("second", "message"),
    (
        (_ooo_cycle(1), "duplicate outstanding ID"),
        (
            _ooo_cycle(0, issue=0, response_id=2, response_valid=1),
            "non-outstanding ID",
        ),
    ),
)
def test_out_of_order_id_violations_fail_before_commit(
    engine: str,
    second: dict[str, object],
    message: str,
) -> None:
    with zlang.sim.load(
        ROOT / "examples/request_client.zhl",
        top="RequestClient",
        engine=engine,
    ) as instance:
        for name, value in _ooo_cycle(1).items():
            instance.set(name, value)
        instance.edge("clk")
        for name, value in second.items():
            instance.set(name, value)
        with pytest.raises(ProtocolViolation, match=message):
            instance.edge("clk")
        assert instance.eval()["mem"]["outstanding"] == 1


@pytest.mark.parametrize("engine", ("reference", "native"))
@pytest.mark.parametrize(
    ("filename", "top", "cycles"),
    (
        (
            "rv_to_credit.zhl",
            "RvToCredit",
            (
                {"rx": {"payload": 1, "valid": 1}, "tx": {"return": 0}},
                {"rx": {"payload": 2, "valid": 1}, "tx": {"return": 0}},
                {"rx": {"payload": 3, "valid": 1}, "tx": {"return": 0}},
                {"rx": {"payload": 3, "valid": 1}, "tx": {"return": 1}},
                {"rx": {"payload": 3, "valid": 1}, "tx": {"return": 0}},
            ),
        ),
        (
            "credit_to_rv.zhl",
            "CreditToRv",
            (
                {"rx": {"payload": 9, "send": 1}, "tx": {"ready": 0}},
                {"rx": {"payload": 10, "send": 1}, "tx": {"ready": 0}},
                {"rx": {"payload": 0, "send": 0}, "tx": {"ready": 1}},
                {"rx": {"payload": 0, "send": 0}, "tx": {"ready": 1}},
            ),
        ),
    ),
)
def test_protocol_adapters_are_compiler_lowered(
    engine: str,
    filename: str,
    top: str,
    cycles: tuple[dict[str, object], ...],
) -> None:
    path = ROOT / "examples" / filename
    module = compile_source(path.read_text(encoding="utf-8")).ir
    expected = simulate_connection_cycles(module, cycles)
    program = zlang.sim.compile(path, top=top, engine=engine)
    encoded = json.dumps(program.plan.payload, sort_keys=True)
    assert '"adapter"' not in encoded
    assert '"protocol"' not in encoded
    with program.create() as instance:
        actual = []
        for values in cycles:
            for name, value in values.items():
                instance.set(name, value)
            actual.append(instance.eval())
            instance.edge("clk")
    assert actual == expected


@pytest.mark.parametrize(
    ("filename", "top", "events"),
    (
        (
            "rv_to_credit.zhl",
            "RvToCredit",
            (
                {"reset": {"rst": True}, "edges": ["clk"]},
                {"reset": {"rst": False}},
                {
                    "set": {
                        "rx": {"payload": 5, "valid": 1},
                        "tx": {"return": 0},
                    },
                    "edges": ["clk"],
                },
            ),
        ),
        (
            "credit_to_rv.zhl",
            "CreditToRv",
            (
                {"reset": {"rst": True}, "edges": ["clk"]},
                {"reset": {"rst": False}},
                {
                    "set": {
                        "rx": {"payload": 7, "send": 1},
                        "tx": {"ready": 0},
                    },
                    "edges": ["clk"],
                },
                {"set": {"tx": {"ready": 1}}, "edges": ["clk"]},
            ),
        ),
    ),
)
def test_protocol_adapters_match_direct_systemverilog(
    tmp_path: Path,
    filename: str,
    top: str,
    events: tuple[dict[str, object], ...],
) -> None:
    trace = run_differential(
        ROOT / "examples" / filename,
        top=top,
        events=events,
        directory=tmp_path,
    )
    assert trace.reference == trace.native == trace.direct_sv


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_credit_to_ready_valid_overflow_fails_before_commit(engine: str) -> None:
    with zlang.sim.load(
        ROOT / "examples/credit_to_rv.zhl",
        top="CreditToRv",
        engine=engine,
    ) as instance:
        instance.set("tx", {"ready": 0})
        for payload in (1, 2):
            instance.set("rx", {"payload": payload, "send": 1})
            instance.edge("clk")
        instance.set("rx", {"payload": 3, "send": 1})
        with pytest.raises(ProtocolViolation, match="without credit"):
            instance.edge("clk")


def test_protocol_field_shapes_are_strict() -> None:
    with zlang.sim.load(
        ROOT / "examples/rv_passthrough.zhl",
        top="RvPassthrough",
        engine="reference",
    ) as instance:
        with pytest.raises(
            zlang.sim.SimulationRuntimeError,
            match="requires fields: payload, valid",
        ):
            instance.set("rx", {"payload": 1})
        with pytest.raises(
            zlang.sim.SimulationRuntimeError,
            match="set_packed requires a scalar wire port",
        ):
            instance.set_packed("rx", 1)


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_protocol_trace_selection_is_lowered_to_scalar_names(
    tmp_path: Path,
    engine: str,
) -> None:
    source = _source(
        tmp_path,
        "rv_trace",
        """
        module RvTrace {
            clock clk reset rst
            in rx:rv<u8>
            out tx:rv<u8>
            reg seen:u8=0
            rx.ready=tx.ready
            tx.payload=seen
            tx.valid=rx.valid
            seen <- rx.payload
        }
        """,
    )
    with zlang.sim.load(
        source,
        top="RvTrace",
        engine=engine,
    ) as instance:
        instance.enable_trace(("rx", "tx"))
        instance.set("rx", {"payload": 3, "valid": 1})
        instance.set("tx", {"ready": 1})
        instance.edge("clk")
        trace = instance.drain_trace()
        assert trace
        assert set(trace[-1]) == {
            "$event",
            "rx.payload",
            "rx.valid",
            "rx.ready",
            "tx.payload",
            "tx.valid",
            "tx.ready",
        }
