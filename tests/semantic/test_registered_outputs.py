from __future__ import annotations

import pytest

from zlang.compiler import compile_source
from zlang.backend.systemverilog import emit_artifact
from zlang.ir import PortDirection, RegisterRef
from zlang.native_simulation import simulate_cycles
from zlang.opt import lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


REGISTERED = """
module RegisteredOutput {
    clock clk
    reset rst
    in load:bit
    in value:s8
    out reg held:s8 = -1
    when load { held <- value }
}
"""


def test_registered_output_is_one_public_port_and_one_state_resource() -> None:
    module = compile_source(REGISTERED).ir
    port = module.outputs[0]
    assert port.direction is PortDirection.OUTPUT
    assert port.registered
    assert module.registers[0].name == port.name == "held"
    assert module.registers[0].type == port.type
    assert module.registers[0].domain == port.domain == "clk"
    assert isinstance(module.assignments[0].expression, RegisterRef)
    assert module.assignments[0].expression.name == "held"
    assert restore(lower(module)) == module


def test_registered_output_resets_captures_and_holds() -> None:
    trace = simulate_cycles(
        compile_source(REGISTERED).ir,
        [
            {"load": 0, "value": 4},
            {"load": 1, "value": 7},
            {"load": 0, "value": -8},
        ],
        reset=[True, False, False],
    )
    # Native traces expose the pre-edge state for each input cycle.  The
    # accepted update becomes observable on the following cycle.
    assert [cycle["held"] for cycle in trace] == [-1, -1, 7]


@pytest.mark.parametrize(
    "source,output_name",
    (
        (
            "module V { clock c reset r in load:bit in value:vec<2,u8> "
            "out reg held:vec<2,u8>=[1,2] when load { held <- value } }",
            "held",
        ),
        (
            "struct Pair { left:u8 right:u8 } module S { clock c reset r "
            "in load:bit in value:Pair out reg held:Pair=Pair{left=1 right=2} "
            "when load { held <- value } }",
            "held",
        ),
        (
            "union Value { Empty Data { value:s8 } } module U { "
            "clock c reset r in load:bit in value:s8 "
            "out reg held:Value=Value.Empty "
            "when load { held <- Value.Data{value=value} } }",
            "held",
        ),
        (
            "module D { clock a reset ar @a clock b reset br @b "
            "in value:u8 @b out reg held:u8 @b=0 "
            "rule Capture @b when 1 { held <- value } }",
            "held",
        ),
    ),
)
def test_registered_outputs_accept_register_storable_types_and_domains(
    source: str, output_name: str
) -> None:
    module = compile_source(source).ir
    output = next(port for port in module.outputs if port.name == output_name)
    register = next(item for item in module.registers if item.name == output_name)
    assert output.registered
    assert output.type == register.type
    assert output.domain == register.domain


def test_registered_output_without_initializer_has_no_reset_assignment() -> None:
    module = compile_source(
        "module NoReset { clock clk reset rst in load:bit in value:u8 "
        "out reg held:u8 when load { held <- value } }"
    ).ir
    assert module.registers[0].initial is None
    rtl = emit_artifact(module).text
    assert "always_ff @(posedge clk)" in rtl
    assert "if (rst)" not in rtl
    assert "held <= value;" in rtl
    assert "assign held = held;" not in rtl


def test_fsm_transition_updates_registered_output() -> None:
    module = compile_source("""
    enum Phase { Idle Active }
    module FsmRegisteredOutput {
        clock clk reset rst in start:bit
        out reg remaining:u8=0
        fsm phase:Phase=Idle {
            Idle { when start -> Active { remaining <- 7 } }
            Active { -> Idle {} }
        }
    }
    """).ir
    trace = simulate_cycles(
        module,
        ({"start": 0}, {"start": 1}, {"start": 0}, {"start": 0}),
        reset=(True, False, False, False),
    )
    assert [cycle["remaining"] for cycle in trace] == [0, 0, 7, 7]


def test_drive_retains_transient_zero_default() -> None:
    source = (
        "module Pulse { clock clk reset rst in fire:bit out pulse:bit "
        "when fire { drive pulse = 1 } }"
    )
    trace = simulate_cycles(
        compile_source(source).ir,
        [{"fire": 0}, {"fire": 1}, {"fire": 0}],
        reset=[False, False, False],
    )
    assert [cycle["pulse"] for cycle in trace] == [0, 1, 0]


@pytest.mark.parametrize(
    "source,message",
    (
        (
            "module M { clock c reset r out x:u8 when 1 { x <- 1 } }",
            "output wire 'x' is not stored.*out reg.*drive x",
        ),
        (
            "module M { clock c reset r reg x:u8=0 out y:u8 "
            "when 1 { drive x = 1 } y=x }",
            "drive target 'x' is stored state",
        ),
        (
            "module M { clock c reset r out reg x:u8=0 "
            "when 1 { drive x = 1 } }",
            "registered output 'x' is stored state",
        ),
        (
            "module M { clock c reset r out reg x:u8=0 x=1 }",
            "cannot also have a combinational assignment",
        ),
        (
            "module M { clock c reset r in reg x:u8=0 }",
            "registered port 'x' must be an output",
        ),
        (
            "module M { clock c reset r out reg a,b:u8=0 }",
            "registered output declarations must contain exactly one name",
        ),
        (
            "module M { clock c reset r out reg stream:rv<u8> }",
            "protocol port 'stream' cannot be a registered output",
        ),
    ),
)
def test_registered_output_and_drive_invalid_combinations_fail(
    source: str, message: str
) -> None:
    with pytest.raises(SemanticError, match=message):
        analyze(parse(source))


def test_public_aggregate_leaf_collision_is_rejected_with_both_paths() -> None:
    source = """
    struct Head { valid:bit }
    struct Queue { head:Head head_valid:bit }
    module Collision {
        in queue:Queue
        out observed:bit
        observed = queue.head.valid
    }
    """
    with pytest.raises(SemanticError) as raised:
        analyze(parse(source))
    error = raised.value
    assert error.code == "ZL-SEMANTIC-PUBLIC-ABI-COLLISION"
    assert "queue.head.valid" in str(error)
    assert "queue.head_valid" in str(error)
    assert error.primary is not None
    assert any("first declaration/assignment span" in note for note in error.notes)
