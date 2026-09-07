"""Backend route closure for nested ``when`` action activations.

These probes intentionally exercise the public emitter dispatcher. Every
otherwise-supported action/composition route must lower per-effect activation
through its authoritative scheduler. Only a pre-existing unsupported base
route may fail closed before publishing an artifact; nested ``when`` itself
must not create a narrower backend subset.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from zlang.backend.clash import emit as emit_clash
from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.naming import module_rtl_names
from zlang.backend.systemverilog import emit_experimental as emit_systemverilog
from zlang.compiler import compile_source
from zlang.parser import parse
from zlang.semantic import analyze
from zlang.toolchain import lint_with_verilator
from zlang.toolchain import find_clash_executable, generate_verilog


VERILATOR = shutil.which("verilator")
CLASH = find_clash_executable()


SCALAR_DELAY_SOURCE = """
module DelayedNestedChild {
  clock clk reset rst
  in step, choose:bit
  in data:u8
  out value:u8
  reg state:u8=0
  update: when step {
    when delay<1>(choose) { state <- data }
  }
  value=state
}

module DelayedNestedTop {
  clock clk reset rst
  in step, choose:bit
  in data:u8
  out value:u8
  inst child:DelayedNestedChild { step choose data }
  value=child.value
}
"""


MIXED_READY_VALID_SOURCE = """
module ConditionalMixed {
  clock clk reset rst
  in input:rv<u8>
  in choose:bit
  out output:rv<u8>
  out event:bit
  reg state:u8=0

  input.ready=output.ready
  output.payload=state
  output.valid=input.valid
  update: when input.transfer {
    when choose {
      state <- input.payload
      event <- 1
    }
  }
}
"""


ORDINARY_HIERARCHY_SOURCE = """
module ConditionalRvLeaf {
  in input:rv<u8>
  out output:rv<u8>
  input.ready=output.ready
  output.payload=input.payload
  output.valid=input.valid
}

module ConditionalRvTop {
  clock clk reset rst
  in input:rv<u8>
  in choose:bit
  out output:rv<u8>
  out observed:u8
  out event:bit
  reg seen:u8=0
  inst leaf:ConditionalRvLeaf
  connect input -> leaf.input
  connect leaf.output -> output
  track: when input.transfer {
    when choose {
      seen <- input.payload
      event <- 1
    }
  }
  observed=seen
}
"""


AGGREGATE_HIERARCHY_SOURCE = """
protocol ConditionalBus {
  role source
  role sink
  channel data:rv<u8> source -> sink
  member alert:bit sink -> source
}

module ConditionalAggregateLeaf {
  clock clk reset rst
  interface bus:ConditionalBus.sink
  bus.data.ready=1
  bus.alert=0
}

module ConditionalAggregateTop {
  clock clk reset rst
  interface bus:ConditionalBus.sink
  in gate, choose:bit
  out observed:u8
  out event:bit
  reg state:u8=0
  inst leaf:ConditionalAggregateLeaf
  connect bus -> leaf.bus
  update: when gate {
    when choose {
      state <- 9
      event <- 1
    }
  }
  observed=state
}
"""


STORAGE_SOURCE = """
module ConditionalStorage {
  clock clk reset rst
  in go, choose:bit
  in address:u2
  in data:u8
  out count:u2
  out front, memory_data, state_value:u8
  out event:bit
  fifo queue:fifo<u8,2>
  memory table:mem<u8,4> {
    read_latency 1
    collision write_first
  }
  reg state:u8=0

  operate: when go {
    when choose {
      queue.push(data)
      table.write(address,data)
      state <- data
      event <- 1
    } else {
      queue.pop()
      table.read(address)
      state <- truncate<8>(state + 1)
      event <- 0
    }
  }
  count=queue.count
  front=queue.front
  memory_data=table.read_data
  state_value=state
}
"""


ORDINARY_HIERARCHY_STORAGE_SOURCE = """
module ConditionalStorageRvLeaf {
  in input:rv<u8>
  out output:rv<u8>
  input.ready=output.ready
  output.payload=input.payload
  output.valid=input.valid
}

module ConditionalStorageRvTop {
  clock clk reset rst
  in input:rv<u8>
  in go, choose:bit
  in address:u2
  out output:rv<u8>
  out count:u2
  out front, memory_data, rom_data, observed:u8
  out event:bit
  fifo queue:fifo<u8,2>
  memory table:mem<u8,4> {
    read_latency 1
    collision write_first
  }
  rom constants:rom<u8,4> {
    read_latency 1
    init generate(i in 0..4) truncate<8>(extend<8>(i) + 10)
  }
  reg state:u8=0
  inst leaf:ConditionalStorageRvLeaf
  connect input -> leaf.input
  connect leaf.output -> output
  constants.read_address=address
  operate: when go {
    when choose {
      queue.push(input.payload)
      table.write(address,input.payload)
      state <- input.payload
      event <- 1
    } else {
      queue.pop()
      table.read(address)
      state <- truncate<8>(state + 1)
      event <- 0
    }
  }
  count=queue.count
  front=queue.front
  memory_data=table.read_data
  rom_data=constants.read_data
  observed=state
}
"""


AGGREGATE_HIERARCHY_STORAGE_SOURCE = """
protocol ConditionalStorageBus {
  role source
  role sink
  channel data:rv<u8> source -> sink
  member alert:bit sink -> source
}

module ConditionalStorageAggregateLeaf {
  clock clk reset rst
  interface bus:ConditionalStorageBus.sink
  bus.data.ready=1
  bus.alert=0
}

module ConditionalStorageAggregateTop {
  clock clk reset rst
  interface bus:ConditionalStorageBus.sink
  in go, choose:bit
  in address:u2
  out count:u2
  out front, memory_data, rom_data, observed:u8
  out event:bit
  fifo queue:fifo<u8,2>
  memory table:mem<u8,4> {
    read_latency 1
    collision write_first
  }
  rom constants:rom<u8,4> {
    read_latency 1
    init generate(i in 0..4) truncate<8>(extend<8>(i) + 20)
  }
  reg state:u8=0
  inst leaf:ConditionalStorageAggregateLeaf
  connect bus -> leaf.bus
  constants.read_address=address
  operate: when go {
    when choose {
      queue.push(bus.data.payload)
      table.write(address,bus.data.payload)
      state <- bus.data.payload
      event <- 1
    } else {
      queue.pop()
      table.read(address)
      state <- truncate<8>(state + 1)
      event <- 0
    }
  }
  count=queue.count
  front=queue.front
  memory_data=table.read_data
  rom_data=constants.read_data
  observed=state
}
"""


CSR_SOURCE = """
module ConditionalCsrBank {
  clock clk reset rst
  in tick, choose:bit
  out count:u8
  reg counter:u8=0
  update: when tick {
    when choose { counter <- truncate<8>(counter + 1) }
  }
  count=counter
  csr control @0 {
    CONTROL @0 {
      enable bit @0 rw = 0
      reserved bits<31> @31:1 reserved
    }
  }
}

module ConditionalCsrTop {
  clock clk reset rst
  in tick, choose:bit
  in addr, wdata:u32
  in read, write:bit
  out count:u8
  out rdata:u32
  out ready:bit
  inst bank:ConditionalCsrBank { tick choose addr wdata read write }
  count=bank.count
  rdata=bank.rdata
  ready=bank.ready
}
"""


REQUEST_RESPONSE_SOURCE = """
struct ConditionalRequest { addr:u8 }
struct ConditionalResponse { data:u8 }

module ConditionalRequester {
  clock clk reset rst
  interface bus:request_response<ConditionalRequest,ConditionalResponse> {
    max_outstanding 1
    ordering in_order
  }
  in fire, accept_response, choose:bit
  out marker, event:bit
  reg state:bit=0
  bus.request.payload=ConditionalRequest { addr=7 }
  bus.request.valid=fire
  bus.response.ready=accept_response
  mark: when bus.request.transfer {
    when choose {
      state <- 1
      event <- 1
    }
  }
  marker=state
}

module ConditionalResponder {
  clock clk reset rst
  interface bus:request_response<ConditionalRequest,ConditionalResponse> {
    max_outstanding 1
    ordering in_order
  }
  in accept_request, choose:bit
  out handled:bit
  reg state:bit=0
  bus.request.ready=accept_request
  bus.response.payload=ConditionalResponse { data=11 }
  bus.response.valid=bus.request.transfer
  handle: when bus.request.transfer {
    when choose { state <- 1 }
  }
  handled=state
}

module ConditionalRequestResponseTop {
  clock clk reset rst
  in fire, accept_response, choose, accept_request:bit
  out marker, event, handled:bit
  inst requester:ConditionalRequester { fire accept_response choose }
  inst responder:ConditionalResponder { accept_request choose }
  connect requester.bus -> responder.bus
  marker=requester.marker
  event=requester.event
  handled=responder.handled
}
"""


OUT_OF_ORDER_REQUEST_RESPONSE_SOURCE = """
struct ConditionalTaggedRequest { id:u2 data:u8 }
struct ConditionalTaggedResponse { id:u2 data:u8 }

module ConditionalTaggedRequester {
  clock clk reset rst
  interface bus:request_response<ConditionalTaggedRequest,ConditionalTaggedResponse> {
    max_outstanding 2
    ordering out_of_order
    match_by id
  }
  in fire, accept_response, choose:bit
  out observed, event:bit
  reg state:bit=0
  bus.request.payload=ConditionalTaggedRequest { id=1 data=7 }
  bus.request.valid=fire
  bus.response.ready=accept_response
  update: when bus.request.transfer {
    when choose {
      state <- 1
      event <- 1
    }
  }
  observed=state
}
"""


BUFFERED_CONNECTION_SOURCE = """
module ConditionalBufferedConnection {
  clock clk reset rst
  in input:rv<u8>
  in go, choose:bit
  out output:rv<u8>
  out event:bit
  connect input -> output { buffer 1 }
  armed=go & choose
  event_value=choose
  update: when go { when armed { event <- event_value } }
}
"""


UNNESTED_MIXED_OUTPUT_SOURCE = """
module UnnestedMixedOutput {
  clock clk reset rst
  in input:rv<u8>
  in go:bit
  out output:rv<u8>
  out fired:bit
  reg state:u8=0

  input.ready=output.ready
  output.payload=state
  output.valid=input.valid
  update: when go {
    state <- input.payload
    fired <- 1
  }
}
"""


UNNESTED_HIERARCHY_SOURCE = """
module UnnestedHierarchyLeaf {
  in input:rv<u8>
  out output:rv<u8>
  input.ready=output.ready
  output.payload=input.payload
  output.valid=input.valid
}

module UnnestedHierarchyTop {
  clock clk reset rst
  in input:rv<u8>
  out output:rv<u8>
  out observed:u8
  reg seen:u8=0
  inst leaf:UnnestedHierarchyLeaf
  connect input -> leaf.input
  connect leaf.output -> output
  track: when input.transfer { seen <- input.payload }
  observed=seen
}
"""


BUFFERED_CONNECTION_HARNESS = r"""
#include "VConditionalBufferedConnection.h"
#include "verilated.h"

static void settle(VConditionalBufferedConnection& d) {
  d.clk = 0;
  d.eval();
}

static void clock_step(VConditionalBufferedConnection& d) {
  d.clk = 0; d.eval();
  d.clk = 1; d.eval();
  d.clk = 0; d.eval();
}

int main(int argc, char** argv) {
  Verilated::commandArgs(argc, argv);
  VConditionalBufferedConnection d;
  d.input_payload = 42;
  d.input_valid = 1;
  d.output_ready = 0;
  d.go = 1;
  d.choose = 1;
  d.rst = 1;
  settle(d);
  if (d.zlang_event || d.output_valid) return 1;
  clock_step(d);

  d.rst = 0;
  settle(d);
  if (!d.zlang_event || !d.input_ready) return 2;
  clock_step(d);
  d.input_valid = 0;
  d.go = 0;
  settle(d);
  if (d.zlang_event || !d.output_valid || d.output_payload != 42) return 3;

  d.choose = 0;
  d.go = 1;
  settle(d);
  if (d.zlang_event) return 4;
  d.rst = 1;
  settle(d);
  if (d.zlang_event) return 5;
  clock_step(d);
  settle(d);
  return d.output_valid ? 6 : 0;
}
"""


CSR_HIERARCHY_HARNESS = r"""
#include "VConditionalCsrTop.h"
#include "verilated.h"

static void settle(VConditionalCsrTop& d) {
  d.clk = 0;
  d.eval();
}

static void clock_step(VConditionalCsrTop& d) {
  d.clk = 0; d.eval();
  d.clk = 1; d.eval();
  d.clk = 0; d.eval();
}

int main(int argc, char** argv) {
  Verilated::commandArgs(argc, argv);
  VConditionalCsrTop d;
  d.tick = 1;
  d.choose = 0;
  d.addr = 0;
  d.wdata = 0;
  d.read = 0;
  d.write = 0;
  d.rst = 1;
  clock_step(d);
  if (d.count != 0) return 1;

  d.rst = 0;
  clock_step(d);
  if (d.count != 0) return 2;
  d.choose = 1;
  settle(d);
  if (d.count != 0) return 3;
  clock_step(d);
  if (d.count != 1) return 4;

  d.read = 1;
  settle(d);
  if (!d.ready || d.rdata != 0) return 5;
  d.rst = 1;
  settle(d);
  clock_step(d);
  return d.count == 0 ? 0 : 6;
}
"""


REQUEST_RESPONSE_HIERARCHY_HARNESS = r"""
#include "VConditionalRequestResponseTop.h"
#include "verilated.h"

static void settle(VConditionalRequestResponseTop& d) {
  d.clk = 0;
  d.eval();
}

static void clock_step(VConditionalRequestResponseTop& d) {
  d.clk = 0; d.eval();
  d.clk = 1; d.eval();
  d.clk = 0; d.eval();
}

int main(int argc, char** argv) {
  Verilated::commandArgs(argc, argv);
  VConditionalRequestResponseTop d;
  d.fire = 1;
  d.accept_response = 1;
  d.accept_request = 1;
  d.choose = 0;
  d.rst = 1;
  settle(d);
  if (d.marker || d.zlang_event || d.handled) return 1;
  clock_step(d);

  d.rst = 0;
  settle(d);
  if (d.zlang_event) return 2;
  clock_step(d);
  if (d.marker || d.handled) return 3;

  d.choose = 1;
  settle(d);
  if (!d.zlang_event || d.marker || d.handled) return 4;
  clock_step(d);
  if (!d.marker || !d.handled) return 5;

  d.fire = 0;
  settle(d);
  if (d.zlang_event) return 6;
  d.rst = 1;
  settle(d);
  if (d.zlang_event) return 7;
  clock_step(d);
  return (!d.marker && !d.handled) ? 0 : 8;
}
"""


HIERARCHY_STORAGE_HARNESS = r"""
#include "VConditionalStorageRvTop.h"
#include "verilated.h"

static void settle(VConditionalStorageRvTop& d) {
  d.clk = 0;
  d.eval();
}

static void clock_step(VConditionalStorageRvTop& d) {
  d.clk = 0; d.eval();
  d.clk = 1; d.eval();
  d.clk = 0; d.eval();
}

int main(int argc, char** argv) {
  Verilated::commandArgs(argc, argv);
  VConditionalStorageRvTop d;
  d.input_payload = 37;
  d.input_valid = 1;
  d.output_ready = 1;
  d.go = 1;
  d.choose = 1;
  d.address = 2;
  d.rst = 1;
  settle(d);
  if (d.zlang_event || d.count || d.observed || d.memory_data || d.rom_data)
    return 1;
  clock_step(d);

  d.rst = 0;
  settle(d);
  if (!d.zlang_event || d.count || d.observed) return 2;
  if (!d.output_valid || d.output_payload != 37 || !d.input_ready) return 3;
  clock_step(d);
  d.go = 0;
  settle(d);
  if (d.zlang_event || d.count != 1 || d.front != 37 || d.observed != 37)
    return 4;
  if (d.rom_data != 12) return 5;

  d.go = 1;
  d.choose = 0;
  settle(d);
  if (d.zlang_event || d.count != 1 || d.observed != 37) return 6;
  clock_step(d);
  d.go = 0;
  settle(d);
  if (d.count || d.observed != 38 || d.memory_data != 37) return 7;

  d.input_payload = 99;
  d.go = 1;
  d.choose = 1;
  d.rst = 1;
  settle(d);
  if (d.zlang_event) return 8;
  clock_step(d);
  settle(d);
  if (d.count || d.observed || d.memory_data || d.rom_data) return 9;
  return 0;
}
"""


def _module(source: str, top: str):
    return compile_source(source, top=top, include_clash=False).ir


def _assert_conditional_scheduler(text: str, *, effects: int = 1) -> None:
    assert "zlang_condition_0_active" in text
    for index in range(effects):
        assert f"zlang_action_{index}_enable" in text


def test_scalar_top_and_child_materialize_delay_from_action_activation() -> None:
    child = _module(SCALAR_DELAY_SOURCE, "DelayedNestedChild")
    top = _module(SCALAR_DELAY_SOURCE, "DelayedNestedTop")
    delay_name = module_rtl_names(child).stage("delay", 0, 1)

    for emitted in (emit_clash(child), emit_clash(top)):
        assert f"{delay_name} = register" in emitted
        assert f"zlang_condition_0_active = {delay_name}" in emitted
        assert "zlang_action_0_enable" in emitted
    for emitted in (emit_systemverilog(child), emit_systemverilog(top)):
        assert f"logic {delay_name};" in emitted
        assert f"zlang_condition_0_active = {delay_name}" in emitted


def test_mixed_ready_valid_route_uses_one_axis_for_sibling_effects() -> None:
    module = _module(MIXED_READY_VALID_SOURCE, "ConditionalMixed")
    clash = emit_clash(module)
    direct = emit_systemverilog(module)

    _assert_conditional_scheduler(clash, effects=2)
    assert clash.count("zlang_condition_0_active =") == 1
    assert "zlang_condition_1_active" not in clash
    assert "conditionalMixedTransition ::" not in clash
    assert "zlang_condition_0_active" in direct


def test_ordinary_and_aggregate_hierarchy_preserve_root_activations() -> None:
    for source, top in (
        (ORDINARY_HIERARCHY_SOURCE, "ConditionalRvTop"),
        (AGGREGATE_HIERARCHY_SOURCE, "ConditionalAggregateTop"),
    ):
        module = _module(source, top)
        clash = emit_clash(module)
        direct = emit_systemverilog(module)
        _assert_conditional_scheduler(clash, effects=2)
        assert "zlang_condition_0_active" in direct
        assert "rule_" in direct
        assert "fire && zlang_condition_0_active" in direct


def test_storage_route_enables_every_nested_effect() -> None:
    module = _module(STORAGE_SOURCE, "ConditionalStorage")
    clash = emit_clash(module)
    direct = emit_systemverilog(module)

    _assert_conditional_scheduler(clash, effects=8)
    for marker in (
        "queue_push",
        "queue_pop",
        "table_read",
        "table_write",
        "state",
        "event",
    ):
        assert marker in clash
        assert marker in direct


def test_csr_top_and_hierarchical_csr_child_preserve_activation() -> None:
    bank = _module(CSR_SOURCE, "ConditionalCsrBank")
    wrapper = _module(CSR_SOURCE, "ConditionalCsrTop")

    for emitted in (emit_clash(bank), emit_systemverilog(bank)):
        assert "zlang_condition_0_active" in emitted
        assert "csr_control_control_enable" in emitted

    clash = emit_clash(wrapper)
    _assert_conditional_scheduler(clash)
    assert "counter = register" in clash
    assert "bank_count" in clash
    assert "csr_control_control_enable" in clash
    direct = emit_systemverilog(wrapper)
    assert "zlang_condition_0_active" in direct
    assert "csr_control_control_enable" in direct


def test_request_response_standalone_and_hierarchy_preserve_activation() -> None:
    requester = _module(REQUEST_RESPONSE_SOURCE, "ConditionalRequester")
    responder = _module(REQUEST_RESPONSE_SOURCE, "ConditionalResponder")
    top = _module(REQUEST_RESPONSE_SOURCE, "ConditionalRequestResponseTop")

    for module in (requester, responder):
        clash = emit_clash(module)
        direct = emit_systemverilog(module)
        _assert_conditional_scheduler(clash)
        assert "zlang_condition_0_active" in direct

    clash = emit_clash(top)
    assert clash.count("zlang_condition_0_active =") == 2
    assert "protocol_conditionalRequester" in clash
    assert "protocol_conditionalResponder" in clash
    assert "rr_requester_bus_requester_outstanding" in clash
    direct = emit_systemverilog(top)
    assert "zlang_condition_0_active" in direct
    assert module_rtl_names(top).child_signal("requester", "marker") in direct
    assert "rr_requester_bus_requester_outstanding" in direct


def test_buffered_connection_closes_locals_and_preserves_activation() -> None:
    # Use the typed semantic IR directly so the backend sees the ordered local
    # bindings before the canonical optimizer legally inlines them.
    module = analyze(parse(BUFFERED_CONNECTION_SOURCE))
    clash = emit_clash(module)
    _assert_conditional_scheduler(clash)
    assert "armed =" in clash
    assert "event_value =" in clash
    assert "input_output_buffer_count = register" in clash

    direct = emit_systemverilog(module)
    assert "zlang_condition_0_active" in direct
    assert "rule_update_fire && zlang_condition_0_active" in direct
    assert "ZLangRvFifo_8_1" in direct
    assert "zlang_local_fifo_0" in direct


def test_clash_out_of_order_requester_preserves_nested_state_and_output() -> None:
    module = _module(
        OUT_OF_ORDER_REQUEST_RESPONSE_SOURCE, "ConditionalTaggedRequester"
    )
    clash = emit_clash(module)

    _assert_conditional_scheduler(clash, effects=2)
    assert "bus_ids" in clash
    assert "state = register" in clash
    assert "event =" in clash


def test_protocol_hierarchy_top_storage_uses_one_atomic_transition() -> None:
    for source, top in (
        (ORDINARY_HIERARCHY_STORAGE_SOURCE, "ConditionalStorageRvTop"),
        (
            AGGREGATE_HIERARCHY_STORAGE_SOURCE,
            "ConditionalStorageAggregateTop",
        ),
    ):
        module = _module(source, top)
        clash = emit_clash(module)
        direct = emit_systemverilog(module)
        _assert_conditional_scheduler(clash, effects=8)
        for marker in (
            "queue_count = register",
            "table_cells = register",
            "constants_read_data",
            "state = register",
            "event =",
        ):
            assert marker in clash
        assert "zlang_condition_0_active" in direct


def test_clash_unconditional_rule_effects_use_resolved_transition_routes() -> None:
    mixed = emit_clash(_module(
        UNNESTED_MIXED_OUTPUT_SOURCE, "UnnestedMixedOutput"
    ))
    assert "rule_update_fire" in mixed
    assert "state = register" in mixed
    assert "fired =" in mixed

    hierarchy = emit_clash(_module(
        UNNESTED_HIERARCHY_SOURCE, "UnnestedHierarchyTop"
    ))
    assert "rule_track_fire" in hierarchy
    assert "seen = register" in hierarchy
    assert "seen_next" in hierarchy


@pytest.mark.skipif(
    VERILATOR is None or CLASH is None,
    reason="real Clash and Verilator are required",
)
@pytest.mark.parametrize(
    ("source", "top"),
    (
        (UNNESTED_MIXED_OUTPUT_SOURCE, "UnnestedMixedOutput"),
        (UNNESTED_HIERARCHY_SOURCE, "UnnestedHierarchyTop"),
    ),
)
def test_clash_unconditional_transition_routes_generate_and_lint(
    source: str, top: str, tmp_path,
) -> None:
    rtl = generate_verilog(
        emit_clash(_module(source, top)), top, tmp_path / top, CLASH
    )
    assert rtl
    lint_with_verilator(rtl, top)


@pytest.mark.skipif(
    VERILATOR is None or CLASH is None,
    reason="real Clash and Verilator are required",
)
@pytest.mark.parametrize(
    ("source", "top"),
    (
        (BUFFERED_CONNECTION_SOURCE, "ConditionalBufferedConnection"),
        (CSR_SOURCE, "ConditionalCsrTop"),
        (REQUEST_RESPONSE_SOURCE, "ConditionalRequester"),
        (REQUEST_RESPONSE_SOURCE, "ConditionalResponder"),
        (REQUEST_RESPONSE_SOURCE, "ConditionalRequestResponseTop"),
        (
            OUT_OF_ORDER_REQUEST_RESPONSE_SOURCE,
            "ConditionalTaggedRequester",
        ),
        (ORDINARY_HIERARCHY_STORAGE_SOURCE, "ConditionalStorageRvTop"),
        (
            AGGREGATE_HIERARCHY_STORAGE_SOURCE,
            "ConditionalStorageAggregateTop",
        ),
    ),
)
def test_clash_nested_composition_routes_generate_and_lint(
    source: str, top: str, tmp_path: Path,
) -> None:
    artifact = emit_clash_artifact(_module(source, top))
    rtl = generate_verilog(
        artifact.text,
        top,
        tmp_path / top,
        CLASH,
        companions=artifact.companions,
    )
    assert rtl
    if not artifact.companions:
        lint_with_verilator(rtl, top)
        return
    # Clash 1.11's romFile primitive widens its safe array index to host Int.
    # Keep Verilator fatal for every other default warning while waiving only
    # that upstream primitive shape.
    completed = subprocess.run(
        [
            VERILATOR,
            "--lint-only",
            "-Wno-WIDTHTRUNC",
            "--top-module",
            top,
            *(str(path) for path in rtl),
        ],
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def _run_clash_behavior(
    source: str,
    top: str,
    harness: str,
    root: Path,
) -> None:
    artifact = emit_clash_artifact(_module(source, top))
    rtl = generate_verilog(
        artifact.text,
        top,
        root / "rtl",
        CLASH,
        companions=artifact.companions,
    )
    for companion in artifact.companions:
        destination = root / companion.logical_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(companion.text)
    harness_path = root / "nested_when.cpp"
    harness_path.write_text(harness)
    obj = root / "obj"
    command = [
        VERILATOR,
        "--cc",
        "--exe",
        "--build",
        "--top-module",
        top,
        "--Mdir",
        str(obj),
        "-o",
        "nested_when_sim",
    ]
    if artifact.companions:
        command.append("-Wno-WIDTHTRUNC")
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (*command, *(str(path) for path in rtl), str(harness_path)),
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(obj / "nested_when_sim"),),
        cwd=root,
        text=True,
        capture_output=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.skipif(
    VERILATOR is None or CLASH is None,
    reason="real Clash and Verilator are required",
)
@pytest.mark.parametrize(
    ("source", "top", "harness"),
    (
        (
            BUFFERED_CONNECTION_SOURCE,
            "ConditionalBufferedConnection",
            BUFFERED_CONNECTION_HARNESS,
        ),
        (CSR_SOURCE, "ConditionalCsrTop", CSR_HIERARCHY_HARNESS),
        (
            REQUEST_RESPONSE_SOURCE,
            "ConditionalRequestResponseTop",
            REQUEST_RESPONSE_HIERARCHY_HARNESS,
        ),
        (
            ORDINARY_HIERARCHY_STORAGE_SOURCE,
            "ConditionalStorageRvTop",
            HIERARCHY_STORAGE_HARNESS,
        ),
    ),
)
def test_clash_nested_composition_routes_simulate_atomicity_and_reset(
    source: str, top: str, harness: str, tmp_path: Path,
) -> None:
    _run_clash_behavior(source, top, harness, tmp_path)


@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
@pytest.mark.parametrize(
    ("source", "top"),
    (
        (SCALAR_DELAY_SOURCE, "DelayedNestedChild"),
        (MIXED_READY_VALID_SOURCE, "ConditionalMixed"),
        (AGGREGATE_HIERARCHY_SOURCE, "ConditionalAggregateTop"),
        (STORAGE_SOURCE, "ConditionalStorage"),
        (CSR_SOURCE, "ConditionalCsrTop"),
        (REQUEST_RESPONSE_SOURCE, "ConditionalRequestResponseTop"),
        (BUFFERED_CONNECTION_SOURCE, "ConditionalBufferedConnection"),
    ),
)
def test_successful_direct_routes_are_strict_lint_clean(
    source: str, top: str, tmp_path,
) -> None:
    rtl = tmp_path / f"{top}.sv"
    rtl.write_text(emit_systemverilog(_module(source, top)))
    lint_with_verilator((rtl,), top)
