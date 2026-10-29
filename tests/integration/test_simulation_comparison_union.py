# SPDX-License-Identifier: Apache-2.0
"""Comparison consumes native packed union outputs without guessing variants."""

import pytest

from zlang import sim
from zlang.simulation_compare import compare_program


@pytest.mark.parametrize("simulator", ("iverilog", "verilator"))
@pytest.mark.parametrize("rv", (False, True))
def test_native_rtl_comparison_keeps_tagged_union_output_bits(tmp_path, simulator, rv):
    path = tmp_path / "union.zhl"
    endpoint = "out packet:rv<Packet>" if rv else "out packet:Packet"
    target = "packet.payload" if rv else "packet"
    path.write_text(f"""union Packet {{Idle Data {{value:s8}}}}
module Top {{in raw:bits<9> {endpoint}
 {target}=raw[8] ? Packet.Data {{value=bitcast<s8>(raw[7:0])}} : Packet.Idle
 {'packet.valid=raw[0]' if rv else ''}}}
""")
    events = [{"set": {"raw": raw}} for raw in (0, 255, 256, 257, 383, 384, 511)]
    if rv:
        for i, event in enumerate(events):
            event["set"]["packet"] = {"ready": i % 2}
    result = compare_program(sim.compile(path, top="Top"), events, simulator=simulator,
                             artifact_directory=tmp_path / "comparison")
    assert result.native_trace == result.rtl_trace
    name = "packet_payload" if rv else "packet"
    assert [row[name] for row in result.rtl_trace] == [0, 0, 256, 257, 383, 384, 511]
