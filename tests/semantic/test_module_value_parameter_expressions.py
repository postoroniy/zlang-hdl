from dataclasses import fields, is_dataclass
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from zlang.backend.clash import emit as emit_clash
from zlang.backend.systemverilog import emit_experimental
from zlang.equivalence import make_equivalence_property
from zlang.ir import Constant, ParameterRef
from zlang.ir import EquivalenceRelation
from zlang.ir.callables import expand_callable_calls
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.opt import lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.toolchain import find_clash_executable, generate_verilog


ROOT = Path(__file__).resolve().parents[2]


def _walk(value: object):
    yield value
    if isinstance(value, tuple):
        for item in value:
            yield from _walk(item)
    elif is_dataclass(value):
        for item in fields(value):
            yield from _walk(getattr(value, item.name))


def test_fft_scheduling_parameters_become_contextual_concrete_constants() -> None:
    source = (ROOT / "tests" / "fixtures" / "fft" /
              "value_parameter_expression.zl").read_text()
    module = analyze(parse(source))
    constants = [item for item in _walk(module.assignments) if isinstance(item, Constant)]
    assert {(item.value, str(item.type)) for item in constants} >= {
        (4, "u5"), (7, "u5"), (2, "u4"),
    }
    assert not any(
        isinstance(item, ParameterRef) and item.name in {"D", "STEP"}
        for item in _walk(module)
    )
    assert "module parameter D=4" in {
        item.origin.construct for item in constants if item.origin is not None
    }


def test_parameter_arithmetic_folds_with_the_width_depth_evaluator() -> None:
    module = analyze(parse("""
module Fold<N=8,STAGE=2,STEP=3> {
  in x:u8
  out depth:u8 out half:u8 out shifted:u16 out mixed:u16
  depth = 1 << (N - STAGE - 1)
  half = N / 2
  shifted = 1 << N
  mixed = N + STEP
}
"""))
    assert tuple(assignment.expression.value for assignment in module.assignments) == (
        32, 4, 256, 11,
    )


def test_parameter_default_spelling_is_canonicalized_to_its_resolved_value() -> None:
    expression_default = analyze(parse("module M<N=4*2>{out y:u8 y=N}"))
    literal_default = analyze(parse("module M<N=8>{out y:u8 y=N}"))
    assert expression_default.parameters == (("N", "value", 8),)
    assert expression_default.assignments[0].expression.value == 8
    assert expression_semantic_identity(expression_default.assignments[0].expression) == (
        expression_semantic_identity(literal_default.assignments[0].expression)
    )


def test_mixed_runtime_parameter_operands_match_literal_context_typing() -> None:
    parameterized = analyze(parse(
        "module P<D=4>{in counter:u3 in value:u8 out phase:bit out shifted:u8 "
        "phase=counter>=D shifted=value<<D}"
    ))
    literal = analyze(parse(
        "module P{in counter:u3 in value:u8 out phase:bit out shifted:u8 "
        "phase=counter>=4 shifted=value<<4}"
    ))
    assert tuple(
        expression_semantic_identity(item.expression)
        for item in parameterized.assignments
    ) == tuple(
        expression_semantic_identity(item.expression)
        for item in literal.assignments
    )


def test_nested_value_specialization_resolves_at_every_hierarchy_level() -> None:
    module = analyze(parse("""
module GrandChild<K=1>{in x:u8 out y:bit y=x>=K}
module Child<D=2>{in x:u8 out y:bit inst g:GrandChild<K=D-1>{x} y=g.y}
module Top<N=8>{in x:u8 out y:bit inst c:Child<D=N/2>{x} y=c.y}
"""))
    child = module.children[0]
    grandchild = child.children[0]
    assert module.parameters == (("N", "value", 8),)
    assert child.parameters == (("D", "value", 4),)
    assert grandchild.parameters == (("K", "value", 3),)
    assert grandchild.assignments[0].expression.right.value == 3


def test_type_and_value_parameters_share_specialization_without_conflation() -> None:
    module = analyze(parse("""
module Stage<type Sample,D=4>{
  clock c reset r in x:Sample in counter:u4 out phase:bit
  fifo delay:fifo<Sample,D>
  delay.data=x delay.push=0 delay.pop=0 phase=counter>=D
}
module Top{clock c reset r in x:u8 in counter:u4 out phase:bit
  inst s:Stage<Sample=u8,D=8>{x counter} phase=s.phase}
"""))
    child = module.children[0]
    assert str(child.inputs[0].type) == "u8"
    assert child.fifos[0].depth == 8
    assert child.assignments[-1].expression.right.value == 8


def test_generic_function_value_parameter_uses_same_constant_semantics() -> None:
    module = analyze(parse(
        "fn ge<N=4>(x:u8)->bit{x>=N} "
        "module M{in x:u8 out a:bit out b:bit a=ge(x) b=ge<7>(x)}"
    ))
    expanded = tuple(
        expand_callable_calls(
            item.expression,
            (*module.functions, *module.callable_definitions),
        )
        for item in module.assignments
    )
    assert tuple(item.right.value for item in expanded) == (4, 7)


def test_canonical_round_trip_and_literal_form_have_same_expression_identity() -> None:
    parameterized = analyze(parse("module M<D=4>{in x:u8 out y:bit y=x>=D}"))
    literal = analyze(parse("module M{in x:u8 out y:bit y=x>=4}"))
    canonical = lower(parameterized)
    assert restore(canonical) == parameterized
    assert not any(
        isinstance(item, ParameterRef) and item.name == "D"
        for item in _walk(canonical)
    )
    assert expression_semantic_identity(parameterized.assignments[0].expression) == (
        expression_semantic_identity(literal.assignments[0].expression)
    )
    equivalence = make_equivalence_property(
        parameterized.assignments[0].expression,
        literal.assignments[0].expression,
        candidate_class="value",
        reference_root="parameterized",
        implementation_root="literal",
        inputs=("x",),
    )
    assert equivalence.relation_kind is EquivalenceRelation.SAME_CYCLE_VALUE


@pytest.mark.parametrize(
    ("source", "message"),
    (
        ("module M<D>{in x:u8 out y:bit y=x>=D}", "unresolved compile-time parameter 'D'"),
        ("module C<D>{in x:u8 out y:u8 y=x} module M{in x:u8 in n:u8 out y:u8 inst c:C<D=n>{x} y=c.y}", "unresolved compile-time parameter 'n'"),
        ("module M<D=300>{in x:u3 out y:bit y=x>=D}", "constant 300 does not fit u3"),
        ("module M<D=-1>{in x:u8 out y:u8 y=x<<D}", "shift amount must be non-negative"),
        ("module M<D=0>{out y:u8 y=4/D}", "division by zero"),
        ("module M<D=3>{out y:u8 y=D/2}", "division must be exact"),
        ("module M<D=2>{in x:u8 out y:u8 y=x/D}", "only supported in compile-time"),
        ("module M<D=2>{in D:u8 out y:u8 y=D}", "conflicts with module value parameter"),
    ),
)
def test_value_parameter_expression_diagnostics(source: str, message: str) -> None:
    with pytest.raises(SemanticError, match=message):
        analyze(parse(source))


def test_both_backends_only_receive_concrete_parameter_operands() -> None:
    cases = (
        ("ParamCompare", "in x:u8 out y:bit y=x>=D"),
        ("ParamArithmetic", "in x:u8 out y:u16 y=x*D"),
        ("ParamTernary", "in x:u8 out y:u8 y=x>=D ? D : 0"),
        ("ParamShift", "in x:u8 out y:u8 y=x<<D"),
    )
    direct_sources: list[tuple[str, str]] = []
    clash_sources: list[tuple[str, str]] = []
    for name, body in cases:
        module = analyze(parse(f"module {name}<D=4>{{{body}}}"))
        direct = emit_experimental(module)
        clash = emit_clash(module)
        assert "parameter" not in direct.lower()
        direct_sources.append((name, direct))
        clash_sources.append((name, clash))

    verilator = shutil.which("verilator")
    if verilator is not None:
        with tempfile.TemporaryDirectory() as directory:
            for name, direct in direct_sources:
                path = Path(directory) / f"{name}.sv"
                path.write_text(direct)
                result = subprocess.run(
                    (verilator, "--lint-only", "-Wall", "--top-module", name, str(path)),
                    text=True, capture_output=True,
                )
                assert result.returncode == 0, result.stderr

    clash_executable = find_clash_executable()
    if clash_executable is not None:
        with tempfile.TemporaryDirectory() as directory:
            name, clash = clash_sources[2]
            files = generate_verilog(
                clash, name,
                Path(directory), clash_executable,
            )
            assert files
