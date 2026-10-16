# SPDX-License-Identifier: Apache-2.0
"""Exercise the distributed DAP executable against the real RuleAction bundle."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess

import pytest

from zlang.native_debug_bundle import build_native_debug_bundle


ROOT = Path(__file__).resolve().parents[2]


def test_disconnect_drains_late_live_requests_until_transport_eof() -> None:
    executable = Path(os.environ.get("ZLANG_DEBUG_ADAPTER", str(ROOT / "release/native-editor/linux-x64/zlang-debug-adapter")))
    responses = _transcript([
        ("initialize", {}), ("disconnect", {}),
        ("zlang/waveformStatus", {}), ("disconnect", {}),
        ("setBreakpoints", {"breakpoints": []}),
        ("setBreakpoints", {"breakpoints": [{"line": 1}]}),
    ], executable)
    assert responses[1]["success"] and responses[2]["success"]
    assert responses[3]["success"] is False
    assert "disconnected" in responses[3]["message"]
    assert responses[4]["success"]
    assert responses[5]["success"] and responses[5]["body"] == {"breakpoints": []}
    assert responses[6]["success"] is False


def _transcript(requests: list[tuple[str, dict]], executable: Path) -> dict[int, dict]:
    frames = []
    for seq, (command, arguments) in enumerate(requests, 1):
        body = json.dumps({"seq": seq, "type": "request", "command": command, "arguments": arguments}).encode()
        frames.append(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    result = subprocess.run([str(executable)], input=b"".join(frames), capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode()
    stream = io.BytesIO(result.stdout)
    responses = {}
    while header := stream.readline():
        assert header.startswith(b"Content-Length: "), header
        assert stream.readline() == b"\r\n"
        message = json.loads(stream.read(int(header.split(b":", 1)[1])))
        if message["type"] == "response":
            responses[message["request_seq"]] = message
    assert len(responses) == len(requests)
    return responses


@pytest.mark.parametrize("line_offset", [0, 1])
def test_source_breakpoint_highlights_requested_line_until_execution_resumes(
    tmp_path: Path, line_offset: int,
) -> None:
    source = ROOT / "examples/rule_action.zhl"
    bundle = build_native_debug_bundle(source, top="RuleAction")
    site = next(item for item in bundle.sites if item.kind == "rule")
    line = site.start_line + line_offset
    plan = tmp_path / "plan.json"
    overlay = tmp_path / "overlay.json"
    plan.write_bytes(bundle.plan_bytes)
    overlay.write_text(json.dumps(bundle.to_data()), encoding="utf-8")
    requests = [
        ("initialize", {}),
        ("launch", {"planPath": str(plan), "overlayPath": str(overlay),
                    "events": str(ROOT / "examples/rule_action.events.jsonl")}),
        ("setBreakpoints", {"source": {"path": str(source)},
                            "breakpoints": [{"line": line}]}),
        ("next", {"threadId": 1}), ("next", {"threadId": 1}),
        ("stackTrace", {"threadId": 1}),  # reset/disabled events did not hit
        ("next", {"threadId": 1}),
        ("stackTrace", {"threadId": 1}),
        ("zlang/waveformStatus", {}),
        ("stackTrace", {"threadId": 1}),  # inspecting does not clear the stop
        ("setBreakpoints", {"source": {"path": str(source)}, "breakpoints": []}),
        ("stackTrace", {"threadId": 1}),  # breakpoint edits do not move the stop
        ("next", {"threadId": 1}),
        ("stackTrace", {"threadId": 1}),  # resuming clears the old source stop
        ("restart", {}), ("stackTrace", {"threadId": 1}),
    ]
    executable = Path(os.environ.get("ZLANG_DEBUG_ADAPTER", str(ROOT / "release/native-editor/linux-x64/zlang-debug-adapter")))
    responses = _transcript(requests, executable)
    assert all(item["success"] for item in responses.values())
    assert responses[3]["body"]["breakpoints"][0]["verified"]
    assert responses[9]["body"]["event"] == 3
    frame = responses[8]["body"]["stackFrames"][0]
    assert frame["source"]["path"] == str(source)
    assert frame["line"] == line
    assert frame["column"] >= 1
    assert responses[10]["body"]["stackFrames"][0] == frame
    assert responses[12]["body"]["stackFrames"][0] == frame
    for seq in (6, 14, 16):
        assert responses[seq]["body"]["stackFrames"][0].get("source") is None


def test_rule_action_live_controls_and_step_results(tmp_path: Path) -> None:
    bundle = build_native_debug_bundle(ROOT / "examples/rule_action.zhl", top="RuleAction")
    plan = tmp_path / "plan.json"
    overlay = tmp_path / "overlay.json"
    plan.write_bytes(bundle.plan_bytes)
    overlay.write_text(json.dumps(bundle.to_data()), encoding="utf-8")
    requests = []

    def call(dap_command: str, **arguments: object) -> int:
        requests.append((dap_command, arguments))
        return len(requests)

    call("initialize")
    call("launch", planPath=str(plan), overlayPath=str(overlay),
         events=str(ROOT / "examples/rule_action.events.jsonl"), traceSignals=["count"], stopOnEntry=True)
    catalog_id = call("zlang/signalCatalog")
    variables_id = call("variables", variablesReference=4)
    write_id = call("setVariable", variablesReference=1, name="rst", value="1")
    samples = []
    for event in range(6):
        if event:
            call("next", threadId=1)
        samples.append(call("zlang/waveformQuery", command="value_at", signals=["count", "clk", "rst"], timestamp=event*2))
    ticks = [call("zlang/waveformQuery", command="value_at", signals=["clk", "rst"], timestamp=tick) for tick in range(12)]
    rising = call("zlang/waveformQuery", command="search", query={
        "signals": ["clk"], "start": 0, "end": 11, "kind": "rising", "reverse": False, "limit": 100,
    })
    falling = call("zlang/waveformQuery", command="search", query={
        "signals": ["clk"], "start": 0, "end": 11, "kind": "falling", "reverse": False, "limit": 100,
    })
    call("restart")
    restarted = call("zlang/waveformQuery", command="value_at", signals=["count", "clk", "rst"], timestamp=0)
    call("disconnect")
    executable = Path(os.environ.get("ZLANG_DEBUG_ADAPTER", str(ROOT / "release/native-editor/linux-x64/zlang-debug-adapter")))
    responses = _transcript(requests, executable)
    for identity, response in responses.items():
        assert response["success"] is (identity != write_id), response
    traced = {signal["name"]: signal for signal in responses[catalog_id]["body"]["signals"]}
    assert (traced["clk"]["kind"], traced["rst"]["kind"]) == ("clock", "reset")
    assert traced["clk"]["traced"] and traced["rst"]["traced"]
    assert {item["name"] for item in responses[variables_id]["body"]["variables"]} == {"clk", "rst"}
    assert all("readOnly" in item["presentationHint"]["attributes"]
               for item in responses[variables_id]["body"]["variables"])
    for event, (identity, count) in enumerate(zip(samples, [0, 0, 0, 1, 2, 2], strict=True)):
        assert responses[identity]["body"]["values"] == {
            "count": format(count, "08b"), "clk": "1" if event else "0", "rst": "1" if event == 1 else "0",
        }
    for tick, identity in enumerate(ticks):
        assert responses[identity]["body"]["values"] == {
            "clk": "1" if tick > 0 and tick % 2 == 0 else "0", "rst": "1" if tick in (1, 2) else "0",
        }
    assert [item["timestamp"] for item in responses[rising]["body"]["matches"]] == [2, 4, 6, 8, 10]
    assert [item["timestamp"] for item in responses[falling]["body"]["matches"]] == [3, 5, 7, 9, 11]
    assert responses[restarted]["body"]["values"] == {"count": "00000000", "clk": "0", "rst": "0"}
