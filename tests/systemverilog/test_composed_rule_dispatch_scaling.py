"""Bounded regressions for ordinary-rule hierarchy dispatch and traversal.

The 27-rule shape is the smallest synthetic witness for the ZL-017 failure:
each rule writes an independent register, so direct-SV emission must not build
the global scheduler truth table merely because the module is a child.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

import zlang.backend.systemverilog.emitter as sv_emitter
from zlang.backend.systemverilog import SystemVerilogEmissionError, emit_artifact
from zlang.compiler import compile_source
from zlang.ir import hierarchy as ir_hierarchy
from zlang.ir.state import StateResourceKind, groups_conflict
from zlang.simulate import simulate_cycles


RULE_COUNT = 27
ROOT = Path(__file__).resolve().parents[2]


def _independent_rule_source() -> str:
    child_inputs = "\n".join(
        f"    in enable_{index} : bit" for index in range(RULE_COUNT)
    )
    child_outputs = "\n".join(
        f"    out state_{index} : uint<W>" for index in range(RULE_COUNT)
    )
    registers = "\n".join(
        f"    reg value_{index} : uint<W> = 0" for index in range(RULE_COUNT)
    )
    rules = "\n".join(
        f"    rule write_{index} when enable_{index} "
        f"{{ value_{index} <- data }}"
        for index in range(RULE_COUNT)
    )
    child_assignments = "\n".join(
        f"    state_{index} = value_{index}" for index in range(RULE_COUNT)
    )
    wrapper_inputs = "\n".join(
        f"    in {owner}_enable_{index} : bit"
        for owner in ("first", "second")
        for index in range(RULE_COUNT)
    )
    wrapper_outputs = "\n".join(
        f"    out {owner}_state_{index} : u8"
        for owner in ("first", "second")
        for index in range(RULE_COUNT)
    )
    bindings = "\n".join(
        ["    first.data = first_data", "    second.data = second_data"]
        + [
            f"    {owner}.enable_{index} = {owner}_enable_{index}"
            for owner in ("first", "second")
            for index in range(RULE_COUNT)
        ]
    )
    wrapper_assignments = "\n".join(
        f"    {owner}_state_{index} = {owner}.state_{index}"
        for owner in ("first", "second")
        for index in range(RULE_COUNT)
    )
    return f"""
module IndependentRuleChild<W=8> {{
    clock clk reset rst
    in data : uint<W>
{child_inputs}
{child_outputs}
{registers}
{rules}
{child_assignments}
}}

module IndependentRuleWrapper {{
    clock clk reset rst
    in first_data, second_data : u8
{wrapper_inputs}
{wrapper_outputs}
    inst first : IndependentRuleChild<W=8>
    inst second : IndependentRuleChild<W=8>
{bindings}
{wrapper_assignments}
}}
"""


INDEPENDENT_RULE_SOURCE = _independent_rule_source()


PRIORITY_ATOMIC_SOURCE = """
module PriorityAtomicChild {
    clock clk reset rst
    in high, low : bit
    out owner, high_side, low_side : u8
    reg owner_state : u8 = 0
    reg high_state : u8 = 0
    reg low_state : u8 = 0

    priority {
        higher: when high {
            owner_state <- 1
            high_state <- 161
        }
        lower: when low {
            owner_state <- 2
            low_state <- 178
        }
    }

    owner = owner_state
    high_side = high_state
    low_side = low_state
}

module PriorityAtomicWrapper {
    clock clk reset rst
    in high, low : bit
    out owner, high_side, low_side : u8
    inst child : PriorityAtomicChild { high low }
    owner = child.owner
    high_side = child.high_side
    low_side = child.low_side
}
"""


CACHE_SOURCE = """
module CacheLeaf {
    in x : u8
    out y : u8
    y = x
}

module CacheBranch {
    in x : u8
    out left_y, right_y : u8
    inst left : CacheLeaf { x }
    inst right : CacheLeaf { x }
    left_y = left.y
    right_y = right.y
}

module CacheTop {
    in x : u8
    out first_y, second_y : u8
    inst first : CacheBranch { x }
    inst second : CacheBranch { x }
    first_y = first.left_y
    second_y = second.right_y
}
"""


MALFORMED_SPECIALIZATION_SOURCE = """
module WidthChild<W=8> {
    in x : uint<W>
    out y : uint<W>
    y = x
}

module WidthTop {
    in narrow : u8
    in wide : u16
    out narrow_y : u8
    out wide_y : u16
    inst first : WidthChild<W=8> { x=narrow }
    inst second : WidthChild<W=16> { x=wide }
    narrow_y = first.y
    wide_y = second.y
}
"""


SAFE_READY_VALID_FIFO_SOURCE = """
module SafeReadyValidFifo {
    clock clk
    async reset arst @clk
    in rx : rv<u8>
    out tx : rv<u8>
    fifo queue : fifo<u8,2>
    queue.data = rx.payload
    queue.push = rx.transfer
    queue.pop = tx.transfer
    rx.ready = queue.ready
    tx.payload = queue.front
    tx.valid = queue.valid
}
"""


GLOBAL_MEMORY_SOURCE = """
module GlobalMemory {
    clock clk reset rst
    in read_address : u2
    in write_enable : bit
    in write_address : u2
    in write_data : u8
    out read_data : u8
    memory table : mem<u8,4> {
        read_latency 1
        collision write_first
    }
    table.read_address = read_address
    table.write_enable = write_enable
    table.write_address = write_address
    table.write_data = write_data
    read_data = table.read_data
}
"""


CSR_SOURCE = """
module DedicatedCsr {
    clock clk reset rst
    csr control @0 {
        CONTROL @0 {
            enable bit @0 rw = 0
            reserved bits<31> @31:1 reserved
        }
    }
}
"""


REQUEST_RESPONSE_SOURCE = """
module DedicatedRequester {
    clock clk reset rst
    interface mem : request_response<u8,u16> {
        max_outstanding 2
        ordering in_order
    }
    in request_payload : u8
    in issue : bit
    in accept : bit
    out response_payload : u16
    mem.request.payload = request_payload
    mem.request.valid = issue
    mem.response.ready = accept
    response_payload = mem.response.payload
}
"""


READY_VALID_SOURCE = """
module DedicatedReadyValid {
    in source : rv<u8>
    out sink : rv<u8>
    source.ready = sink.ready
    sink.payload = source.payload
    sink.valid = source.valid
}
"""


SCHEDULED_FIFO_SOURCE = """
module ScheduledFifoState {
    clock clk reset rst
    in data : u8
    in push, pop : bit
    out front : u8
    fifo queue : fifo<u8,2>
    rule enqueue when push { queue.push(data) }
    rule dequeue when pop { queue.pop() }
    front = queue.front
}
"""


SCHEDULED_MEMORY_SOURCE = """
module ScheduledMemoryState {
    clock clk reset rst
    in address : u2
    in read_enable, write_enable : bit
    in data : u8
    out value : u8
    memory table : mem<u8,4> {
        read_latency 1
        collision write_first
    }
    rule fetch when read_enable { table.read(address) }
    rule store when write_enable { table.write(address,data) }
    value = table.read_data
}
"""


MIXED_GLOBAL_FIFO_SOURCE = """
module MixedGlobalFifoState {
    clock clk reset rst
    in data : u8
    in push, pop, remember : bit
    out value : u8
    fifo queue : fifo<u8,2>
    queue.data = data
    queue.push = push
    queue.pop = pop
    reg remembered : u8 = 0
    rule save when remember { remembered <- data }
    value = remembered
}
"""


def _module_definition(text: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^module {re.escape(name)} \(.*?^endmodule$",
        text,
    )
    assert match is not None, f"missing SystemVerilog module {name}"
    return match.group(0)


def _normalized_definition(text: str, name: str) -> str:
    definition = _module_definition(text, name)
    return definition.replace(f"module {name} (", "module DUT (", 1)


def _independent_cycles() -> tuple[list[dict[str, int]], list[bool]]:
    disabled = {
        f"{owner}_enable_{index}": 0
        for owner in ("first", "second")
        for index in range(RULE_COUNT)
    }

    def cycle(
        first_data: int,
        second_data: int,
        first_enabled: tuple[int, ...] = (),
        second_enabled: tuple[int, ...] = (),
    ) -> dict[str, int]:
        values = {
            "first_data": first_data,
            "second_data": second_data,
            **disabled,
        }
        values.update(
            {f"first_enable_{index}": 1 for index in first_enabled}
        )
        values.update(
            {f"second_enable_{index}": 1 for index in second_enabled}
        )
        return values

    return (
        [
            cycle(0, 0),
            cycle(0x2A, 0x3C, (0, 13, 26), (1, 14, 25)),
            cycle(0xFF, 0xEE),
            cycle(0x55, 0x77, (1,), (2,)),
            cycle(0, 0),
            cycle(
                0xFF,
                0xEE,
                tuple(range(RULE_COUNT)),
                tuple(range(RULE_COUNT)),
            ),
            cycle(0, 0),
        ],
        [True, False, False, False, False, True, False],
    )


def _standalone_cycles(
    wrapper_cycles: list[dict[str, int]], owner: str
) -> list[dict[str, int]]:
    return [
        {
            "data": cycle[f"{owner}_data"],
            **{
                f"enable_{index}": cycle[f"{owner}_enable_{index}"]
                for index in range(RULE_COUNT)
            },
        }
        for cycle in wrapper_cycles
    ]


def test_27_independent_register_rules_use_bounded_child_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    standalone = compile_source(
        INDEPENDENT_RULE_SOURCE,
        top="IndependentRuleChild",
    ).ir
    wrapper = compile_source(
        INDEPENDENT_RULE_SOURCE,
        top="IndependentRuleWrapper",
    ).ir
    transition = wrapper.children[0].resolved_transition
    assert transition is not None
    assert len(transition.action_groups) == RULE_COUNT
    assert {
        resource.kind for resource in transition.resources
    } == {StateResourceKind.REGISTER}
    assert all(
        not groups_conflict(left, right)
        for index, left in enumerate(transition.action_groups)
        for right in transition.action_groups[index + 1 :]
    )
    assert (
        wrapper.elaborated_instances[0].specialization_identity
        == wrapper.elaborated_instances[1].specialization_identity
    )

    def forbidden_selection_regions(*_args, **_kwargs):
        raise AssertionError(
            "independent register rules must not enumerate scheduler regions"
        )

    monkeypatch.setattr(sv_emitter, "selection_regions", forbidden_selection_regions)
    standalone_artifact = emit_artifact(standalone)
    first_wrapper_artifact = emit_artifact(wrapper)
    second_wrapper_artifact = emit_artifact(wrapper)

    definitions = re.findall(
        r"(?m)^module (IndependentRuleChild_s[A-Za-z0-9_]+) \(",
        first_wrapper_artifact.text,
    )
    assert len(definitions) == 1
    child_component = definitions[0]
    assert first_wrapper_artifact.text.count(f"{child_component} first (") == 1
    assert first_wrapper_artifact.text.count(f"{child_component} second (") == 1
    assert _normalized_definition(
        standalone_artifact.text, "IndependentRuleChild"
    ) == _normalized_definition(first_wrapper_artifact.text, child_component)
    assert first_wrapper_artifact.text == second_wrapper_artifact.text
    assert first_wrapper_artifact.artifact_hash == second_wrapper_artifact.artifact_hash
    assert first_wrapper_artifact.to_json() == second_wrapper_artifact.to_json()

    wrapper_cycles, resets = _independent_cycles()
    first_trace = simulate_cycles(
        standalone,
        _standalone_cycles(wrapper_cycles, "first"),
        reset=resets,
    )
    second_trace = simulate_cycles(
        standalone,
        _standalone_cycles(wrapper_cycles, "second"),
        reset=resets,
    )
    wrapper_trace = simulate_cycles(wrapper, wrapper_cycles, reset=resets)
    for first_row, second_row, wrapper_row in zip(
        first_trace, second_trace, wrapper_trace, strict=True
    ):
        for index in range(RULE_COUNT):
            assert wrapper_row[f"first_state_{index}"] == first_row[
                f"state_{index}"
            ]
            assert wrapper_row[f"second_state_{index}"] == second_row[
                f"state_{index}"
            ]

    assert first_trace[2]["state_0"] == 0x2A
    assert first_trace[2]["state_13"] == 0x2A
    assert first_trace[2]["state_26"] == 0x2A
    assert first_trace[2]["state_1"] == 0
    assert second_trace[2]["state_1"] == 0x3C
    assert second_trace[2]["state_14"] == 0x3C
    assert second_trace[2]["state_25"] == 0x3C
    assert second_trace[2]["state_0"] == 0
    assert first_trace[3] == first_trace[2]
    assert second_trace[3] == second_trace[2]
    assert first_trace[4]["state_1"] == 0x55
    assert second_trace[4]["state_2"] == 0x77
    assert all(value == 0 for value in first_trace[5].values())
    assert all(value == 0 for value in second_trace[5].values())
    assert first_trace[6] == first_trace[5]
    assert second_trace[6] == second_trace[5]


def test_conflicting_priority_group_keeps_losing_side_effect_atomic() -> None:
    standalone = compile_source(
        PRIORITY_ATOMIC_SOURCE,
        top="PriorityAtomicChild",
    ).ir
    wrapper = compile_source(
        PRIORITY_ATOMIC_SOURCE,
        top="PriorityAtomicWrapper",
    ).ir
    transition = wrapper.children[0].resolved_transition
    assert transition is not None
    higher, lower = transition.action_groups
    assert groups_conflict(higher, lower)

    standalone_artifact = emit_artifact(standalone)
    wrapper_artifact = emit_artifact(wrapper)
    for text in (standalone_artifact.text, wrapper_artifact.text):
        assert "rule_higher_fire" in text
        assert "rule_lower_fire" in text

    cycles = [
        {"high": 0, "low": 0},
        {"high": 1, "low": 1},
        {"high": 0, "low": 0},
        {"high": 0, "low": 1},
        {"high": 0, "low": 0},
        {"high": 1, "low": 1},
        {"high": 0, "low": 0},
    ]
    resets = [True, False, False, False, False, True, False]
    expected = [
        (0, 0, 0),
        (0, 0, 0),
        (1, 161, 0),
        (1, 161, 0),
        (2, 161, 178),
        (0, 0, 0),
        (0, 0, 0),
    ]
    for module in (standalone, wrapper):
        trace = simulate_cycles(module, cycles, reset=resets)
        assert [
            (row["owner"], row["high_side"], row["low_side"])
            for row in trace
        ] == expected


def _verilator_testbench() -> str:
    enable_declarations = "\n".join(
        f"  logic {owner}_enable_{index}=0;"
        for owner in ("first", "second")
        for index in range(RULE_COUNT)
    )
    bank_declarations = "\n".join(
        f"  logic [7:0] {prefix}_state_{index};"
        for prefix in ("standalone_first", "standalone_second", "first", "second")
        for index in range(RULE_COUNT)
    )
    standalone_connections = {
        owner: ",\n".join(
            ["    .clk(clk)", "    .rst(rst)", f"    .data({owner}_data)"]
            + [
                f"    .enable_{index}({owner}_enable_{index})"
                for index in range(RULE_COUNT)
            ]
            + [
                f"    .state_{index}(standalone_{owner}_state_{index})"
                for index in range(RULE_COUNT)
            ]
        )
        for owner in ("first", "second")
    }
    wrapper_connections = ",\n".join(
        [
            "    .clk(clk)",
            "    .rst(rst)",
            "    .first_data(first_data)",
            "    .second_data(second_data)",
        ]
        + [
            f"    .{owner}_enable_{index}({owner}_enable_{index})"
            for owner in ("first", "second")
            for index in range(RULE_COUNT)
        ]
        + [
            f"    .{owner}_state_{index}({owner}_state_{index})"
            for owner in ("first", "second")
            for index in range(RULE_COUNT)
        ]
    )
    mismatch = " ||\n        ".join(
        f"standalone_{owner}_state_{index} !== {owner}_state_{index}"
        for owner in ("first", "second")
        for index in range(RULE_COUNT)
    )
    not_zero = " ||\n        ".join(
        f"standalone_{owner}_state_{index} !== 8'h00"
        for owner in ("first", "second")
        for index in range(RULE_COUNT)
    )
    disable_all = " ".join(
        f"{owner}_enable_{index}=0;"
        for owner in ("first", "second")
        for index in range(RULE_COUNT)
    )
    enable_all = " ".join(
        f"{owner}_enable_{index}=1;"
        for owner in ("first", "second")
        for index in range(RULE_COUNT)
    )
    return f"""
module tb;
  logic clk=0, rst=1;
  logic [7:0] first_data=0, second_data=0;
{enable_declarations}
{bank_declarations}

  logic high=0, low=0;
  logic [7:0] standalone_owner, standalone_high_side, standalone_low_side;
  logic [7:0] wrapped_owner, wrapped_high_side, wrapped_low_side;

  IndependentRuleChild standalone_first (
{standalone_connections["first"]}
  );
  IndependentRuleChild standalone_second (
{standalone_connections["second"]}
  );
  IndependentRuleWrapper wrapped (
{wrapper_connections}
  );
  PriorityAtomicChild priority_standalone (
    .clk(clk), .rst(rst), .high(high), .low(low),
    .owner(standalone_owner), .high_side(standalone_high_side),
    .low_side(standalone_low_side)
  );
  PriorityAtomicWrapper priority_wrapped (
    .clk(clk), .rst(rst), .high(high), .low(low),
    .owner(wrapped_owner), .high_side(wrapped_high_side),
    .low_side(wrapped_low_side)
  );

  task tick; begin #1 clk=1; #1 clk=0; #1; end endtask
  task check_banks_equal; begin
    if ({mismatch}) $fatal(1, "standalone/composed bank mismatch");
  end endtask
  task check_banks_zero; begin
    if ({not_zero}) $fatal(1, "bank did not reset");
  end endtask
  task check_priority_equal; begin
    if (standalone_owner !== wrapped_owner ||
        standalone_high_side !== wrapped_high_side ||
        standalone_low_side !== wrapped_low_side)
      $fatal(1, "standalone/composed priority mismatch");
  end endtask

  initial begin
    tick;
    check_banks_equal; check_banks_zero; check_priority_equal;

    rst=0; first_data=8'h2a; second_data=8'h3c;
    first_enable_0=1; first_enable_13=1; first_enable_26=1;
    second_enable_1=1; second_enable_14=1; second_enable_25=1;
    high=1; low=1; tick;
    check_banks_equal; check_priority_equal;
    if (standalone_first_state_0 !== 8'h2a ||
        standalone_first_state_13 !== 8'h2a ||
        standalone_first_state_26 !== 8'h2a ||
        standalone_first_state_1 !== 8'h00 ||
        standalone_second_state_1 !== 8'h3c ||
        standalone_second_state_14 !== 8'h3c ||
        standalone_second_state_25 !== 8'h3c ||
        standalone_second_state_0 !== 8'h00)
      $fatal(1, "independent rule update failed");
    if (standalone_owner !== 8'h01 || standalone_high_side !== 8'ha1 ||
        standalone_low_side !== 8'h00)
      $fatal(1, "losing priority group leaked a side effect");

    {disable_all} high=0; low=0;
    first_data=8'hff; second_data=8'hee; tick;
    check_banks_equal; check_priority_equal;
    if (standalone_first_state_0 !== 8'h2a ||
        standalone_first_state_13 !== 8'h2a ||
        standalone_first_state_26 !== 8'h2a ||
        standalone_second_state_1 !== 8'h3c ||
        standalone_second_state_14 !== 8'h3c ||
        standalone_second_state_25 !== 8'h3c || standalone_owner !== 8'h01)
      $fatal(1, "state did not hold");

    first_enable_1=1; second_enable_2=1;
    first_data=8'h55; second_data=8'h77; low=1; tick;
    check_banks_equal; check_priority_equal;
    if (standalone_first_state_1 !== 8'h55 ||
        standalone_first_state_0 !== 8'h2a ||
        standalone_second_state_2 !== 8'h77 ||
        standalone_second_state_1 !== 8'h3c)
      $fatal(1, "second independent update failed");
    if (standalone_owner !== 8'h02 || standalone_high_side !== 8'ha1 ||
        standalone_low_side !== 8'hb2)
      $fatal(1, "nonconflicting-cycle lower group did not commit atomically");

    {disable_all} high=0; low=0; tick;
    check_banks_equal; check_priority_equal;
    if (standalone_first_state_1 !== 8'h55 ||
        standalone_second_state_2 !== 8'h77 || standalone_low_side !== 8'hb2)
      $fatal(1, "updated state did not hold");

    rst=1; first_data=8'hff; second_data=8'hee;
    {enable_all} high=1; low=1; tick;
    check_banks_equal; check_banks_zero; check_priority_equal;
    if (standalone_owner !== 0 || standalone_high_side !== 0 ||
        standalone_low_side !== 0)
      $fatal(1, "priority state did not reset");

    rst=0; {disable_all} high=0; low=0; tick;
    check_banks_equal; check_banks_zero; check_priority_equal;
    if (standalone_owner !== 0 || standalone_high_side !== 0 ||
        standalone_low_side !== 0)
      $fatal(1, "reset state did not hold");
    $finish;
  end
endmodule
"""


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_standalone_and_composed_rule_dispatch_match_in_verilator(
    tmp_path: Path,
) -> None:
    artifacts = (
        emit_artifact(
            compile_source(
                INDEPENDENT_RULE_SOURCE,
                top="IndependentRuleChild",
            ).ir
        ),
        emit_artifact(
            compile_source(
                INDEPENDENT_RULE_SOURCE,
                top="IndependentRuleWrapper",
            ).ir
        ),
        emit_artifact(
            compile_source(
                PRIORITY_ATOMIC_SOURCE,
                top="PriorityAtomicChild",
            ).ir
        ),
        emit_artifact(
            compile_source(
                PRIORITY_ATOMIC_SOURCE,
                top="PriorityAtomicWrapper",
            ).ir
        ),
    )
    rtl = tmp_path / "rule_dispatch.sv"
    bench = tmp_path / "tb.sv"
    rtl.write_text("\n".join(artifact.text for artifact in artifacts))
    bench.write_text(_verilator_testbench())
    object_directory = tmp_path / "obj"
    environment = dict(os.environ)
    environment["CCACHE_DISABLE"] = "1"
    build = subprocess.run(
        (
            "verilator",
            "--binary",
            "--timing",
            "-Wno-fatal",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSED",
            "-Wno-UNDRIVEN",
            "--top-module",
            "tb",
            "--Mdir",
            str(object_directory),
            str(rtl),
            str(bench),
        ),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert build.returncode == 0, build.stderr or build.stdout
    run = subprocess.run(
        (str(object_directory / "Vtb"),),
        check=False,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.parametrize(
    ("source", "top", "markers"),
    (
        (
            (ROOT / "examples" / "fifo_bridge.zhl").read_text(),
            "FifoBridge",
            (
                "logic [7:0] queue_storage [0:3];",
                "logic queue_push_request, queue_pop_request;",
            ),
        ),
        (
            SAFE_READY_VALID_FIFO_SOURCE,
            "SafeReadyValidFifo",
            (
                "logic [7:0] queue_storage [0:1];",
                "logic queue_push_request, queue_pop_request;",
            ),
        ),
        (
            GLOBAL_MEMORY_SOURCE,
            "GlobalMemory",
            (
                "logic [7:0] zlang_table_cells [0:3];",
                "integer zlang_memory_reset_index;",
            ),
        ),
        (
            CSR_SOURCE,
            "DedicatedCsr",
            (
                "logic csr_control_control_enable;",
                "if ((write && addr == 32'h00000000))",
            ),
        ),
        (
            REQUEST_RESPONSE_SOURCE,
            "DedicatedRequester",
            (
                "logic [1:0] mem_outstanding;",
                "assign mem_request_transfer =",
            ),
        ),
        (
            READY_VALID_SOURCE,
            "DedicatedReadyValid",
            (
                "assign source_ready = sink_ready;",
                "assign sink_payload = source_payload;",
                "assign sink_valid = source_valid;",
            ),
        ),
    ),
)
def test_standalone_dedicated_dispatch_does_not_fall_into_unified_state(
    source: str,
    top: str,
    markers: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_unified_state(*_args, **_kwargs):
        raise AssertionError(
            f"standalone dedicated emitter for {top} fell into unified state"
        )

    monkeypatch.setattr(
        sv_emitter,
        "_emit_unified_state_module",
        forbidden_unified_state,
    )
    text = emit_artifact(
        compile_source(source, top=top).ir
    ).text
    for marker in markers:
        assert marker in text


@pytest.mark.parametrize(
    ("source", "top", "markers"),
    (
        (
            SCHEDULED_FIFO_SOURCE,
            "ScheduledFifoState",
            ("rule_enqueue_fire", "queue_push", "queue_storage"),
        ),
        (
            SCHEDULED_MEMORY_SOURCE,
            "ScheduledMemoryState",
            ("rule_fetch_fire", "zlang_table_read_fire", "zlang_table_cells"),
        ),
    ),
)
def test_scheduled_storage_remains_on_the_unified_transition_path(
    source: str,
    top: str,
    markers: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = sv_emitter._emit_unified_state_module
    calls: list[str] = []

    def counted(module):
        calls.append(module.name)
        return original(module)

    monkeypatch.setattr(sv_emitter, "_emit_unified_state_module", counted)
    text = emit_artifact(
        compile_source(source, top=top).ir
    ).text

    assert calls == [top]
    for marker in markers:
        assert marker in text


def test_mixed_global_fifo_and_user_rules_still_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    original = sv_emitter._emit_unified_state_module

    def counted(module):
        calls.append(module.name)
        return original(module)

    monkeypatch.setattr(sv_emitter, "_emit_unified_state_module", counted)
    module = compile_source(
        MIXED_GLOBAL_FIFO_SOURCE,
        top="MixedGlobalFifoState",
    ).ir
    with pytest.raises(
        SystemVerilogEmissionError,
        match="combines user registers/rules with globally controlled FIFO",
    ) as caught:
        emit_artifact(module)

    assert caught.value.code == "ZL-BACKEND-SYSTEMVERILOG-UNCLAIMED-STATE"
    assert calls == []


def test_one_emission_fingerprints_each_exact_module_object_at_most_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = compile_source(
        CACHE_SOURCE, top="CacheTop"
    ).ir
    copied = deepcopy(module)
    unrelated = compile_source(
        "module OtherLeaf { in x:u8 out y:u8 y=x } "
        "module OtherTop { in x:u8 out y:u8 inst child:OtherLeaf { x } "
        "y=child.y }",
        top="OtherTop",
    ).ir
    original_fingerprint = ir_hierarchy.specialization_fingerprint
    calls: dict[str, list[object]] = {
        "first": [],
        "copied": [],
        "unrelated": [],
    }
    active = "first"

    def counted(child):
        calls[active].append(child)
        return original_fingerprint(child)

    monkeypatch.setattr(ir_hierarchy, "specialization_fingerprint", counted)
    active = "first"
    first_text = sv_emitter.emit(module)
    active = "copied"
    copied_text = sv_emitter.emit(copied)
    active = "unrelated"
    unrelated_text = sv_emitter.emit(unrelated)

    assert first_text == copied_text
    assert first_text != unrelated_text
    assert {name: len(items) for name, items in calls.items()} == {
        "first": 6,
        "copied": 6,
        "unrelated": 1,
    }
    for items in calls.values():
        identities = Counter(id(item) for item in items)
        assert identities
        assert max(identities.values()) == 1
    assert all(
        first is not second
        for first in calls["first"]
        for second in calls["copied"] + calls["unrelated"]
    )


def test_emission_cache_does_not_hide_malformed_shared_specialization() -> None:
    module = compile_source(
        MALFORMED_SPECIALIZATION_SOURCE,
        top="WidthTop",
    ).ir
    first, second = module.elaborated_instances
    malformed = replace(
        module,
        elaborated_instances=(
            first,
            replace(
                second,
                specialization_identity=first.specialization_identity,
            ),
        ),
    )

    with pytest.raises(
        SystemVerilogEmissionError,
        match="specialization identity .* is reused for incompatible 'WidthChild'",
    ):
        emit_artifact(malformed)
