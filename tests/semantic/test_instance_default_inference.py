"""Internal defaults must not force premature sibling-input evaluation."""

import pytest

from zlang.compiler import compile_source
from zlang.semantic import SemanticError


def source(width="W=8,", actual="u8"):
    return f"""
module Cell<W=8,D=2> {{
    in x:uint<W> out y:uint<W> y=x
}}
module Top {{
    in a:{actual} out y:{actual}
    first:Cell<{width}D=2>
    later:Cell<W=8,D=2>
    first.x=later.y
    later.x=a
    y=first.y
}}
"""


def test_unrelated_default_does_not_evaluate_forward_sibling():
    implicit = compile_source(
        source().replace("first:Cell<W=8,D=2>", "first:Cell<W=8>"), top="Top"
    )
    explicit = compile_source(source(), top="Top")
    assert implicit.ir == explicit.ir


def test_input_parameter_inference_still_overrides_its_default():
    result = compile_source(
        """
module Cell<W=8,D=2>{in x:uint<W> out y:uint<W> y=x}
module Top{in a:u16 out y:u16 c:Cell<D=2> c.x=a y=c.y}
""",
        top="Top",
    )
    assert str(result.ir.children[0].inputs[0].type) == "u16"


def test_concrete_input_type_mismatch_is_not_suppressed():
    with pytest.raises(SemanticError, match="u16|u8"):
        compile_source(source(actual="u16"), top="Top")


def test_inference_does_not_evaluate_concrete_runtime_control_binding():
    text = """
module Cell<W=8>{in x:uint<W> in enable:bit out y:uint<W> y=enable ? x : 0}
module Top {
    in a:u16 in enable:bit out y:u16
    c:Cell
    c.x=a
    c.enable=late
    late=~enable
    y=c.y
}
"""
    implicit = compile_source(text, top="Top")
    explicit = compile_source(text.replace("c:Cell\n", "c:Cell<W=16>\n"), top="Top")
    assert implicit.ir == explicit.ir


def test_inferred_width_does_not_hide_concrete_control_type_mismatch():
    text = """
module Cell<W=8>{in x:uint<W> in enable:bit out y:uint<W> y=enable ? x : 0}
module Top {in a:u16 in wrong:u2 out y:u16 c:Cell c.x=a c.enable=wrong y=c.y}
"""
    with pytest.raises(SemanticError, match="bit|u2"):
        compile_source(text, top="Top")


def _derived_source(binding="later.y", producer_first=False, explicit=False, *, array_producer=False):
    first = "first:Cell<W=8" + (",P=12>" if explicit else ">")
    later = "later[2]:Cell<W=8,P=12>" if array_producer else "later:Cell<W=8>"
    actual = "later[1].y" if array_producer else "later.y"
    declarations = f"{later}\n{first}" if producer_first else f"{first}\n{later}"
    local = ""
    if binding == "typed":
        local, binding = f"late:bits<12>={actual}", "late"
    elif binding == "untyped":
        local, binding = f"late={actual}", "late"
    bindings = "generate(i in 0..2){later[i].x=a}" if array_producer else "later.x=a"
    return f"""
module Cell<W=8,P=W+2> {{
    clock clk reset rst in x:bits<P> out reg y:bits<P>=0 y <- x
}}
module Top {{
    clock clk reset rst in a:bits<12> out y:bits<12>
    {declarations}
    first.x={binding} {bindings}
    {local}
    y=first.y
}}
"""


@pytest.mark.parametrize("binding", ("later.y", "typed", "untyped"))
@pytest.mark.parametrize("producer_first", (False, True))
def test_derived_input_width_uses_later_sibling_and_parent_local(binding, producer_first):
    array = binding != "later.y"
    implicit = compile_source(_derived_source(binding, producer_first, array_producer=array), top="Top")
    explicit = compile_source(_derived_source(binding, producer_first, True, array_producer=array), top="Top")
    assert implicit.ir == explicit.ir
    later = ["later[0]", "later[1]"] if array else ["later"]
    assert [instance.name for instance in implicit.ir.instances] == (
        [*later, "first"] if producer_first else ["first", *later]
    )
    assert all(str(child.inputs[0].type) == "bits<12>" for child in implicit.ir.children)


@pytest.mark.parametrize("type_", ("u16", "s13", "bits<10>", "fixed<12,4>", "Box<u8>"))
def test_required_type_inference_uses_later_sibling_without_defaults(type_):
    array = type_ != "u16"
    later = f"later[2]:Cell<T={type_}>" if array else f"later:Cell<T={type_}>"
    bindings = "generate(i in 0..2){later[i].x=a}" if array else "later.x=a"
    actual = "later[1].y" if array else "later.y"
    text = f"""struct Box<type T>{{value:T}}
module Cell<type T>{{in x:T out y:T y=x}}
module Top {{in a:{type_} out y:{type_}
    first:Cell {later}
    first.x=late {bindings} late={actual} y=first.y
}}"""
    implicit = compile_source(text, top="Top")
    explicit = compile_source(text.replace("first:Cell ", f"first:Cell<T={type_}> "), top="Top")
    assert implicit.ir == explicit.ir


@pytest.mark.parametrize("anchor_first", (False, True))
@pytest.mark.parametrize("reverse_children", (False, True))
def test_mutual_registered_inputs_infer_from_anchors_before_defaults(anchor_first, reverse_children):
    ports = "in anchor:bits<W> in x:bits<W>" if anchor_first else "in x:bits<W> in anchor:bits<W>"
    children = "right:Cell left:Cell" if reverse_children else "left:Cell right:Cell"
    text = f"""module Cell<W=8>{{clock clk reset rst {ports}
        out reg y:bits<W>=0 y <- x ^ anchor}}
module Top {{clock clk reset rst in a,b:bits<16> out y:bits<16>
    {children} left.x=right.y right.x=left.y left.anchor=a right.anchor=b y=left.y
}}"""
    implicit = compile_source(text, top="Top")
    explicit = compile_source(text.replace("left:Cell", "left:Cell<W=16>")
                             .replace("right:Cell", "right:Cell<W=16>"), top="Top")
    assert implicit.ir == explicit.ir


def test_self_output_type_is_anchored_by_another_input():
    text = """module Cell<W=8>{clock clk reset rst in x:bits<W> in anchor:bits<W>
        out reg y:bits<W>=0 y <- x ^ anchor}
module Top{clock clk reset rst in a:bits<16> out y:bits<16>
    child:Cell child.x=child.y child.anchor=a y=child.y} """
    assert compile_source(text, top="Top").ir == compile_source(
        text.replace("child:Cell ", "child:Cell<W=16> "), top="Top",
    ).ir


@pytest.mark.parametrize("bad_local, message", (
    ("late:bits<12>=missing", "unknown input 'missing'"),
    ("late:bits<12>=zeros<11>", "bits<11>.*bits<12>|bits<12>.*bits<11>"),
    ("late=late", "cyclic local type inference"),
))
def test_type_probes_do_not_suppress_normal_local_validation(bad_local, message):
    text = _derived_source("typed").replace("late:bits<12>=later.y", bad_local)
    with pytest.raises(SemanticError, match=message):
        compile_source(text, top="Top")


def test_inferred_sibling_width_does_not_hide_combinational_cycle():
    text = """module Cell<W=8>{in x:bits<W> in anchor:bits<W> out y:bits<W> y=x}
module Top {in a:bits<16> out y:bits<16> left:Cell right:Cell
    left.x=right.y right.x=left.y left.anchor=a right.anchor=a y=left.y}"""
    with pytest.raises(SemanticError, match="combinational child dependency cycle"):
        compile_source(text, top="Top")


def test_unanchored_required_inference_cycle_fails_closed():
    text = """module Cell<W>{clock clk reset rst in x:bits<W> out reg y:bits<W>=0 y<-x}
module Top{clock clk reset rst out y:bits<8> first:Cell later:Cell
    first.x=later.y later.x=first.y y=first.y}"""
    with pytest.raises(SemanticError, match="cyclic exact specialization inference"):
        compile_source(text, top="Top")


@pytest.mark.parametrize("condition", ("1", "selected"))
def test_inference_only_queries_the_selected_compile_time_branch(condition):
    text = f"""module Cell<type T>{{in x:T out y:T y=x}}
module Other{{out y:u8 y=0}}
module Top{{in a:u8 out y:u8 c:Cell later:Other
    c.x=if {condition} {{a}} else {{later.missing}}
    selected:bit=1 y=c.y}}"""
    implicit = compile_source(text, top="Top")
    explicit = compile_source(text.replace("c:Cell ", "c:Cell<T=u8> "), top="Top")
    assert implicit.ir == explicit.ir


def test_typed_self_local_can_anchor_a_child_without_default_guessing():
    text = """module Cell<W>{clock clk reset rst in x:bits<W> out reg y:bits<W>=0 y<-x}
module Top {clock clk reset rst out y:bits<12> c:Cell c.x=late
    late:bits<12>=c.y y=c.y}"""
    assert compile_source(text, top="Top").ir == compile_source(
        text.replace("c:Cell ", "c:Cell<W=12> "), top="Top",
    ).ir


@pytest.mark.parametrize("anchor_first", (False, True))
@pytest.mark.parametrize("reverse_children", (False, True))
def test_one_external_anchor_resolves_both_mutually_connected_children(anchor_first, reverse_children):
    ports = "in anchor:bits<W> in x:bits<W>" if anchor_first else "in x:bits<W> in anchor:bits<W>"
    children = "right:Cell left:Anchor" if reverse_children else "left:Anchor right:Cell"
    text = f"""module Anchor<W>{{clock clk reset rst {ports}
        out reg y:bits<W>=0 y<-x ^ anchor}}
module Cell<W>{{clock clk reset rst in x:bits<W> out reg y:bits<W>=0 y<-x}}
module Top{{clock clk reset rst in a:bits<16> out y:bits<16> {children}
    left.x=right.y left.anchor=a right.x=left.y y=left.y}}"""
    explicit = text.replace("left:Anchor", "left:Anchor<W=16>").replace("right:Cell", "right:Cell<W=16>")
    assert compile_source(text, top="Top").ir == compile_source(explicit, top="Top").ir


def test_inferred_generated_local_preserves_functional_binder_identities(monkeypatch):
    from copy import copy
    from dataclasses import replace
    from zlang.ast import nodes as ast
    from zlang.ir import types as ir_types
    from zlang.semantic.instances import InstanceSpecializer, _PendingInputType
    from zlang.source import SourceSpan

    original_query = InstanceSpecializer._inference_expression_type
    probes, signature_checks = [], []

    def published(context):
        arena, scope, tooling = context.services.expression_arena, context.scope, context.services.tooling
        def optional(values):
            return None if values is None else tuple(values)
        return (
            arena.statistics, dict(context.environment.type_resolver._resolved),
            tuple(context.environment.type_resolver._active),
            tuple((id(value), origins) for value, origins in arena.provenance_table._entries),
            scope.next_delay_instance, dict(scope.functional_binder_ordinals),
            tuple(scope.next_functional_binder_ordinal), tuple(scope.functional_specialization_certificates),
            dict(scope.instance_outputs), dict(scope.instance_arrays),
            optional(tooling.definition_resolutions), optional(tooling.definition_declarations),
            optional(tooling.completion_scopes), optional(tooling.signature_help_calls),
            dict(tooling.definition_targets),
        )

    def check_partial_signature(specializer):
        declaration = specializer._instance_declarations["c"]
        child = specializer.known_modules[declaration.module]
        assert id(declaration) in specializer._inference_arguments
        resolver = specializer.context.type_resolver
        query = resolver.for_query()
        assert query is not resolver and query._resolved is not resolver._resolved
        assert query._active is not resolver._active and query._definition_context is None
        query._resolved["query_only"] = ir_types.UIntType(8)
        assert "query_only" not in resolver._resolved

        def isolated(syntax, parameters=(), values=None):
            probe = copy(specializer)
            probe.known_modules = dict(specializer.known_modules)
            probe._specializations = dict(specializer._specializations)
            probe._inference_arguments = {id(declaration): (dict(values or {}), {})}
            probe.known_modules[declaration.module] = replace(
                child, parameters=(*child.parameters, *parameters),
                source_identity="demo.signature", source_hash="d" * 64,
                ports=tuple(replace(port, type_name=syntax)
                            if port.direction is ast.Direction.OUTPUT and port.name == "y" else port
                            for port in child.ports),
            )
            return probe

        with pytest.raises(_PendingInputType):
            isolated(ast.TypeName("T"))._inference_output_type(declaration, "y")
        signature_checks.append("declared-unbound-parameter")
        span = SourceSpan(7, 9, 7, 20)
        with pytest.raises(SemanticError, match="unknown type 'MissingType'") as caught:
            isolated(ast.TypeName("MissingType", origin=span))._inference_output_type(declaration, "y")
        assert caught.value.primary is not None and caught.value.primary.span == span
        assert caught.value.primary.source_unit == "demo.signature" and caught.value.primary.digest == "d" * 64
        signature_checks.append("invalid-concrete-type-with-child-origin")
        with pytest.raises(SemanticError, match="positive"):
            isolated(ast.TypeName("bits<P>"), (ast.ModuleParameter("P"),), {"P": 0})._inference_output_type(declaration, "y")
        signature_checks.append("invalid-concrete-width")

    def observe(specializer, expression):
        context = specializer.context.expression_context
        before = published(context)
        result = original_query(specializer, expression)
        after = published(context)
        probes.append((before, after))
        assert after == before, "type-only probe published parent state"
        if not signature_checks:
            check_partial_signature(specializer)
            assert published(context) == after
        return result

    monkeypatch.setattr(InstanceSpecializer, "_inference_expression_type", observe)
    text = """module Cell<type T>{in x:T out y:T y=x}
module Top {in a:vec<2,u8> out y:vec<2,u8> c:Cell
    c.x=late late=generate(i in 0..2) a[i] y=c.y}"""
    implicit = compile_source(text, top="Top")
    explicit = compile_source(text.replace("c:Cell\n", "c:Cell<T=vec<2,u8>>\n"), top="Top")
    assert implicit.ir == explicit.ir
    assert implicit.selected_ir_identity == explicit.selected_ir_identity
    assert probes and all(before == after for before, after in probes)
    assert signature_checks == ["declared-unbound-parameter", "invalid-concrete-type-with-child-origin", "invalid-concrete-width"]
