"""Optional range inference never requires full expansion of opaque guards."""

import pytest
from types import SimpleNamespace

from tests.support.typed_callables import typed_function
from zlang import sim
from zlang.compiler import compile_source
from zlang.ir import callables as ir_callables
from zlang.ir.expressions import Call
from zlang.ir.types import UIntType
from zlang.semantic import SemanticError
from zlang.semantic.callables import _expand_analysis_calls


def source(*, nested=False, action="flag <- 1", ports="", result="y=flag"):
    chain = "\n".join(
        f"t{i}=mix({'a' if i == 0 else f't{i-1}'})" for i in range(17)
    )
    guard = "chain(a)!=0"
    body = f"when 1 {{when {guard} {{{action}}}}}" if nested else f"when {guard} {{{action}}}"
    return f'''fn mix(x:bits<8>)->bits<8>{{x ^ (x<<1)}}
fn chain(a:bits<8>)->bits<8>{{{chain}\nt16}}
module Top {{clock clk reset rst in a:bits<8> out y:bit
 {ports} reg flag:bit=0 {body} {result}}}'''


@pytest.mark.parametrize("nested", (False, True))
def test_opaque_large_guard_preserves_native_binary_values(tmp_path, nested):
    path = tmp_path / "top.zhl"
    path.write_text(source(nested=nested))
    with sim.compile(path).create() as machine:
        for value in range(256):
            machine.set("a", value)
            machine.reset("rst", asserted=True)
            machine.tick("clk")
            machine.reset("rst", asserted=False)
            machine.tick("clk")
            expected = value
            for _ in range(17):
                expected = (expected ^ (expected << 1)) & 255
            assert machine.get("y") == int(expected != 0)


@pytest.mark.parametrize("nested", (False, True))
def test_opaque_large_guard_does_not_invent_index_bounds(nested):
    text = source(
        nested=nested,
        ports="in values:vec<4,bit> in key:u3",
        action="flag <- values[key]",
    )
    with pytest.raises(SemanticError, match="range|index"):
        compile_source(text)


def test_small_callable_predicate_still_refines_indices():
    compile_source('''fn valid(key:u3)->bit{key<4}
module Top {clock clk reset rst in values:vec<4,bit> in key:u3
 out y:bit reg flag:bit=0 when valid(key){flag<-values[key]} y=flag}''')


def test_large_guard_does_not_hide_wrong_argument_type():
    with pytest.raises(SemanticError):
        compile_source(source().replace("chain(a)!=0", "chain(extend<16>(a))!=0"))


@pytest.mark.parametrize("invalid", ("cycle", "unknown"))
def test_budget_fallback_revalidates_entire_retained_graph(monkeypatch, invalid):
    type_ = UIntType(8)
    root = Call("f", (), type_, "f-id")
    body = root if invalid == "cycle" else Call("absent", (), type_, "absent-id")
    definition = typed_function("f", (), type_, body, "f-id")
    context = SimpleNamespace(services=SimpleNamespace(callables=SimpleNamespace(
        function_definitions={"f": definition}, callable_definitions={})))

    def exhausted(*args, **kwargs):
        raise ir_callables.CallableExpansionNodeLimit("fixture node budget")

    monkeypatch.setattr(ir_callables, "expand_callable_calls", exhausted)
    with pytest.raises(SemanticError, match="recursive|cycle|unknown"):
        _expand_analysis_calls(root, context, purpose="guard", refinement_only=True)


def test_mandatory_analysis_does_not_accept_an_incomplete_view(monkeypatch):
    def exhausted(*args, **kwargs):
        raise ir_callables.CallableExpansionNodeLimit("fixture node budget")

    root = Call("f", (), UIntType(8), "f-id")
    context = SimpleNamespace(services=SimpleNamespace(callables=SimpleNamespace(
        function_definitions={}, callable_definitions={})))
    monkeypatch.setattr(ir_callables, "expand_callable_calls", exhausted)
    with pytest.raises(SemanticError, match="fixture node budget"):
        _expand_analysis_calls(root, context, purpose="index")
