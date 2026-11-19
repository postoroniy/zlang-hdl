# SPDX-License-Identifier: Apache-2.0
"""Short union payloads must use the same MSB alignment as packing and SV."""

from pathlib import Path

import pytest

from zlang import sim
from zlang.ir.packing import pack_tagged_union_runtime
from zlang.ir.runtime_values import TaggedUnionValue
from tests.simulation.differential import run_differential

SOURCE = """
union Mixed { Full { value : s8 } Short { code : bits<4> } Empty }
module UnionPadding {
    in short : u1
    in data : s8
    in raw_code : bits<4>
    out forwarded : Mixed
    out selected : bits<8>
    message : Mixed = switch short {
        0 => Mixed.Full { value = data }
        else => Mixed.Short { code = raw_code }
    }
    forwarded = message
    selected = match message {
        Mixed.Full { value } => bitcast<bits<8>>(value)
        Mixed.Short { code } => extend<8>(code)
        Mixed.Empty => 0
    }
}
"""


@pytest.mark.parametrize("logic_state", [False, True])
def test_short_union_native_projection_and_raw_abi_agree(tmp_path: Path, logic_state: bool) -> None:
    path = tmp_path / "UnionPadding.zhl"
    path.write_text(SOURCE, encoding="utf-8")
    with sim.compile(path, logic_state=logic_state).create() as instance:
        instance.set("data", -7)
        for code in range(16):
            instance.set("short", 1)
            instance.set("raw_code", code)
            instance.eval()
            assert instance.get_packed("forwarded") == (1 << 8) | (code << 4)
            assert instance.get("selected") == code
        instance.set("short", 0)
        instance.eval()
        assert instance.get_packed("forwarded") == 249
        assert instance.get("selected") == 249
    _check_nominal_union_input(tmp_path, logic_state)


def _check_nominal_union_input(tmp_path: Path, logic_state: bool) -> None:
    path = tmp_path / "UnionAdmission.zhl"
    path.write_text("""
union Command { Idle Write { amount:u4 } }
union Fault { Okay Fatal { code:u4 } }
module UnionAdmission {
    in action:Command in fault:Fault out result:u4 out observed:u4
    result=match action {Command.Idle=>0 Command.Write{amount}=>amount}
    observed=match fault {Fault.Okay=>0 Fault.Fatal{code}=>code}
}
""")
    program = sim.compile(path, logic_state=logic_state)
    types = {port.name: port.type for port in program.module.inputs}
    assert types["action"].width == types["fault"].width
    good = TaggedUnionValue(types["action"], "Write", (("amount", 3),))
    wrong = TaggedUnionValue(types["fault"], "Fatal", (("code", 13),))
    with program.create() as instance:
        instance.run_events(({"set": {"action": good}},))
        assert instance.get("result") == 3
        with pytest.raises(sim.SimulationRuntimeError, match="Fault.*nominal type 'Command'"):
            instance.set("action", wrong)
        with pytest.raises(sim.SimulationRuntimeError, match="Fault.*nominal type 'Command'"):
            instance.run_events(({"set": {"action": wrong}},))
        assert instance.get_packed("action") == pack_tagged_union_runtime(good)
        instance.eval()
        assert instance.get("result") == 3
        instance.set_packed("action", pack_tagged_union_runtime(wrong))
        instance.eval()
        assert instance.get("result") == 13


def test_short_union_four_state_payload_preserves_ux_and_zero_padding(tmp_path: Path) -> None:
    path = tmp_path / "UnionPadding.zhl"
    path.write_text(SOURCE, encoding="utf-8")
    with sim.compile(path, logic_state=True).create() as instance:
        instance.set("short", 1)
        instance.set_logic("raw_code", "1ux0")
        instance.eval()
        assert instance.get_logic("forwarded").to_bits() == "011ux00000"
        assert instance.get_logic("selected").to_bits() == "00001ux0"


def test_short_union_native_matches_direct_sv(tmp_path: Path) -> None:
    path = tmp_path / "UnionPadding.zhl"
    # The comparator's public-value adapter does not reconstruct nominal union
    # objects. Test the scalar match result here; the raw union ABI is checked
    # independently against the packing oracle above.
    path.write_text(SOURCE.replace("out forwarded : Mixed", "").replace("forwarded = message", ""), encoding="utf-8")
    events = tuple({"set":{"short":1,"data":-7,"raw_code":code}} for code in range(16))
    trace = run_differential(path, top="UnionPadding", events=events, directory=tmp_path)
    assert trace.native == trace.direct_sv
    assert [event["selected"] for event in trace.native] == list(range(16))
