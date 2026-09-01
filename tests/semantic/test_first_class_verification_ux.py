from __future__ import annotations

from dataclasses import replace
import hashlib

import pytest

from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.systemverilog import emit_artifact as emit_systemverilog_artifact
from zlang.backend.systemverilog import emit_experimental as emit_systemverilog
from zlang.compiler import compile_source
from zlang.ir.expressions import Binary, RegisterRef
from zlang.ir.formal import PropertyKind
from zlang.ir import verification as ir_verification
from zlang.ir.verification import ContractKind, VerificationGoalKind
from zlang.module_resolver import attach_source_identity
from zlang.opt import CanonicalizationError, lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.simulate import VerificationAssertionError, simulate_cycles


def _compile(source: str):
    return compile_source(source, include_clash=False)


def test_empty_verification_overlay_does_not_hash_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(_module: object) -> str:
        raise AssertionError(
            "verification-free modules must not compute verification identity"
        )

    monkeypatch.setattr(
        ir_verification, "verification_module_identity", unexpected
    )
    module = analyze(parse("module Plain { in a:u8 out y:u8 y=a }"))
    assert module.verification_scopes == ()


def test_nonempty_verification_overlay_hashes_once_and_preserves_exact_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ir_verification.verification_module_identity
    hashed_modules: list[object] = []

    def counted(module: object) -> str:
        hashed_modules.append(module)
        return original(module)

    monkeypatch.setattr(
        ir_verification, "verification_module_identity", counted
    )
    module = analyze(parse(
        "module Checked { clock clk reset rst in a:bit out y:bit y=a "
        "assert matches { y == a } cover observed { y } }"
    ))

    assert len(hashed_modules) == 1
    namespace = original(hashed_modules[0])
    (scope,) = module.verification_scopes
    expected_scope = hashlib.sha256(
        repr((namespace, "scope", "$module", "clk")).encode("utf-8")
    ).hexdigest()
    assert scope.semantic_id == expected_scope
    assert tuple(goal.semantic_id for goal in scope.goals) == tuple(
        hashlib.sha256(
            repr((
                namespace,
                "scope",
                "$module",
                "clk",
                goal.kind.value,
                goal.name,
            )).encode("utf-8")
        ).hexdigest()
        for goal in scope.goals
    )


def test_single_domain_clock_is_inferred_for_module_goals_and_contracts() -> None:
    result = _compile(
        """
        module Counter {
            clock clk
            reset rst
            in enable : bit
            out value : u8
            reg count : u8 = 0
            count <- mux(enable, truncate<8>(count + 1), count)
            value = count

            assert state_matches_output { count == value }
            cover reaches_one { count == 1 }
            contract legal_enable {
                require input_is_bit { (enable == 0) | (enable == 1) }
                ensure output_is_visible { value == value }
            }
        }
        """
    )

    assert [(scope.name, scope.clock, scope.reset) for scope in result.ir.verification_scopes] == [
        ("$module", "clk", "rst"),
        ("legal_enable", "clk", "rst"),
    ]
    assert [goal.kind for goal in result.ir.verification_scopes[0].goals] == [
        VerificationGoalKind.ASSERT,
        VerificationGoalKind.COVER,
    ]


@pytest.mark.parametrize(
    ("source", "message"),
    (
        (
            "module NoDomain { in a:bit out y:bit y=a assert ok { y == a } }",
            "clock inference requires exactly one clock/reset domain",
        ),
        (
            "module Multi { clock a reset ra @a clock b reset rb @b "
            "in x:bit @a out y:bit @a y=x assert ok { y == x } }",
            "clock inference requires exactly one clock/reset domain",
        ),
        (
            "module Unknown { clock c reset r in a:bit out y:bit y=a "
            "cover seen @ missing { y } }",
            "unknown clock 'missing'",
        ),
    ),
)
def test_verification_clock_inference_fails_closed(
    source: str, message: str
) -> None:
    with pytest.raises(SemanticError, match=message):
        _compile(source)


def test_explicit_clock_selects_one_domain_and_rejects_cross_domain_reads() -> None:
    accepted = _compile(
        "module Multi { clock a reset ra @a clock b reset rb @b "
        "in x:bit @a out y:bit @a y=x assert ok @ a { y == x } }"
    )
    assert accepted.ir.verification_scopes[0].clock == "a"
    assert accepted.ir.verification_scopes[0].reset == "ra"

    with pytest.raises(SemanticError, match="references signal in domain 'b'"):
        _compile(
            "module Multi { clock a reset ra @a clock b reset rb @b "
            "in x:bit @b out y:bit @a y=0 assert bad @ a { x == 0 } }"
        )


@pytest.mark.parametrize(
    ("source", "message"),
    (
        (
            "module Duplicate { clock c reset r in a:bit out y:bit y=a "
            "assert same { y } cover same { y } }",
            "duplicate verification clause 'same' in scope '\\$module'",
        ),
        (
            "module Duplicate { clock c reset r in a:bit out y:bit y=a "
            "contract s { require same { a } assert same { y } } }",
            "duplicate verification clause 'same' in scope 's'",
        ),
        (
            "module Duplicate { clock c reset r in a:bit out y:bit y=a "
            "contract s { assert one { y } } contract s { cover two { y } } }",
            "duplicate verification contract 's'",
        ),
    ),
)
def test_verification_names_are_unique_in_their_declared_scope(
    source: str, message: str
) -> None:
    with pytest.raises(SemanticError, match=message):
        _compile(source)


def test_verification_words_remain_contextual_identifiers_semantically() -> None:
    result = _compile(
        "module Contextual { "
        "in assert, cover, contract, require, ensure : bit "
        "out y : bit "
        "y = assert & cover | contract & require | ensure "
        "}"
    )

    assert [port.name for port in result.ir.ports] == [
        "assert",
        "cover",
        "contract",
        "require",
        "ensure",
        "y",
    ]
    assert result.ir.verification_scopes == ()


def test_legacy_assume_guarantee_normalize_into_module_scope() -> None:
    result = _compile(
        "module Legacy { clock clk reset rst in legal:bit out y:bit y=legal "
        "assume legal_input @ clk disable iff rst { legal } "
        "guarantee output_matches @ clk disable iff rst { y == legal } }"
    )

    assert [contract.kind for contract in result.ir.contracts] == [
        ContractKind.ASSUME,
        ContractKind.GUARANTEE,
    ]
    (scope,) = result.ir.verification_scopes
    assert (scope.name, scope.clock, scope.reset) == ("$module", "clk", "rst")
    assert [requirement.name for requirement in scope.requirements] == ["legal_input"]
    assert [(goal.kind, goal.name) for goal in scope.goals] == [
        (VerificationGoalKind.ASSERT, "output_matches")
    ]
    generated = {
        item.generated_from: item.kind for item in result.formal_design.properties
    }
    assert generated["contract:legal_input"] is PropertyKind.ASSUMPTION
    assert generated["contract:output_matches"] is PropertyKind.ASSERTION


def test_contract_requirements_are_environment_owned_and_scope_local() -> None:
    result = _compile(
        "module Scoped { clock clk reset rst in legal:bit in a:bit out y:bit y=a "
        "assert global_ok { y == a } "
        "contract gated { "
        "require legal_input { legal } "
        "assert gated_ok { y == a } "
        "cover gated_cover { y } "
        "} }"
    )

    predicates = {
        item.generated_from: item.predicate.render()
        for item in result.formal_design.properties
        if item.predicate is not None
    }
    assert predicates["verification-assert:$module:global_ok"] == "(port:y == port:a)"
    assert predicates["verification-assert:gated:gated_ok"] == (
        "(port:legal -> (port:y == port:a))"
    )
    covers = {
        item.generated_from: item.predicate.render()
        for item in result.formal_design.covers
    }
    assert covers["verification-feasibility:gated"] == "port:legal"
    assert covers["verification-cover:gated:gated_cover"] == "(port:legal && port:y)"

    with pytest.raises(SemanticError, match="implementation-owned"):
        _compile(
            "module Bad { clock clk reset rst in a:bit out y:bit y=a "
            "contract c { require constrains_dut { y } assert g { y == a } } }"
        )

    with pytest.raises(
        SemanticError,
        match="must contain at least one assert, ensure, or cover goal",
    ):
        _compile(
            "module NoGoal { clock clk reset rst in legal:bit out y:bit y=legal "
            "contract assumptions_only { require legal_input { legal } } }"
        )


def test_internal_assert_and_enum_cover_retain_typed_state_observations() -> None:
    result = _compile(
        "enum State { Idle Done } "
        "module Machine { clock clk reset rst in go:bit out done:bit "
        "reg state:State=State.Idle "
        "state <- mux(go, State.Done, state) "
        "done = state == State.Done "
        "assert state_is_legal { (state == State.Idle) | (state == State.Done) } "
        "cover reaches_done { state == State.Done } }"
    )

    (scope,) = result.ir.verification_scopes
    assertion, cover = scope.goals
    assert assertion.kind is VerificationGoalKind.ASSERT
    assert cover.kind is VerificationGoalKind.COVER
    assert isinstance(assertion.expression, Binary)
    assert isinstance(cover.expression, Binary)
    assert isinstance(cover.expression.left, RegisterRef)
    assert any(
        item.generated_from == "verification-cover:$module:reaches_done"
        for item in result.formal_design.covers
    )


def test_ensure_must_observe_only_public_implementation_outputs() -> None:
    accepted = _compile(
        "module Good { clock c reset r in a:bit out y:bit y=a "
        "contract io { ensure matches { y == a } } }"
    )
    assert accepted.ir.verification_scopes[0].goals[0].kind is VerificationGoalKind.ENSURE

    rejected = (
        (
            "module Bad { clock c reset r in a:bit out y:bit y=a "
            "contract io { ensure input_only { a } } }",
            "must observe at least one implementation-owned public output",
        ),
        (
            "module Bad { clock c reset r in a:bit out y:bit reg q:bit=0 "
            "q<-a y=q contract io { ensure hidden { y == q } } }",
            "ensure cannot depend on hidden register 'q'",
        ),
    )
    for source, message in rejected:
        with pytest.raises(SemanticError, match=message):
            _compile(source)


def test_verification_overlay_round_trips_without_changing_hardware_identity() -> None:
    plain = _compile(
        "module Same { clock clk reset rst in a:bit out y:bit y=a }"
    )
    verified = _compile(
        "module Same { clock clk reset rst in a:bit out y:bit y=a "
        "assert output_matches { y == a } cover output_high { y } }"
    )

    assert verified.high_level_ir_identity == plain.high_level_ir_identity
    assert verified.selected_ir_identity == plain.selected_ir_identity
    canonical = lower(verified.ir)
    assert canonical.verification_scopes
    assert canonical.verification_expressions
    assert restore(canonical) == verified.ir


def test_verification_overlay_does_not_change_real_clash_artifact() -> None:
    plain = compile_source(
        "module SameClash { clock clk reset rst in a:bit out y:bit y=a }"
    )
    verified = compile_source(
        "module SameClash { clock clk reset rst in a:bit out y:bit y=a "
        "assert output_matches { y == a } cover output_high { y } }"
    )

    assert verified.clash == plain.clash
    plain_artifact = emit_clash_artifact(
        plain.ir,
        selected_ir_identity=plain.selected_ir_identity,
    )
    verified_artifact = emit_clash_artifact(
        verified.ir,
        selected_ir_identity=verified.selected_ir_identity,
    )
    assert verified_artifact.text == plain_artifact.text
    assert verified_artifact.artifact_hash == plain_artifact.artifact_hash


def test_verification_only_generic_specialization_does_not_change_hardware() -> None:
    prefix = "fn same<type T>(x:T)->bit { x == x } "
    plain = _compile(
        prefix + "module Same { clock clk reset rst in a:u8 out y:u8 y=a }"
    )
    verified = _compile(
        prefix
        + "module Same { clock clk reset rst in a:u8 out y:u8 y=a "
        "assert reflexive { same(a) } }"
    )

    assert plain.ir.callable_definitions == ()
    assert verified.ir.callable_definitions == plain.ir.callable_definitions
    assert verified.high_level_ir_identity == plain.high_level_ir_identity
    assert verified.selected_ir_identity == plain.selected_ir_identity
    assert verified.clash == plain.clash
    assert emit_systemverilog(verified.ir) == emit_systemverilog(plain.ir)
    assert (
        emit_systemverilog_artifact(
            verified.ir,
            selected_ir_identity=verified.selected_ir_identity,
        ).artifact_hash
        == emit_systemverilog_artifact(
            plain.ir,
            selected_ir_identity=plain.selected_ir_identity,
        ).artifact_hash
    )


def test_goal_identity_is_stable_across_unrelated_verification_source_changes() -> None:
    source = (
        "module V { clock c reset r in a:bit out y:bit y=a "
        "assert stable { y == a } }"
    )
    extended = source[:-1] + " cover unrelated { y } }"

    def analyzed(text: str):
        return analyze(attach_source_identity(
            parse(text),
            "example.verification",
            hashlib.sha256(text.encode()).hexdigest(),
        ))

    first = analyzed(source)
    second = analyzed(extended)
    assert first.source_hash != second.source_hash
    assert (
        first.verification_scopes[0].goals[0].semantic_id
        == second.verification_scopes[0].goals[0].semantic_id
    )


def test_goal_identity_changes_with_implementation_semantics() -> None:
    direct = _compile(
        "module Same { clock c reset r in a:bit out y:bit y=a "
        "assert matches { y == a } }"
    )
    inverted = _compile(
        "module Same { clock c reset r in a:bit out y:bit y=~a "
        "assert matches { y == a } }"
    )

    assert direct.selected_ir_identity != inverted.selected_ir_identity
    assert (
        direct.ir.verification_scopes[0].goals[0].semantic_id
        != inverted.ir.verification_scopes[0].goals[0].semantic_id
    )


def test_malformed_canonical_verification_links_fail_closed() -> None:
    result = _compile(
        "module V { clock clk reset rst in a:bit out y:bit y=a "
        "contract c { require legal { a } assert ok { y == a } } }"
    )
    canonical = lower(result.ir)
    (scope,) = canonical.verification_scopes
    (goal,) = scope.goals

    with pytest.raises(ValueError, match="references the wrong scope"):
        replace(scope, goals=(replace(goal, scope_id="missing-scope"),))

    missing_expression = replace(
        goal,
        expression=len(canonical.verification_expressions) + 7,
    )
    malformed_scope = replace(scope, goals=(missing_expression,))
    malformed = replace(canonical, verification_scopes=(malformed_scope,))
    with pytest.raises(CanonicalizationError, match="expression root .* does not exist"):
        restore(malformed)
    negative_expression = replace(goal, expression=-1)
    with pytest.raises(CanonicalizationError, match="expression root %-1 does not exist"):
        restore(replace(
            canonical,
            verification_scopes=(replace(scope, goals=(negative_expression,)),),
        ))

    with pytest.raises(CanonicalizationError, match="references missing clock 'absent'"):
        restore(replace(
            canonical,
            verification_scopes=(replace(scope, clock="absent"),),
        ))
    with pytest.raises(
        CanonicalizationError,
        match="must use reset 'rst' for clock 'clk'",
    ):
        restore(replace(
            canonical,
            verification_scopes=(replace(scope, reset="wrong"),),
        ))


def test_canonical_verification_ids_are_globally_unique() -> None:
    result = _compile(
        "module V { clock a reset ra @a clock b reset rb @b "
        "in x:bit @a out y:bit @a y=x "
        "assert ax @a { y == x } cover by @b { 1 } }"
    )
    canonical = lower(result.ir)
    first, second = canonical.verification_scopes

    with pytest.raises(ValueError, match="scope IDs must be unique"):
        replace(
            canonical,
            verification_scopes=(
                first,
                replace(
                    second,
                    semantic_id=first.semantic_id,
                    goals=tuple(
                        replace(goal, scope_id=first.semantic_id)
                        for goal in second.goals
                    ),
                ),
            ),
        )

    first_goal = first.goals[0]
    second_goal = second.goals[0]
    with pytest.raises(ValueError, match="goal IDs must be unique"):
        replace(
            canonical,
            verification_scopes=(
                first,
                replace(
                    second,
                    goals=(replace(second_goal, semantic_id=first_goal.semantic_id),),
                ),
            ),
        )


def test_compile_time_false_requirement_is_a_semantic_error() -> None:
    with pytest.raises(
        SemanticError,
        match="verification requirement 'impossible' is compile-time false",
    ):
        _compile(
            "module Impossible { clock clk reset rst in a:bit out y:bit y=a "
            "contract c { require impossible { 0 } assert output_ok { y == a } } }"
        )


def test_verification_predicates_resolve_module_parameters_as_constants() -> None:
    result = _compile(
        "module Bounded<DEPTH=4> { clock clk reset rst in count:u3 out y:u3 "
        "y=count assert within_depth { count <= DEPTH } "
        "contract legal { require legal_count { count < DEPTH } "
        "ensure unchanged { y == count } } }"
    )

    rendered = {
        item.generated_from: item.predicate.render()
        for item in result.formal_design.properties
        if item.predicate is not None
    }
    assert rendered["verification-assert:$module:within_depth"] == "(port:count <= 4)"
    assert rendered["verification-ensure:legal:unchanged"] == (
        "((port:count < 4) -> (port:y == port:count))"
    )


def test_quantized_fixed_conversion_in_predicate_fails_semantically() -> None:
    with pytest.raises(SemanticError, match="cannot use quantized fixed-point") as caught:
        _compile(
            "module BadFixedGoal { clock clk reset rst "
            "in a:fixed<16,8> out y:bit y=0 "
            "assert quantized { "
            "quantize<fixed<12,4>>(a){round floor overflow wrap} == 0 "
            "} }"
        )
    assert caught.value.code == "ZL-VERIFY-PREDICATE"
    assert caught.value.primary is not None


def test_cycle_simulator_samples_internal_assertions_before_state_commit() -> None:
    result = _compile(
        "module CheckedCounter { clock clk reset rst out value:u2 "
        "reg count:u2=0 count <- truncate<2>(count + 1) value=count "
        "assert bounded { count < 2 } }"
    )

    with pytest.raises(VerificationAssertionError) as caught:
        simulate_cycles(result.ir, ({}, {}, {}), reset=(False, False, False))

    assert caught.value.goal_name == "bounded"
    assert caught.value.cycle == 2
    assert caught.value.source_origin is not None


def test_packed_aggregate_projections_lower_to_exact_typed_predicates() -> None:
    struct_output = _compile(
        "struct Payload { ok:bit data:u8 } "
        "module StructOutput { clock clk reset rst in a:Payload out y:Payload y=a "
        "contract io { ensure output_ok { y.ok } } }"
    )
    predicate = next(
        item.predicate
        for item in struct_output.formal_design.properties
        if item.generated_from == "verification-ensure:io:output_ok"
    )
    assert predicate is not None
    assert predicate.width == 1

    protocol = _compile(
        "struct Payload { ok:bit data:u8 } "
        "module Protocol { clock clk reset rst in rx:rv<Payload> out tx:rv<Payload> "
        "tx.payload=rx.payload tx.valid=rx.valid rx.ready=tx.ready "
        "contract io { ensure output_ok { tx.payload.ok | !tx.valid } } }"
    )
    protocol_predicate = next(
        item.predicate
        for item in protocol.formal_design.properties
        if item.generated_from == "verification-ensure:io:output_ok"
    )
    assert protocol_predicate is not None
    assert "port:tx.payload" in protocol_predicate.observation_ids()

    fifo = _compile(
        "struct Payload { ok:bit data:u8 } "
        "module Queue { clock clk reset rst in push:bit in pop:bit in d:Payload "
        "out front_ok:bit fifo q:fifo<Payload,2> "
        "q.data=d q.push=push q.pop=pop front_ok=q.front.ok "
        "assert front_shape { q.front.ok | !q.valid } }"
    )
    fifo_predicate = next(
        item.predicate
        for item in fifo.formal_design.properties
        if item.generated_from == "verification-assert:$module:front_shape"
    )
    assert fifo_predicate is not None
    assert "fifo:q.front" in fifo_predicate.observation_ids()


def test_verification_inlines_immutable_fixed_point_locals() -> None:
    result = _compile(
        "module FixedLocal { clock clk reset rst in a:SF8.8 out is_zero:bit "
        "zero_value:SF8.8=fixed_raw(0) is_zero=a==zero_value "
        "assert exact_zero { a == zero_value } }"
    )
    predicate = next(
        item.predicate
        for item in result.formal_design.properties
        if item.generated_from == "verification-assert:$module:exact_zero"
    )
    assert predicate is not None
    assert predicate.render() == "(port:a == 0)"


def test_structured_field_slice_and_pure_call_predicates_lower() -> None:
    result = _compile(
        "struct Pair { left:u4 right:u4 } "
        "fn nonzero(x:u4)->bit { x != 0 } "
        "module Structured { clock clk reset rst in x:u4 out p:Pair "
        "p = Pair { left=x right=0 } "
        "contract io { ensure left_matches { p.left == x } } "
        "assert left_bits { pack(p)[7:4] == pack(x) } "
        "cover nonzero_left { nonzero(p.left) } }"
    )
    source_properties = [
        item for item in result.formal_design.properties
        if (item.generated_from or "").startswith("verification-")
    ]
    source_covers = [
        item for item in result.formal_design.covers
        if (item.generated_from or "").startswith("verification-cover:")
    ]
    assert len(source_properties) == 2
    assert len(source_covers) == 1
    assert all(item.predicate is not None for item in (*source_properties, *source_covers))
