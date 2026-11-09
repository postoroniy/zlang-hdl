# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Read-only child inputs use the actual driver and preserve admission checks."""

import pytest

from tests.support.hierarchy_observation_cases import delayed_observation_source, observation_source
from zlang.compilation_session import CompilationSession
from zlang.compiler import compile_source
from zlang.ir import expressions as ir_expr
from zlang.ir.traversal import walk_expression
from zlang.semantic import SemanticError


@pytest.mark.parametrize("manual", (False, True))
@pytest.mark.parametrize("alias", (False, True))
def test_payload_valid_ready_and_transfer_accept_direct_and_local_observations(manual, alias):
    result = compile_source(observation_source(manual=manual, alias=alias))
    assert {a.target.name for a in result.ir.assignments} >= {
        "observed", "observed_valid", "observed_ready", "fired",
    }


def test_transfer_in_a_rule_guard_is_available_before_transition_analysis():
    source = observation_source(manual=True).replace(
        "out observed:u8", "out reg count:u8=0\n out observed:u8",
    ).replace("local_fired=", "when peer.bus.response.transfer { count <- truncate<8>(count+1) }\n local_fired=")
    assert compile_source(source).ir.registers


def test_manual_driver_declarations_can_follow_the_observing_aliases():
    source = observation_source(manual=True)
    bindings = ("peer.bus.request.valid=enable", "peer.bus.request.payload=data",
                "peer.bus.response.ready=consume")
    for binding in bindings:
        source = source.replace(binding, "")
    position = source.rfind("}")
    source = source[:position] + "\n" + "\n".join(bindings) + "\n" + source[position:]
    assert compile_source(source).ir


def test_manual_driver_can_use_an_earlier_immutable_parent_expression():
    source = observation_source(manual=True).replace(
        "peer.bus.response.ready=consume", "allowed=consume\n peer.bus.response.ready=allowed",
    )
    assert compile_source(source).ir


def test_observers_share_the_original_delayed_driver_and_advance_its_allocator():
    module = CompilationSession(delayed_observation_source()).semantic_ir
    binding = next(b for b in module.instance_bindings if isinstance(b.expression, ir_expr.Delay))
    observed = next(local for local in module.locals if local.name == "local_observed_ready")
    assert observed.expression == binding.expression
    other = next(a for a in module.assignments if a.target.name == "other")
    assert other.expression.instance != binding.expression.instance
    roots = (*[b.expression for b in module.instance_bindings],
             *[a.expression for a in module.assignments], *[v.expression for v in module.locals])
    assert len({value.instance for root in roots for value in walk_expression(root)
                if isinstance(value, ir_expr.Delay)}) == 2


def test_child_output_stays_child_owned():
    source = observation_source(manual=True).replace(
        "peer.bus.response.ready=consume", "peer.bus.response.ready=consume\n peer.bus.response.valid=enable",
    )
    with pytest.raises(SemanticError, match="child-owned"):
        compile_source(source)


def test_duplicate_manual_driver_is_not_hidden_by_observation():
    source = observation_source(manual=True).replace(
        "peer.bus.response.ready=consume", "peer.bus.response.ready=consume\n peer.bus.response.ready=1",
    )
    with pytest.raises(SemanticError, match="multiple drivers"):
        compile_source(source)


def test_direct_connection_plus_explicit_input_driver_is_rejected():
    source = observation_source().replace(
        "requester.bus -> peer.bus", "requester.bus -> peer.bus\n peer.bus.request.valid=enable",
    )
    with pytest.raises(SemanticError, match="multiple drivers"):
        compile_source(source)


def test_cyclic_manual_input_observation_is_rejected():
    source = observation_source(manual=True).replace(
        "peer.bus.response.ready=consume", "peer.bus.response.ready=peer.bus.response.ready",
    )
    with pytest.raises(SemanticError, match="cyclic child input observation"):
        compile_source(source)


def test_unbound_manual_ready_is_not_guessed():
    source = observation_source(manual=True).replace("peer.bus.response.ready=consume", "")
    with pytest.raises(SemanticError, match="no observable direct driver"):
        compile_source(source)


def test_unknown_endpoint_field_has_an_error_instead_of_a_default_value():
    source = observation_source(manual=True).replace("peer.bus.response.transfer", "peer.bus.response.missing")
    with pytest.raises(SemanticError):
        compile_source(source)


def test_plain_read_only_projection_does_not_add_storage_or_change_child_specialization():
    aliased = compile_source(observation_source(alias=True))
    direct = compile_source(observation_source(alias=False))
    assert [c.specialization_identity for c in aliased.ir.elaborated_instances] == [
        c.specialization_identity for c in direct.ir.elaborated_instances
    ]
    assert not aliased.ir.registers and not aliased.ir.fifos and not aliased.ir.memories


def test_memory_and_global_controller_state_can_coexist():
    source = """module Deferred {clock clk reset rst in a:u2 out y:u8
      reg count:u8=0 count <- truncate<8>(count+1)
      memory mem:mem<u8,4>{read_latency 1 collision read_first
        reset {contents preserve read_data clear}}
      mem.read_address=a mem.write_enable=0 mem.write_address=0
      mem.write_data=0 mem.write_mask=1 y=mem.read_data
    }"""
    module = compile_source(source).ir
    assert len(module.memories) == len(module.registers) == 1
    assert module.resolved_transition is not None


BUFFERED = """
module Producer {clock clk reset rst in d:u8 out tx:rv<u8>
  tx.payload=d tx.valid=1}
module Consumer {clock clk reset rst in rx:rv<u8> rx.ready=1}
module Top {clock clk reset rst in d:u8 out observed:u8
  producer:Producer {d=d} consumer:Consumer
  connect producer.tx -> consumer.rx {buffer 2}
  observed=consumer.rx.payload
}
"""


def test_buffered_consumer_observation_does_not_read_the_prebuffer_producer_value():
    with pytest.raises(SemanticError, match="buffered/CDC input observations"):
        compile_source(BUFFERED)


def test_buffered_producer_output_is_still_observable():
    assert compile_source(BUFFERED.replace("observed=consumer.rx.payload", "observed=producer.tx.payload")).ir


def test_buffered_connection_and_manual_input_driver_stay_conflicting():
    source = BUFFERED.replace("observed=consumer.rx.payload", "consumer.rx.payload=d\n observed=d")
    with pytest.raises(SemanticError, match="multiple drivers"):
        compile_source(source)
