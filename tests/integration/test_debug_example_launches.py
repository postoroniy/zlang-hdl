# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Editor/compiler launch contracts, not numerical IP qualification."""

import json
from pathlib import Path

import pytest

from tests.integration.test_native_debug_controls import _transcript
from zlang.native_debug_bundle import build_native_debug_bundle


ROOT = Path(__file__).resolve().parents[2]
CONFIGURATIONS = json.loads((ROOT / ".vscode/launch.json").read_text())["configurations"]


def _path(configuration, field):
    value = configuration.get(field)
    return None if value is None else Path(value.replace("${workspaceFolder}", str(ROOT)))


def test_launch_inventory_covers_every_public_example_schedule():
    examples = ROOT / "examples"
    # Published runnable stimuli live beside standalone/vendor examples or in
    # a project's simulation directory; generated build outputs are not input.
    paths = [*examples.glob("*.events.jsonl"),
             *(examples / "fpga_debug").glob("*.events.jsonl"),
             *(examples / "projects").rglob("simulation/*.jsonl")]
    supplied = {str(path.relative_to(ROOT)) for path in paths}
    selected = {str(_path(item, "events").relative_to(ROOT)) for item in CONFIGURATIONS}
    assert selected == supplied
    names = [item["name"] for item in CONFIGURATIONS]
    assert len(names) == len(set(names))
    roots = [(item["program"], item["top"]) for item in CONFIGURATIONS]
    assert len(roots) == len(set(roots))
    for item in CONFIGURATIONS:
        assert item["type"] == "zlang-hdl" and item["request"] == "launch"
        assert item["logicState"] and item["stopOnEntry"]
        assert _path(item, "program").is_file()
        assert _path(item, "events").is_file()


@pytest.mark.parametrize("configuration", CONFIGURATIONS, ids=lambda item: item["name"])
def test_each_example_launch_steps_and_restarts_with_reviewed_adapter(tmp_path, configuration):
    bundle = build_native_debug_bundle(
        _path(configuration, "program"), top=configuration["top"],
        events=_path(configuration, "events"), project=_path(configuration, "project"),
        profile=configuration.get("profile"),
    )
    traces = configuration["traceSignals"]
    # The real DAP resolves typed child projections as well as catalog roots.
    # A root-name-only subset check would incorrectly reject input.payload.
    plan, overlay, events = (tmp_path / name for name in ("plan.json", "overlay.json", "events.jsonl"))
    plan.write_bytes(bundle.plan_bytes)
    data = bundle.to_data()
    data.pop("prepared_events")
    overlay.write_text(json.dumps(data))
    events.write_text(bundle.prepared_events.jsonl)
    responses = _transcript([
        ("initialize", {}),
        ("launch", {"planPath": str(plan), "overlayPath": str(overlay),
                    "events": str(events), "traceSignals": traces, "stopOnEntry": True}),
        ("zlang/waveformStatus", {}),
        ("next", {"threadId": 1}), ("next", {"threadId": 1}),
        ("zlang/waveformStatus", {}),
        ("restart", {}), ("zlang/waveformStatus", {}),
        ("disconnect", {}),
    ], ROOT / "release/native-editor/linux-x64/zlang-debug-adapter")
    for response in responses.values():
        assert response["success"], response
    assert [responses[index]["body"]["event"] for index in (3, 6, 8)] == [0, 2, 0]
    signals = responses[3]["body"]["signals"]
    assert {"clk", "rst"} <= {signal["name"] for signal in signals}
    assert len(signals) > 2, "The example must start with useful data traces, not just clocks"
