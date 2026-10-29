# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Typed JSONL is encoded by compiler owners and executed by the reviewed DAP."""

import hashlib
import json
from pathlib import Path

import pytest

from tests.integration.test_native_debug_controls import _transcript
from zlang import sim
from zlang.native_debug_bundle import build_native_debug_bundle
from zlang.native_debug_events import prepare_debug_events
from zlang.sim_cli import _read_events


ROOT = Path(__file__).resolve().parents[2]


def test_cordic_jsonl_launches_with_reviewed_adapter_and_matches_native(tmp_path):
    source = ROOT / "examples/cordic.zhl"
    events = ROOT / "examples/cordic.events.jsonl"
    original = events.read_bytes()
    bundle = build_native_debug_bundle(source, top="Cordic", events=events)
    unchanged = build_native_debug_bundle(source, top="Cordic")
    assert bundle.identity == unchanged.identity
    assert bundle.plan_bytes == unchanged.plan_bytes
    assert bundle.prepared_events.plan_identity == bundle.plan_identity
    assert bundle.prepared_events.source_sha256 == hashlib.sha256(original).hexdigest()
    plan, overlay, stimulus = (tmp_path / name for name in ("plan.json", "overlay.json", "events.jsonl"))
    plan.write_bytes(bundle.plan_bytes)
    data = bundle.to_data()
    data.pop("prepared_events")
    overlay.write_text(json.dumps(data))
    stimulus.write_text(bundle.prepared_events.jsonl)
    signals = ["input.payload", "output.payload.sine", "output.payload.cosine"]
    requests = [("initialize", {}), ("launch", {
        "planPath": str(plan), "overlayPath": str(overlay), "events": str(stimulus),
        "traceSignals": signals, "stopOnEntry": True,
    })]
    requests.extend(("next", {"threadId": 1}) for _ in range(24))
    requests.extend([("zlang/waveformQuery", {"command": "value_at", "signals": signals, "timestamp": 48}), ("disconnect", {})])
    replies = _transcript(requests, ROOT / "release/native-editor/linux-x64/zlang-debug-adapter")
    assert all(reply["success"] for reply in replies.values()), replies
    with sim.compile(source, top="Cordic", logic_state=True).create() as reference:
        reference.run_events(tuple(_read_events(events))[:24])
        observed = reference.get("output")["payload"]
    values = replies[len(requests) - 1]["body"]["values"]
    assert values["input.payload"] == "0" * 16
    for field in ("sine", "cosine"):
        assert values["output.payload." + field] == format(observed[field] & 65535, "016b")
    assert events.read_bytes() == original


def test_signed_wide_struct_and_compact_repeats(tmp_path):
    source = tmp_path / "top.zhl"
    source.write_text("struct Pair {a:s8 b:u4}\n"
                      "module Top {in x:s80 in p:Pair in q:u4 out y:s80 y=x}")
    program = sim.compile(source, top="Top", logic_state=True)
    events = tmp_path / "values.jsonl"
    events.write_text('\n{"set":{"x":-1,"p":{"a":-128,"b":7},"q":1},'
                      '"set_logic":{"q":"x"},"repeat":1000000}\n')
    result = prepare_debug_events(program, events)
    lines = result.jsonl.splitlines()
    assert lines[0] == "" and len(lines) == 2
    record = json.loads(lines[1])
    assert record["repeat"] == 1000000
    assert record["set"] == {}
    assert record["set_logic"] == {"x": "1" * 80, "p": "100000000111", "q": "xxxx"}
    assert result.sha256 == hashlib.sha256(result.jsonl.encode()).hexdigest()


@pytest.mark.parametrize("update, message", [
    ({"input": {"payload": -32769, "valid": 1}}, "does not fit"),
    ({"input": {"payload": 0}}, "requires fields"),
    ({"output": {"payload": 0}}, "requires fields"),
    ({"missing": 1}, "unknown public input"),
    ({"input": {"payload": 0, "valid": 2}}, "does not fit"),
])
def test_invalid_typed_events_fail_at_original_source_line(tmp_path, update, message):
    program = sim.compile(ROOT / "examples/cordic.zhl", top="Cordic", logic_state=True)
    path = tmp_path / "bad.jsonl"
    path.write_text('\n\n' + json.dumps({"set": update}) + '\n')
    with pytest.raises(ValueError, match=message) as error:
        prepare_debug_events(program, path)
    assert str(path) + ":3:" in str(error.value)


def test_lsp_events_are_separate_from_hardware_overlay():
    from zlang.lsp.server import LspServer, path_to_uri

    source = ROOT / "examples/cordic.zhl"
    server = LspServer()
    server.dispatch({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
        "textDocument": {"uri": path_to_uri(source), "version": 1, "text": source.read_text()},
    }})
    response = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "zlang/prepareDebugBundle", "params": {
        "textDocument": {"uri": path_to_uri(source)}, "top": "Cordic",
        "events": str(ROOT / "examples/cordic.events.jsonl"),
    }})[0]
    assert "error" not in response, response
    bundle = response["result"]
    assert bundle["prepared_events"]["schema"] == "zlang-native-debug-events-v1"
    assert bundle["prepared_events"]["plan_identity"] == bundle["plan_identity"]
    assert "prepared_events" not in build_native_debug_bundle(source, top="Cordic").to_data()


def test_debug_event_preparation_bounds_input_output_and_repeat(tmp_path, monkeypatch):
    import zlang.native_debug_events as preparation

    source = tmp_path / "top.zhl"
    source.write_text("module Top {in x:s80 out y:s80 y=x}")
    program = sim.compile(source, top="Top", logic_state=True)
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(preparation, "MAX_PREPARED_EVENTS_BYTES", 64)
    events.write_text(" " * 65)
    with pytest.raises(ValueError, match="event file exceeds"):
        prepare_debug_events(program, events)
    events.write_text('{"set":{"x":-1}}\n')
    with pytest.raises(ValueError, match="prepared debug events exceed"):
        prepare_debug_events(program, events)
    events.write_text('{"repeat":1000001}\n')
    with pytest.raises(sim.SimulationRuntimeError, match="exceeds 1000000 events"):
        prepare_debug_events(program, events)
