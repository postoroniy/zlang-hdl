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

from zlang.backend.naming import module_rtl_names
from zlang.backend.systemverilog import emit_experimental as emit_systemverilog
from zlang.compiler import compile_source
from zlang.parser import parse
from zlang.semantic import analyze
from zlang.toolchain import lint_with_verilator


VERILATOR = shutil.which("verilator")


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
    return compile_source(source, top=top).ir


def _assert_conditional_scheduler(text: str, *, effects: int = 1) -> None:
    assert "zlang_condition_0_active" in text
    for index in range(effects):
        assert f"zlang_action_{index}_enable" in text






























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
