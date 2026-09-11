"""Formal-only publication of existing scheduler-accepted rule firing."""

from __future__ import annotations

from dataclasses import replace
from itertools import product
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

import pytest

from zlang.backend.systemverilog import (
    emit_artifact as emit_sv_artifact,
    emit_formal_artifact as emit_sv_formal_artifact,
)
from zlang.compiler import compile_source
from zlang.formal import (
    build_recursive_formal_design,
    connect_formal_design,
    emit_harness,
    run_verilog_formal,
)
from zlang.ir.formal import FormalStatus
from zlang.ir.formal_observations import rule_fire_observation_id
from zlang.ir.state import select_action_groups
from zlang.workspace import load_project_workspace


SCHEDULED_RULES = """
module ScheduledRules {
    clock clk reset rst
    in high_guard, low_guard, seed_guard : bit
    out count : u2

    fifo queue : fifo<u8,2>

    high: when high_guard { queue.push(0x11) }
    low: when low_guard { queue.push(0x22) }
    seed: when seed_guard { queue.push(0x33) }

    priority high > low
    priority low > seed

    count = queue.count
}
"""

ROOT = Path(__file__).resolve().parents[2]
WIFI_CONTROLLER = (
    ROOT / "examples" / "projects" / "80211a_transmitter" / "src" / "controller.zhl"
)
FORMAL_TOOLS = all(shutil.which(tool) for tool in ("yosys", "sby", "z3"))


def _compiled_rules():
    return compile_source(
        SCHEDULED_RULES,
        top="ScheduledRules",
        source_unit="tests/fixtures/rule-fire-scheduler.zhl",
    )


def _rule_tokens(artifact) -> dict[str, str]:
    result = {}
    for binding in artifact.recursive_bindings:
        local_id = binding.local_semantic_id
        if not local_id.startswith("rule:"):
            continue
        assert binding.physical_available
        assert binding.formal_observation_token is not None
        result[local_id] = binding.formal_observation_token
    return result


def _testbench(artifact, transition) -> str:
    observations = tuple(
        item for item in artifact.formal_observations
        if item.physical_available and item.observation_token is not None
    )
    declarations = []
    connections = [
        ".clk(clk)", ".rst(rst)",
        ".high_guard(high_guard)", ".low_guard(low_guard)",
        ".seed_guard(seed_guard)", ".count(count)",
    ]
    for observation in observations:
        packed = "" if observation.width == 1 else f" [{observation.width - 1}:0]"
        declarations.append(f"  wire{packed} {observation.observation_token};")
        connections.append(
            f".{observation.observation_token}({observation.observation_token})"
        )
    tokens = _rule_tokens(artifact)
    physical_modules = {
        item.rtl_module for item in artifact.recursive_bindings
        if item.physical_available and item.rtl_module is not None
    }
    assert len(physical_modules) == 1
    physical_module = next(iter(physical_modules))
    checks = []
    ordinal = 0
    for count in range(3):
        checks.extend((
            "    rst=1; high_guard=0; low_guard=0; seed_guard=0; tick();",
            "    rst=0;",
        ))
        for _ in range(count):
            checks.append("    seed_guard=1; tick();")
        checks.extend((
            "    seed_guard=0; #1;",
            f"    if (count !== 2'd{count}) $fatal(1, \"occupancy {count}\");",
        ))
        for high, low, seed in product((False, True), repeat=3):
            selected = set(select_action_groups(
                transition,
                {"high": high, "low": low, "seed": seed},
                {"queue": count},
            ))
            checks.append(
                "    high_guard={}; low_guard={}; seed_guard={}; #1;".format(
                    int(high), int(low), int(seed)
                )
            )
            for name in ("high", "low", "seed"):
                expected = int(name in selected)
                checks.append(
                    f"    if ({tokens[rule_fire_observation_id(name)]} !== 1'b{expected}) "
                    f"$fatal(1, \"scheduler case {ordinal} {name}\");"
                )
            ordinal += 1
    checks.extend((
        "    rst=1; high_guard=1; low_guard=1; seed_guard=1; #1;",
        *(f"    if ({token} !== 1'b0) $fatal(1, \"reset fire\");"
          for token in tokens.values()),
    ))
    return f"""
module tb;
  logic clk=0, rst=0, high_guard=0, low_guard=0, seed_guard=0;
  wire [1:0] count;
{chr(10).join(declarations)}
  {physical_module} dut({', '.join(connections)});
  task tick; begin #1 clk=1; #1 clk=0; #1; end endtask
  initial begin
{chr(10).join(checks)}
    $finish;
  end
endmodule
"""


def _run_verilator(files: tuple[Path, ...], artifact, transition, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    testbench = root / "tb.sv"
    testbench.write_text(_testbench(artifact, transition))
    obj = root / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    built = subprocess.run(
        (
            "verilator", "--binary", "--timing", "-Wno-fatal",
            "--top-module", "tb", "--Mdir", str(obj),
            *(str(path) for path in files), str(testbench),
        ),
        cwd=root,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    ran = subprocess.run(
        (str(obj / "Vtb"),), cwd=root, capture_output=True, text=True
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr


def test_rule_fire_catalog_is_formal_only_and_preserves_component_roles() -> None:
    compilation = _compiled_rules()
    design = build_recursive_formal_design(compilation.ir)
    local = [
        item for item in design.bindings
        if item.ref.local_semantic_id.startswith("rule:")
    ]
    assert [item.ref.local_semantic_id for item in local] == [
        "rule:high.fire", "rule:low.fire", "rule:seed.fire",
    ]
    assert all(item.width == 1 and item.signedness == "bit" for item in local)
    assert all(item.ref.source_origin is not None for item in local)
    (component,) = design.components
    assert not any(item.startswith("rule:") for item in component.semantic_state)
    assert set(item.ref.local_semantic_id for item in local) <= set(
        component.formal_observations
    )






def test_wifi_controller_priority_rules_are_connected_to_physical_fire() -> None:
    workspace = load_project_workspace(WIFI_CONTROLLER.resolve())
    assert workspace is not None
    compilation = compile_source(
        WIFI_CONTROLLER.read_text(),
        top="IeeeDataFramer24",
        source_unit=(
            "examples/projects/80211a_transmitter/src/controller.zhl"
        ),
        module_resolver=workspace.resolver,
        dependency_closure=workspace.dependency_closure,
    )
    design = build_recursive_formal_design(compilation.ir)
    artifact = emit_sv_formal_artifact(compilation.ir, design)
    connected = connect_formal_design(compilation.formal_design, artifact)
    priorities = [
        item for item in connected.properties
        if (item.generated_from or "").startswith("priority:")
    ]
    assert {item.generated_from for item in priorities} == {
        "priority:accept_command>accept_emit",
        "priority:accept_emit>accept_buffer",
        "priority:accept_buffer>flush_tail_and_pad",
    }
    assert all(item.non_executable_reason is None for item in priorities)
    assert all(item.source_origin is not None for item in priorities)
    rule_bindings = [
        item for item in artifact.recursive_bindings
        if item.local_semantic_id.startswith("rule:")
    ]
    assert len(rule_bindings) == 6
    assert all(item.physical_available for item in rule_bindings)
    assert all(item.formal_observation_token for item in rule_bindings)


@pytest.mark.skipif(not FORMAL_TOOLS, reason="Yosys, SBY, and Z3 are required")
def test_priority_fire_mutation_has_source_attributed_counterexample() -> None:
    compilation = _compiled_rules()
    design = build_recursive_formal_design(compilation.ir)
    artifact = emit_sv_formal_artifact(compilation.ir, design)
    connected = connect_formal_design(compilation.formal_design, artifact)
    priority = next(
        item for item in connected.properties
        if item.generated_from == "priority:high>low"
    )
    focused = replace(connected, properties=(priority,), covers=())
    harness = emit_harness(focused, depth=4)
    passed = run_verilog_formal(
        harness,
        top="ScheduledRules__m35_formal",
        property_id=priority.id,
        depth=4,
        systemverilog=True,
        source_origin=priority.source_origin,
        timeout_seconds=30,
    )
    assert passed.status is FormalStatus.BOUNDED_PASS

    low = _rule_tokens(artifact)[rule_fire_observation_id("low")]
    assignment = next(
        line for line in harness.splitlines()
        if line.strip().startswith(f"assign {low} =")
    )
    mutated = harness.replace(
        assignment,
        f"  assign {low} = (!rst) & high_guard;",
        1,
    )
    assert mutated != harness
    failed = run_verilog_formal(
        mutated,
        top="ScheduledRules__m35_formal",
        property_id=priority.id,
        depth=4,
        systemverilog=True,
        source_origin=priority.source_origin,
        timeout_seconds=30,
    )
    assert failed.status is FormalStatus.FAILED
    assert failed.source_origin == priority.source_origin
    assert failed.counterexample is not None
    assert failed.counterexample.raw_trace
