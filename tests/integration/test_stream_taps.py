# SPDX-License-Identifier: Apache-2.0
"""Private qualification of stateless, passive typed stream taps."""

import itertools

import pytest

from tests.simulation.differential import run_differential
from tests.support.stream_taps import PAYLOADS, input_value, source, values
from zlang import sim
from zlang.compiler import compile_source


@pytest.mark.parametrize("payload", PAYLOADS, ids=lambda p: p.name)
@pytest.mark.parametrize("rv", (False, True))
def test_native_binary_and_diagnostic_passthrough(tmp_path, payload, rv):
    path = tmp_path / "top.zhl"
    path.write_text(source(payload, rv=rv))
    with sim.compile(path, top="Top", logic_state=True).create() as machine:
        for raw, present, accept, reset in itertools.product(values(payload), range(2), range(2), range(2)):
            machine.set("raw", input_value(payload, raw))
            if rv:
                machine.set("present", present)
                machine.set("accept", accept)
            machine.reset("rst", asserted=bool(reset))
            machine.eval()
            expected = (0 if raw == 3 else raw) if payload.name == "enum" else (
                raw if payload.name != "union" or raw >> 8 else 0
            )
            for name in ("result", "tapped", "reference_payload"):
                assert machine.get_packed(name) == expected
            if rv:
                assert machine.get("upstream_ready") == accept
                assert machine.get("downstream_valid") == present
                assert machine.get("observed_ready") == accept
                assert machine.get("observed_valid") == present
                assert machine.get("observed_transfer") == present * accept
            machine.tick("clk")
        for tag in ("u", "x"):
            machine.set_logic("raw", tag * payload.width)
            if rv:
                machine.set_logic("present", tag)
                machine.set_logic("accept", tag)
            machine.eval()
            expected = machine.get_logic("reference_payload").to_bits()
            assert machine.get_logic("result").to_bits() == expected
            assert machine.get_logic("tapped").to_bits() == expected
            if rv:
                assert machine.get_logic("observed_transfer") == machine.get_logic("reference_transfer")


@pytest.mark.parametrize("payload", PAYLOADS, ids=lambda p: p.name)
@pytest.mark.parametrize("rv", (False, True))
def test_native_matches_emitted_direct_sv(tmp_path, payload, rv):
    path = tmp_path / "top.zhl"
    path.write_text(source(payload, rv=rv))
    events = []
    for i, (raw, present, accept) in enumerate(itertools.product(values(payload), range(2), range(2))):
        inputs = {"raw": input_value(payload, raw)}
        if rv:
            inputs.update(present=present, accept=accept)
        events.append({"set": inputs, "reset": {"rst": i % 7 == 0}, "edges": ["clk"]})
    trace = run_differential(path, top="Top", events=events, directory=tmp_path)
    assert trace.native == trace.direct_sv


@pytest.mark.parametrize("rv", (False, True))
@pytest.mark.parametrize("payload", PAYLOADS, ids=lambda p: p.name)
def test_tap_has_no_state_or_timing_resources(rv, payload):
    module = compile_source(source(payload, rv=rv), top="Top").ir
    child = module.children[0]
    assert not child.registers and not child.children
    assert not child.memories and not child.fifos
    assert not child.rules
    assert child.inputs[0].type == module.outputs[0].type
    tap = next(port for port in child.outputs if port.name == ("tap_payload" if rv else "tap"))
    assert tap.type == child.inputs[0].type
