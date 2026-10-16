# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Event boundary adaptation must retain protocol history only when needed."""

from itertools import repeat
from types import SimpleNamespace

import pytest

from zlang import sim
from zlang.simulation_event_runner import SimulationEventRunner


def test_scalar_event_encoding_has_no_protocol_snapshots() -> None:
    runner = SimulationEventRunner(SimpleNamespace(_ports={}, _request_responses={}))
    encoded, snapshots = runner._encode_events(
        repeat({"edges": ("clk",)}, 10_000), {}, capture_protocol_values=False,
    )
    assert len(encoded) == 10_000
    assert encoded[0] == ([], [], ["clk"])
    assert snapshots == []


@pytest.mark.parametrize("protocol", (False, True))
def test_event_runner_retains_only_required_protocol_history(
    tmp_path, monkeypatch, protocol,
) -> None:
    source = tmp_path / "events.zhl"
    source.write_text(
        "module Events { in rx:rv<u8> out tx:rv<u8> "
        "rx.ready=tx.ready tx.valid=rx.valid tx.payload=rx.payload }"
        if protocol else "module Events { in x:u8 out y:u8=x }"
    )
    snapshots_seen = []
    original = SimulationEventRunner._encode_events

    def encode(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        snapshots_seen.append(result[1])
        return result

    monkeypatch.setattr(SimulationEventRunner, "_encode_events", encode)
    events = [
        {"set": {"rx": {"valid": 1, "payload": value}, "tx": {"ready": value & 1}}}
        if protocol else {"set": {"x": value}}
        for value in (1, 2, 3)
    ]
    with sim.load(source) as instance:
        actual = instance.run_events(iter(events))
    if protocol:
        assert actual == [
            {"rx": {"ready": value & 1, "transfer": value & 1},
             "tx": {"valid": 1, "payload": value, "transfer": value & 1}}
            for value in (1, 2, 3)
        ]
        snapshots, = snapshots_seen
        assert len(snapshots) == 3
        assert len({id(snapshot) for snapshot in snapshots}) == 3
    else:
        assert actual == [{"y": value} for value in (1, 2, 3)]
        assert snapshots_seen == [[]]
