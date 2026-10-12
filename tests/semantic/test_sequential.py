from pathlib import Path
import unittest

from zlang.ir.expressions import Delay, RegisterRef, Truncate
from zlang.ir.types import UIntType
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[2]


class SequentialSemanticTests(unittest.TestCase):
    def test_counter_register_and_next_state_are_typed(self) -> None:
        module = analyze(parse((ROOT / "examples/counter.zhl").read_text()))
        self.assertEqual(module.clock, "clk")
        self.assertEqual(module.reset, "rst")
        self.assertEqual(module.registers[0].type, UIntType(8))
        self.assertIsInstance(module.assignments[0].expression, RegisterRef)
        self.assertIsInstance(module.next_assignments[0].expression, Truncate)

    def test_delay_is_sequential_typed_ir(self) -> None:
        module = analyze(parse((ROOT / "examples/delayed_mul.zhl").read_text()))
        expression = module.assignments[0].expression
        self.assertIsInstance(expression, Delay)
        self.assertEqual(expression.cycles, 2)
        self.assertEqual(expression.type, UIntType(16))

    def test_clock_and_reset_are_required_together(self) -> None:
        source = "module Bad { clock clk out y:u1 y=0 }"
        with self.assertRaisesRegex(SemanticError, "clock and reset must be declared together"):
            analyze(parse(source))

    def test_named_clock_domains_require_explicit_reset_associations(self) -> None:
        clocks = "module Bad { clock a clock b reset r out y:u1 y=0 }"
        resets = "module Bad { clock c reset a reset b out y:u1 y=0 }"
        with self.assertRaisesRegex(SemanticError, "requires an explicit clock domain"):
            analyze(parse(clocks))
        with self.assertRaisesRegex(SemanticError, "requires exactly one reset"):
            analyze(parse(resets))

    def test_register_requires_clock_and_reset(self) -> None:
        source = "module Bad { reg x:u8=0 out y:u8 y=x }"
        with self.assertRaisesRegex(SemanticError, "register 'x' requires a clock"):
            analyze(parse(source))

    def test_register_initial_value_must_be_constant_and_exact(self) -> None:
        nonconstant = """
            module Bad { clock c reset r in a:u8 reg x:u8=a out y:u8 y=x }
        """
        overflow = """
            module Bad { clock c reset r reg x:u8=256 out y:u8 y=x }
        """
        with self.assertRaisesRegex(SemanticError, "unknown input 'a'"):
            analyze(parse(nonconstant))
        with self.assertRaisesRegex(SemanticError, "constant 256 does not fit u8"):
            analyze(parse(overflow))

    def test_next_state_target_and_type_are_checked(self) -> None:
        missing = """
            module Bad { clock c reset r out y:u8 y=0 missing <- 0 }
        """
        width = """
            module Bad { clock c reset r reg x:u8=0 out y:u8 y=x x <- x + 1 }
        """
        with self.assertRaisesRegex(SemanticError, "target 'missing' is not a register"):
            analyze(parse(missing))
        with self.assertRaisesRegex(SemanticError, "next state.*has type u9, expected u8"):
            analyze(parse(width))

    def test_register_has_at_most_one_next_assignment(self) -> None:
        source = """
            module Bad { clock c reset r reg x:u8=0 out y:u8 y=x x<-0 x<-1 }
        """
        with self.assertRaisesRegex(SemanticError, "more than one next-state"):
            analyze(parse(source))

    def test_missing_next_assignment_means_hold(self) -> None:
        source = "module Hold { clock c reset r reg x:u8=3 out y:u8 y=x }"
        module = analyze(parse(source))
        self.assertEqual(module.next_assignments, ())

    def test_delay_requires_clock_and_reset(self) -> None:
        source = "module Bad { in a:u8 out y:u8 y=delay<1>(a) }"
        with self.assertRaisesRegex(SemanticError, "delay requires a module clock"):
            analyze(parse(source))

    def test_delay_is_not_allowed_in_pure_function(self) -> None:
        source = """
            fn bad(x:u8)->u8 { delay<1>(x) }
            module Bad { clock c reset r in x:u8 out y:u8 y=bad(x) }
        """
        with self.assertRaisesRegex(SemanticError, "delay requires a module clock"):
            analyze(parse(source))

    def test_delay_of_aggregate_is_rejected_until_reset_values_exist(self) -> None:
        source = """
            struct P { x:u8 }
            module Bad { clock c reset r in p:P out y:P y=delay<1>(p) }
        """
        with self.assertRaisesRegex(SemanticError, "delay reset value is not defined"):
            analyze(parse(source))


if __name__ == "__main__":
    unittest.main()
