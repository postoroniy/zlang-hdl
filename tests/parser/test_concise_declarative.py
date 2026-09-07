from dataclasses import replace
from pathlib import Path
import unittest

from zlang.backend.systemverilog import emit_artifact, emit_experimental
from zlang.compiler import compile_source
from zlang.opt import lower, restore
from zlang.opt.identity import canonical_ir_identity
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.ir import expressions as ir_expr


class ConciseDeclarativeSyntaxTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[2]

    def test_concise_instance_and_bare_connection_match_verbose_ir(self) -> None:
        child = "module Child { in x:u8 out y:u8 y=x }"
        verbose = child + " module Top { inst c:Child { x } in x:u8 in rx:rv<u8> out tx:rv<u8> out y:u8 connect rx -> tx y=c.y }"
        concise = child + " module Top { c:Child { x } in x:u8 in rx:rv<u8> out tx:rv<u8> out y:u8 rx -> tx y=c.y }"
        self.assertEqual(analyze(parse(verbose)), analyze(parse(concise)))

    def test_concise_named_specialization_matches_verbose_instance(self) -> None:
        child = "module Child<type T,N>{in x:T out y:T y=x}"
        verbose = child + " module Top{in x:u8 out y:u8 inst c:Child<T=u8,N=2>{x} y=c.y}"
        concise = child + " module Top{in x:u8 out y:u8 c:Child<T=u8,N=2>{x} y=c.y}"

        syntax = parse(concise)
        self.assertEqual(
            tuple((item.name, item.value) for item in syntax.generic_declarations[0].specializations),
            (("T", "u8"), ("N", 2)),
        )
        verbose_ir = analyze(parse(verbose))
        concise_ir = analyze(syntax)
        self.assertEqual(verbose_ir, concise_ir)
        verbose_canonical = lower(verbose_ir)
        concise_canonical = lower(concise_ir)
        self.assertEqual(
            canonical_ir_identity(verbose_canonical),
            canonical_ir_identity(concise_canonical),
        )
        self.assertEqual(restore(concise_canonical), concise_ir)
        self.assertEqual(
            verbose_ir.elaborated_instances[0].specialization_identity,
            concise_ir.elaborated_instances[0].specialization_identity,
        )
        self.assertEqual(
            verbose_ir.elaborated_instances[0].instance_identity,
            concise_ir.elaborated_instances[0].instance_identity,
        )

    def test_concise_named_specialization_preserves_nested_types_and_values(self) -> None:
        child = "module Child<type T,N>{in x:T out y:T y=x}"
        verbose = (
            child
            + " module Top<K=4>{in x:vec<2,u8> out y:vec<2,u8> "
            "inst c:Child<T=vec<2,u8>,N=K/2>{x} y=c.y}"
        )
        concise = verbose.replace("inst c:", "c:")
        self.assertEqual(analyze(parse(verbose)), analyze(parse(concise)))

    def test_concise_protocol_endpoint_infers_only_domain(self) -> None:
        source = """
        protocol P { role source role sink channel data:rv<u8> source -> sink }
        module Top { clock clk reset rst p:P.source }
        """
        module = analyze(parse(source))
        self.assertEqual(module.aggregate_protocol_endpoints[0].domain, "clk")
        self.assertEqual(module.aggregate_protocol_endpoints[0].members[0].domain, "clk")

    def test_multiple_domains_require_explicit_endpoint_domain(self) -> None:
        source = """
        protocol P { role source role sink channel data:rv<u8> source -> sink }
        module Top { clock a clock b reset ra @a reset rb @b p:P.source }
        """
        with self.assertRaisesRegex(SemanticError, "requires an explicit domain"):
            analyze(parse(source))

    def test_ternary_is_right_associative_and_is_existing_mux_ir(self) -> None:
        module = analyze(parse(
            "module T { in a:bit in b:bit in x:u8 in y:u8 in z:u8 "
            "out q:u8 q = a ? x : b ? y : z }"
        ))
        expression = module.assignments[0].expression
        self.assertIsInstance(expression, ir_expr.Mux)
        self.assertIsInstance(expression.when_false, ir_expr.Mux)

    def test_anonymous_rule_has_stable_non_user_identity(self) -> None:
        source = "module T { clock c reset r reg x:u8=0 when 1 { x <- x } out y:u8 y=x }"
        first = analyze(parse(source))
        second = analyze(parse(source))
        self.assertEqual(first.rules, second.rules)
        self.assertRegex(first.rules[0].name, r"^__anonymous_rule_[0-9a-f]{16}$")
        self.assertIsNotNone(first.rules[0].guard.origin)

    def test_bare_value_requires_initializer(self) -> None:
        with self.assertRaisesRegex(SemanticError, "requires an initializer"):
            analyze(parse("module T { value:u8 }"))

    def test_module_type_collision_is_ambiguous_but_inst_is_escape(self) -> None:
        declarations = "struct Child { x:bit } module Child { in x:bit }"
        with self.assertRaisesRegex(SemanticError, "is ambiguous"):
            analyze(parse(declarations + " module Top { c:Child }"))
        explicit = analyze(
            parse(declarations + " module Top { inst c:Child { x=0 } }")
        )
        self.assertEqual(explicit.instances[0].module, "Child")

    def test_axi_csr_concise_and_verbose_forms_have_identical_backend_identity(self) -> None:
        verbose = (self.ROOT / "examples/axi_csr_top.zhl").read_text()
        concise = (
            verbose.replace("    interface axi :", "    axi :")
            .replace("    inst frontend :", "    frontend :")
            .replace("    inst csr      :", "    csr      :")
            .replace("    connect axi ->", "    axi ->")
            .replace("    connect frontend.regbus ->", "    frontend.regbus ->")
        )
        verbose_ir = compile_source(verbose, top="AxiCsrTop").ir
        concise_ir = compile_source(concise, top="AxiCsrTop").ir
        self.assertEqual(verbose_ir, concise_ir)
        self.assertEqual(emit_experimental(verbose_ir), emit_experimental(concise_ir))
        verbose_artifact = emit_artifact(verbose_ir)
        concise_artifact = emit_artifact(concise_ir)
        self.assertEqual(
            tuple((item.semantic_signal_id, item.rtl_path) for item in verbose_artifact.bindings),
            tuple((item.semantic_signal_id, item.rtl_path) for item in concise_artifact.bindings),
        )

    def test_top_aggregate_pass_through_expands_forward_and_ready_paths(self) -> None:
        prefix = """
        protocol Stream { role source role sink channel t:rv<u8> source -> sink }
        module Top { clock clk reset rst
          interface i:Stream.sink @clk
          interface o:Stream.source @clk
        """
        bare = compile_source(prefix + "i -> o }").ir
        verbose = compile_source(prefix + "connect i -> o }").ir
        self.assertEqual(bare, verbose)
        self.assertEqual(len(bare.aggregate_protocol_connections), 1)
        self.assertFalse(bare.aggregate_protocol_connections[0].delegation)
        self.assertEqual(
            {
                (assignment.target.name, getattr(assignment.signal, "value", None))
                for assignment in bare.assignments
            },
            {("o__t", "payload"), ("o__t", "valid"), ("i__t", "ready")},
        )

    def test_top_aggregate_pass_through_matches_explicit_leaf_backend_behavior(self) -> None:
        prefix = """
        protocol Stream { role source role sink channel t:rv<u8> source -> sink }
        module Top { clock clk reset rst
          interface i:Stream.sink @clk
          interface o:Stream.source @clk
        """
        sugar = compile_source(prefix + "i -> o }")
        manual = compile_source(
            prefix
            + "o.t.payload=i.t.payload o.t.valid=i.t.valid i.t.ready=o.t.ready }"
        )
        self.assertEqual(sugar.clash, manual.clash)
        self.assertEqual(emit_experimental(sugar.ir), emit_experimental(manual.ir))
        self.assertIn("i__t_payload = zlangRvPayload <$> i__t", sugar.clash)
        self.assertIn("o__t_ready = zlangRvReady <$> o__t_backward", sugar.clash)

    def test_top_aggregate_pass_through_rejects_reverse_flow(self) -> None:
        source = """
        protocol Stream { role source role sink channel t:rv<u8> source -> sink }
        module Top { clock clk reset rst
          interface i:Stream.sink @clk
          interface o:Stream.source @clk
          o -> i
        }
        """
        with self.assertRaisesRegex(
            SemanticError, "aggregate pass-through source 'o' has the wrong role"
        ):
            compile_source(source)

    def test_top_aggregate_pass_through_rejects_specialization_mismatch(self) -> None:
        source = """
        protocol Stream<W=8> {
          role source role sink channel t:rv<uint<W>> source -> sink
        }
        module Top { clock clk reset rst
          interface i:Stream<8>.sink @clk
          interface o:Stream<16>.source @clk
          i -> o
        }
        """
        with self.assertRaisesRegex(
            SemanticError, "specialization arguments do not match"
        ):
            compile_source(source)


if __name__ == "__main__":
    unittest.main()
