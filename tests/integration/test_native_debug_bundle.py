# SPDX-License-Identifier: Apache-2.0
"""Compiler/LSP contracts for native debugger metadata."""

from __future__ import annotations

from pathlib import Path

from zlang.lsp.server import LspServer, path_to_uri
from zlang.native_debug_bundle import (
    DEBUG_BUNDLE_SCHEMA,
    DEBUG_OVERLAY_SCHEMA,
    build_native_debug_bundle,
)
from zlang.simulation_plan_model import SimulationPlan


ROOT = Path(__file__).resolve().parents[2]


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
