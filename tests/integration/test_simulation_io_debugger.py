# SPDX-License-Identifier: Apache-2.0
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess

from zlang.native_debug_bundle import build_native_debug_bundle

ROOT = Path(__file__).resolve().parents[2]


def transcript(requests):
    frames = []
    for seq, (command, arguments) in enumerate(requests, 1):
        data = json.dumps(
            {"seq": seq, "type": "request", "command": command, "arguments": arguments}
        ).encode()
        frames.append(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
    executable = os.environ.get(
        "ZLANG_DEBUG_ADAPTER",
        str(ROOT / "release/native-editor/linux-x64/zlang-debug-adapter"),
    )
    result = subprocess.run(
        [executable], input=b"".join(frames), capture_output=True, timeout=30
    )
    assert result.returncode == 0, result.stderr.decode()
    messages = []
    stream = io.BytesIO(result.stdout)
    while header := stream.readline():
        assert header.startswith(b"Content-Length: "), header
        assert stream.readline() == b"\r\n"
        messages.append(json.loads(stream.read(int(header.split(b":")[1]))))
    return messages


def bundle_files(tmp_path, console=False):
    output = "-" if console else str(tmp_path / "results.jsonl")
    bundle = build_native_debug_bundle(
        ROOT / "examples/simulation_io.zhl",
        top="SimulationIoExample",
        events=ROOT / "examples/simulation_io.events.jsonl",
        simulation="vectors",
        io_inputs={"stimulus": ROOT / "examples/simulation_io.vectors.jsonl"},
        io_outputs={"results": output},
    )
    data = bundle.to_data()
    assert data["overlay_schema"] == "zlang-debug-overlay-v4"
    assert (
        hashlib.sha256(data["identity_payload"].encode()).hexdigest() == bundle.identity
    )
    plan = tmp_path / "plan.json"
    plan.write_bytes(bundle.plan_bytes)
    overlay = tmp_path / "overlay.json"
    overlay.write_text(json.dumps(data))
    events = tmp_path / "events.jsonl"
    events.write_text(bundle.prepared_events.to_data()["jsonl"])
    return data, {
        "planPath": str(plan),
        "overlayPath": str(overlay),
        "events": str(events),
    }


def test_real_adapter_step_restart_preserves_output_epochs(tmp_path):
    _, args = bundle_files(tmp_path)
    steps = [("next", {"threadId": 1})] * 12
    messages = transcript(
        [
            ("initialize", {}),
            ("launch", args),
            *steps,
            ("restart", {}),
            *steps,
            ("disconnect", {}),
        ]
    )
    assert all(m["success"] for m in messages if m["type"] == "response")
    records = [
        json.loads(s) for s in (tmp_path / "results.jsonl").read_text().splitlines()
    ]
    assert [r["epoch"] for r in records] == [0, 0, 0, 1, 1, 1]
    assert [r["event"] for r in records] == [3, 4, 8, 3, 4, 8]
    assert records[0]["values"]["sample"]["bits"] == "0100000000000000"
    metadata = json.loads((tmp_path / "results.jsonl.io.json").read_text())
    assert metadata["outputs"]["results"]["records"] == 6


def test_real_adapter_structured_console_and_plan_binding(tmp_path):
    _, args = bundle_files(tmp_path, console=True)
    messages = transcript(
        [("initialize", {}), ("launch", args), *[("next", {})] * 12, ("disconnect", {})]
    )
    logs = [
        json.loads(m["body"]["output"]) for m in messages if m.get("event") == "output"
    ]
    assert [r["event"] for r in logs] == [3, 4, 8]


def test_changed_recipe_even_with_updated_inner_digest_is_rejected(tmp_path):
    data, args = bundle_files(tmp_path)
    encoded = data["simulation_io"]
    spec = json.loads(encoded["json"])
    spec["clock"] = "wrong"
    encoded["json"] = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    encoded["sha256"] = hashlib.sha256(encoded["json"].encode()).hexdigest()
    Path(args["overlayPath"]).write_text(json.dumps(data))
    messages = transcript([("initialize", {}), ("launch", args), ("disconnect", {})])
    response = next(m for m in messages if m.get("request_seq") == 2)
    assert response["success"] is False
    assert "binding differs" in response["message"]
    assert not (tmp_path / "results.jsonl").exists()


def test_real_adapter_multiple_read_sites_and_eof_logs(tmp_path):
    path = tmp_path / "source.zhl"
    path.write_text("""module T {clock clk reset rst in a:u8 out y:u8 y=a
reg select:bit=0 when 1 {select <- ~select}
simulation io @clk {
when select {read src {v->a} else {drive a=0 write dst {v=y}}}
when ~select {read src {v->a} else {drive a=0 write dst {v=y}}}
write dst {v=y}}}""")
    vector = tmp_path / "input.jsonl"
    vector.write_text('{"v":7}\n{"v":9}\n')
    bundle = build_native_debug_bundle(
        path, simulation="io", io_inputs={"src": vector}, io_outputs={"dst": "-"}
    )
    plan = tmp_path / "plan.json"
    plan.write_bytes(bundle.plan_bytes)
    overlay = tmp_path / "overlay.json"
    overlay.write_text(json.dumps(bundle.to_data()))
    args = {
        "planPath": str(plan),
        "overlayPath": str(overlay),
        "clock": "clk",
        "cycles": 3,
    }
    messages = transcript(
        [
            ("initialize", {}),
            ("launch", args),
            ("next", {}),
            ("next", {}),
            ("next", {}),
            ("disconnect", {}),
        ]
    )
    assert all(m["success"] for m in messages if m["type"] == "response"), messages
    logs = [
        json.loads(m["body"]["output"]) for m in messages if m.get("event") == "output"
    ]
    assert [int(row["values"]["v"]["bits"], 2) for row in logs] == [7, 9, 0, 0]
