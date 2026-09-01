from __future__ import annotations

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.ir.expressions import Constant, Switch
from zlang.ir.types import BitType, EnumType, StructType, VecType
from zlang.opt import OptimizationStage, lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.simulate import simulate, simulate_cycles


ENUM_FSM_SOURCE = """
    enum Phase { Idle Active Done }
    module EnumFsm {
        clock clk reset rst
        in start:bit in finish:bit out phase:Phase
        reg state:Phase=Phase.Idle
        when 1 {
            state <- switch state {
                Phase.Idle=>start ? Phase.Active : Phase.Idle
                Phase.Active=>finish ? Phase.Done : Phase.Active
                Phase.Done=>start ? Phase.Active : Phase.Idle
            }
        }
        phase=state
    }
"""


def _compile(source: str):
    return compile_source(source, include_clash=False).ir


def test_enum_ordinals_width_and_qualified_members_are_exact() -> None:
    module = _compile(
        "enum Phase { Idle Header Payload Done Error } "
        "module M { out idle:Phase out payload:Phase out error:Phase "
        "idle=Phase.Idle payload=Phase.Payload error=Phase.Error }"
    )
    (enum_type,) = module.enums
    assert isinstance(enum_type, EnumType)
    assert enum_type.members == ("Idle", "Header", "Payload", "Done", "Error")
    assert enum_type.width == 3
    assert [assignment.expression for assignment in module.assignments] == [
        Constant(0, enum_type),
        Constant(2, enum_type),
        Constant(4, enum_type),
    ]
    assert simulate(module) == {"idle": 0, "payload": 2, "error": 4}


@pytest.mark.parametrize(
    "members,width",
    (("Only", 1), ("A B", 1), ("A B C", 2), ("A B C D", 2), ("A B C D E", 3)),
)
def test_enum_encoding_width_is_minimal_and_never_zero(
    members: str, width: int
) -> None:
    module = _compile(
        f"enum E {{ {members} }} module M {{ out y:E y=E.{members.split()[0]} }}"
    )
    assert module.enums[0].width == width


def test_enum_switch_is_exhaustive_and_lowers_to_ordinal_switch() -> None:
    module = _compile(
        "enum Phase { Idle Active Done } module M { out y:u2 "
        "y=switch Phase.Active { "
        "Phase.Idle=>0 Phase.Active=>1 Phase.Done=>2 } }"
    )
    expression = module.assignments[0].expression
    assert isinstance(expression, (Constant, Switch))
    assert simulate(module) == {"y": 1}


def test_enum_equality_and_inequality_are_nominal() -> None:
    module = _compile(
        "enum Phase { Idle Active } module M { out same:bit out different:bit "
        "same=Phase.Active==Phase.Active different=Phase.Idle!=Phase.Active }"
    )
    assert all(assignment.expression.type == BitType() for assignment in module.assignments)
    assert simulate(module) == {"same": 1, "different": 1}


def test_enum_can_be_used_in_struct_vector_local_register_rule_and_output() -> None:
    source = """
        enum Phase { Idle Active Done }
        struct Snapshot { phase:Phase history:vec<2,Phase> }
        module M {
            clock clk reset rst
            in step:bit out phase:Phase out active:bit out snapshot:Snapshot
            reg state:Phase=Phase.Idle
            local:Phase = state
            when step {
                state <- switch state {
                    Phase.Idle=>Phase.Active
                    Phase.Active=>Phase.Done
                    Phase.Done=>Phase.Idle
                }
            }
            phase=local
            active=state==Phase.Active
            snapshot=Snapshot{phase=state history=generate(i in 0..2) state}
        }
    """
    module = _compile(source)
    snapshot = next(port.type for port in module.ports if port.name == "snapshot")
    assert isinstance(snapshot, StructType)
    history = next(field.type for field in snapshot.fields if field.name == "history")
    assert isinstance(history, VecType)
    assert isinstance(history.element_type, EnumType)
    outputs = simulate_cycles(
        module,
        [
            {"step": 0},
            {"step": 1},
            {"step": 1},
            {"step": 1},
        ],
        reset=[True, False, False, False],
    )
    assert [cycle["phase"] for cycle in outputs] == [0, 0, 1, 2]
    assert [cycle["active"] for cycle in outputs] == [0, 0, 1, 0]


def test_enum_survives_canonical_and_backend_artifact_round_trips() -> None:
    module = _compile(ENUM_FSM_SOURCE)
    restored = restore(lower(module, stage=OptimizationStage.HIGH_LEVEL))
    assert restored == module
    assert restored.enums == module.enums
    artifact = emit_artifact(module)
    round_trip = BackendArtifact.from_json(artifact.to_json())
    phase = next(
        binding for binding in artifact.bindings
        if binding.semantic_signal_id == "port:phase"
    )
    restored_phase = next(
        binding for binding in round_trip.bindings if binding.semantic_signal_id == phase.semantic_signal_id
    )
    assert phase.canonical_type == str(module.enums[0])
    assert phase.width == module.enums[0].width
    assert restored_phase == phase


@pytest.mark.parametrize(
    "source,message",
    (
        (
            "enum E { A A } module M { out y:E y=E.A }",
            "duplicate member 'A' in enum 'E'",
        ),
        (
            "enum E { A B } module M { out y:E y=E.C }",
            "enum 'E' has no member 'C'",
        ),
        (
            "enum E { A B C } module M { out y:u2 y=switch E.A { E.A=>0 E.B=>1 } }",
            "missing enum member",
        ),
        (
            "enum E { A B } module M { out y:u2 y=switch E.A { E.A=>0 E.A=>1 E.B=>2 } }",
            "duplicate enum switch member",
        ),
        (
            "enum E { A B } module M { out y:u2 y=switch E.A { 0=>0 1=>1 } }",
            "enum switch key",
        ),
        (
            "enum E { A B } enum F { A B } module M { out y:bit y=E.A==F.A }",
            "matching nominal enum",
        ),
        (
            "enum E { A B } module M { out y:E y=E.A+E.B }",
            "arithmetic is not defined for enum",
        ),
        (
            "enum E { A B } module M { out y:bit y=E.A<E.B }",
            "ordered comparison is not defined for enum",
        ),
        (
            "enum E { A B } module M { out y:u2 y=extend<2>(E.A) }",
            "cannot resize enum",
        ),
        (
            "enum E { A B } module M { in x:E out y:E y=x }",
            "top-level .*input 'x' cannot expose .*type",
        ),
    ),
)
def test_invalid_enum_forms_fail_closed(source: str, message: str) -> None:
    with pytest.raises(SemanticError, match=message):
        _compile(source)


def test_numeric_switch_remains_compatible_and_requires_else() -> None:
    module = analyze(parse("module M { out y:u2 y=switch 1 { 0=>0 else=>2 } }"))
    assert simulate(module) == {"y": 2}
    with pytest.raises(SemanticError, match="numeric switch requires an else"):
        _compile("module M { out y:u2 y=switch 1 { 0=>0 1=>1 } }")


@pytest.mark.parametrize(
    "source,message",
    (
        (
            "enum State { Idle } type State=u1 module M { out y:u1 y=0 }",
            "type name 'State' is already an alias",
        ),
        (
            "enum State { Idle } struct State { x:u1 } module M { out y:u1 y=0 }",
            "type name 'State' is already a struct",
        ),
    ),
)
def test_enum_shares_the_nominal_type_namespace(
    source: str, message: str
) -> None:
    with pytest.raises(SemanticError, match=message):
        _compile(source)


def test_numeric_literal_cannot_initialize_an_enum() -> None:
    with pytest.raises(
        SemanticError,
        match="numeric literal 0 cannot initialize enum 'E'; use a qualified member",
    ):
        _compile("enum E { A B } module M { out y:E y=0 }")


def test_enum_switch_rejects_wrong_enum_label_and_else_arm() -> None:
    with pytest.raises(SemanticError, match="switch label F.B has enum type"):
        _compile(
            "enum E { A B } enum F { A B } module M { out y:u1 "
            "y=switch E.A { E.A=>0 F.B=>1 } }"
        )
    with pytest.raises(
        SemanticError,
        match="exhaustive enum switch on 'E' must not contain an else arm",
    ):
        _compile(
            "enum E { A B } module M { out y:u1 "
            "y=switch E.A { E.A=>0 E.B=>1 else=>0 } }"
        )


@pytest.mark.parametrize(
    "expression,message",
    (
        ("E.A | E.B", "arithmetic is not defined for enum values"),
        ("E.A << 1", "arithmetic is not defined for enum values"),
        ("-E.A", "unary minus is not defined for enum"),
    ),
)
def test_enum_rejects_bitwise_shift_and_unary_arithmetic(
    expression: str, message: str
) -> None:
    with pytest.raises(SemanticError, match=message):
        _compile(f"enum E {{ A B }} module M {{ out y:E y={expression} }}")


def test_child_enum_input_uses_the_parent_nominal_identity() -> None:
    module = _compile(
        "enum E { A B } "
        "module Child { in x:E out y:bit y=x==E.B } "
        "module Top { out y:bit inst c:Child c.x=E.B y=c.y }"
    )
    enum_type = module.enums[0]
    binding = module.instance_bindings[0]
    child_type = module.children[0].inputs[0].type
    assert binding.expression == Constant(1, enum_type)
    assert child_type == enum_type
    restored = restore(lower(module, stage=OptimizationStage.HIGH_LEVEL))
    assert restored.instance_bindings[0].expression.type == enum_type
    assert restored.children[0].inputs[0].type == enum_type


def test_source_authored_apb_bridge_uses_nominal_phase_without_abi_change() -> None:
    module = _compile(
        "import std.bus.apb "
        "module ApbCsrTop { "
        "clock clk reset rst interface apb:APB<32,32>.slave "
        "inst frontend:APBToRegBus<32,32> "
        "inst csr:RegBusCSRTarget<32,32> "
        "connect apb -> frontend.apb "
        "connect frontend.regbus -> csr.regbus "
        "out done:bit done=csr.done }"
    )
    bridge = next(child for child in module.children if child.name == "APBToRegBus")
    phase = next(register for register in bridge.registers if register.name == "phase")
    assert isinstance(phase.type, EnumType)
    assert phase.type.name == "APBBridgePhase"
    assert phase.type.members == ("Idle", "Access", "Response")
    assert phase.type.width == 2
    assert all(port.type != phase.type for port in module.ports)
