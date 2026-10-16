# SPDX-License-Identifier: Apache-2.0
"""Compiler/LSP contracts for native debugger metadata."""

from __future__ import annotations

import json
from pathlib import Path

from zlang.lsp.server import LspServer, path_to_uri
from zlang.native_debug_bundle import (
    DEBUG_BUNDLE_SCHEMA,
    DEBUG_OVERLAY_SCHEMA,
    build_native_debug_bundle,
)
from zlang.simulation_plan_model import SimulationPlan


ROOT = Path(__file__).resolve().parents[2]


def test_debug_site_covers_typed_actions_without_changing_the_execution_plan() -> None:
    from zlang import sim

    source = ROOT / "examples/rule_action.zhl"
    program = sim.compile(source, top="RuleAction", logic_state=True)
    bundle = build_native_debug_bundle(source, top="RuleAction")
    site = next(item for item in bundle.sites if item.kind == "rule")
    assert (site.start_line, site.end_line) == (10, 12)
    assert bundle.plan_bytes == program.plan.to_bytes()
    assert bundle.plan_identity == program.plan.identity
    assert bundle.selected_ir_identity == program.selected_ir_identity


def test_debug_action_ranges_stop_before_the_following_rule(tmp_path: Path) -> None:
    source = tmp_path / "IndependentActions.zhl"
    source.write_text(
        "module IndependentActions {\n"
        "  clock clk reset rst\n"
        "  in a, b : bit\n"
        "  out reg x : u8 = 0\n"
        "  out reg y : u8 = 0\n"
        "  when a {\n"
        "    x <- 1\n"
        "  }\n"
        "  when b {\n"
        "    y <- 2\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    sites = sorted(build_native_debug_bundle(source).sites, key=lambda site: site.start_line)
    assert [(site.start_line, site.end_line) for site in sites] == [(6, 7), (9, 10)]


def test_checked_in_rule_action_launch_replays_documented_steps() -> None:
    from zlang import sim
    from zlang.sim_cli import _read_events

    configuration = json.loads((ROOT / ".vscode/launch.json").read_text())["configurations"][0]
    assert configuration["type"] == "zlang-hdl"
    assert configuration["request"] == "launch"
    assert configuration["stopOnEntry"] is True
    assert configuration["logicState"] is True
    source = Path(configuration["program"].replace("${workspaceFolder}", str(ROOT)))
    events = Path(configuration["events"].replace("${workspaceFolder}", str(ROOT)))
    bundle = build_native_debug_bundle(source, top=configuration["top"])
    assert set(configuration["traceSignals"]) <= {item.name for item in bundle.signals}
    observed = []
    fired = []
    with sim.compile(source, top=configuration["top"], logic_state=True).create() as instance:
        assert instance.get_logic("count").to_bits() == "0" * 8
        for event in _read_events(events):
            instance.run_events((event,))
            observed.append(instance.get_logic("count").require_binary(name="count"))
            fired.append(instance.get_logic("fired").require_binary(name="fired"))
    assert observed == [0, 0, 1, 2, 2]
    assert fired == [0, 0, 1, 1, 0]


def _open(server: LspServer, source: Path, text: str) -> None:
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {
            "uri": path_to_uri(source), "version": 1, "text": text,
        }},
    })


def test_native_debug_bundle_is_deterministic_and_keeps_paths_presentational() -> None:
    source = ROOT / "examples" / "logic_state_register.zhl"
    first = build_native_debug_bundle(source, top="LogicStateRegister")
    second = build_native_debug_bundle(source, top="LogicStateRegister")

    assert first == second
    assert first.plan_bytes == second.plan_bytes
    round_trip = SimulationPlan.from_bytes(first.plan_bytes)
    assert round_trip.to_bytes() == first.plan_bytes
    assert round_trip.identity == first.plan_identity
    assert round_trip.scheduler_fire_nodes == ()
    assert first.schema == DEBUG_BUNDLE_SCHEMA
    assert first.to_data()["overlay_schema"] == DEBUG_OVERLAY_SCHEMA
    assert first.plan_identity
    assert first.selected_ir_identity
    assert first.overlay_identity
    assert first.identity_data() == second.identity_data()
    assert all("presentation_path" not in item for item in first.identity_data()["sources"])
    assert any(item.presentation_path == str(source) for item in first.sources)
    assert {(item.name, item.direction) for item in first.signals} >= {
        ("load", "input"), ("value", "input"), ("observed", "output"),
    }
    site = next(item for item in first.sites if item.kind == "rule")
    assert site.start_line == 11
    assert site.condition_node is not None
    assert site.clock == "clk"
    assert site.event_id is None


def test_verification_sites_bind_to_runtime_instrumentation_events() -> None:
    source = ROOT / "examples" / "verification" / "bounded_counter.zhl"
    bundle = build_native_debug_bundle(source)
    verification = {
        item.name: item for item in bundle.sites if item.kind == "verification"
    }

    assert set(verification) == {
        "capacity", "exceeds_capacity", "reaches_capacity",
    }
    assert all(item.event_id is not None for item in verification.values())
    assert all(item.condition_node is None for item in verification.values())


def test_debug_signal_catalog_retains_memory_shape() -> None:
    source = ROOT / "examples" / "sync_memory.zhl"
    bundle = build_native_debug_bundle(source, top="SyncMemory")
    memory = next(item for item in bundle.signals if item.kind == "memory")

    assert memory.width > 0
    assert memory.depth is not None and memory.depth > 1


def test_lsp_debug_bundle_requires_saved_sources_and_publishes_exact_schemas(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Debug.zhl"
    saved = (
        "module Debug {\n"
        "  clock clk reset rst\n"
        "  in load:bit in value:u8 out observed:u8\n"
        "  reg stored:u8\n"
        "  when load { stored <- value }\n"
        "  observed = stored\n"
        "}\n"
    )
    source.write_text(saved, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    _open(server, source, saved)
    response = server.dispatch({
        "jsonrpc": "2.0", "id": 41, "method": "zlang/prepareDebugBundle",
        "params": {"textDocument": {"uri": uri}, "top": "Debug"},
    })
    result = response[0]["result"]
    assert result["schema"] == DEBUG_BUNDLE_SCHEMA
    assert result["overlay_schema"] == DEBUG_OVERLAY_SCHEMA
    assert result["plan"]

    dirty = saved.replace("observed = stored", "observed = value")
    server.dispatch({
        "jsonrpc": "2.0", "method": "textDocument/didChange",
        "params": {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": dirty}],
        },
    })
    rejected = server.dispatch({
        "jsonrpc": "2.0", "id": 42, "method": "zlang/prepareDebugBundle",
        "params": {"textDocument": {"uri": uri}, "top": "Debug"},
    })
    assert rejected[0]["error"]["code"] == -32602
    assert "Save All before debugging" in rejected[0]["error"]["message"]
