import unittest

from zlang.parser import ParseError, parse
from zlang.ir import EquivalenceGuardKind
from zlang.opt import lower, restore
from zlang.semantic import SemanticError, analyze


class EquivM27Tests(unittest.TestCase):
    def test_safe_rule_and_guard(self) -> None:
        module = analyze(parse(
            "equiv or_zero { x | zero<x> <=> x when unsigned(x) && width(x) == 8 } "
            "module M { in x:u8 out y:u8 y=x|0 }"
        ))
        self.assertEqual(module.equivalences[0].kind, "or_zero")
        self.assertEqual(module.equivalences[0].variables, ("x",))
        self.assertEqual(module.equivalences[0].bindings, (("value", "x"),))
        self.assertEqual(
            tuple(item.kind for item in module.equivalences[0].guards),
            (EquivalenceGuardKind.UNSIGNED, EquivalenceGuardKind.WIDTH),
        )
        self.assertEqual(restore(lower(module)).equivalences, module.equivalences)

    def test_or_zero_is_exhaustive_for_small_widths(self) -> None:
        for width in range(1, 5):
            for value in range(1 << width):
                self.assertEqual(value | 0, value)

    def test_equiv_is_top_level_only(self) -> None:
        with self.assertRaises(ParseError):
            parse("module M { equiv bad { x <=> x } }")

    def test_arithmetic_identities_are_rejected(self) -> None:
        with self.assertRaises((SemanticError, ParseError)):
            analyze(parse("equiv bad { x * 1 <=> x } module M { in x:u8 out y:u8 y=x }"))
        with self.assertRaises(SemanticError):
            analyze(parse("equiv bad { x * 2 <=> x << 1 } module M { in x:u8 out y:u8 y=x }"))
        with self.assertRaisesRegex(SemanticError, "not an approved"):
            analyze(parse(
                "equiv bad { x | zero<other> <=> x } "
                "module M { in x:u8 out y:u8 y=x }"
            ))
        with self.assertRaisesRegex(SemanticError, "not an approved"):
            analyze(parse(
                "equiv bad { x | zeros<x> <=> x } "
                "module M { in x:u8 out y:u8 y=x }"
            ))

    def test_unsupported_guard_is_rejected(self) -> None:
        with self.assertRaises((SemanticError, ParseError)):
            analyze(parse("equiv bad { x | zero<x> <=> x when foo(x) } module M { in x:u8 out y:u8 y=x }"))

    def test_guards_require_bound_variables_and_exact_arity(self) -> None:
        invalid = (
            ("width(x)", "must compare width"),
            ("unsigned(x) == 8", "does not accept"),
            ("same_type(x)", "expects 2"),
            ("same_type(x,y)", "unbound pattern variable 'y'"),
            ("constant()", "expects 1"),
        )
        for guard, diagnostic in invalid:
            with self.subTest(guard=guard):
                with self.assertRaisesRegex(SemanticError, diagnostic):
                    analyze(parse(
                        f"equiv bad {{ x | zero<x> <=> x when {guard} }} "
                        "module M { in x:u8 out y:u8 y=x }"
                    ))

    def test_source_variable_names_are_retained_as_pattern_roles(self) -> None:
        module = analyze(parse(
            "equiv same { mux(select,value,value) <=> value "
            "when bit(select) && unsigned(value) } "
            "module M { in select:bit in value:u8 out y:u8 y=value }"
        ))
        self.assertEqual(
            module.equivalences[0].bindings,
            (("condition", "select"), ("value", "value")),
        )

    def test_constant_guard_atoms_are_typed_not_retained_as_strings(self) -> None:
        module = analyze(parse(
            "equiv constants { x | zero<x> <=> x "
            "when constant(x) && power_of_two(x) } "
            "module M { in x:u8 out y:u8 y=x }"
        ))
        self.assertEqual(
            tuple(item.kind for item in module.equivalences[0].guards),
            (
                EquivalenceGuardKind.CONSTANT,
                EquivalenceGuardKind.POWER_OF_TWO,
            ),
        )


if __name__ == "__main__":
    unittest.main()
