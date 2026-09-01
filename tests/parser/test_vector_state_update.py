from zlang import ast
from zlang.parser import parse


def test_rule_element_update_retains_typed_target_syntax() -> None:
    module = parse("""
        module VectorWrite {
          clock clk reset rst
          in fire:bit in index:u2 in value:u8
          out y:u8
          reg samples:vec<4,u8>=generate(i in 0..4) 0
          rule write when fire { samples[index] <- value }
          y=samples[0]
        }
    """)

    action = module.rules[0].actions[0]
    assert isinstance(action, ast.NextAssignment)
    assert isinstance(action.target, ast.IndexedAssignmentTarget)
    assert action.target.register == "samples"
    assert isinstance(action.target.index, ast.NameExpr)
    assert action.target.index.name == "index"
    assert action.target.origin is not None


def test_rule_element_update_accepts_a_compile_time_index() -> None:
    module = parse("""
        module StaticVectorWrite {
          clock clk reset rst in fire:bit in value:u8 out y:u8
          reg samples:vec<4,u8>=generate(i in 0..4) 0
          when fire { samples[2] <- value }
          y=samples[2]
        }
    """)
    target = module.rules[0].actions[0].target
    assert isinstance(target, ast.IndexedAssignmentTarget)
    assert isinstance(target.index, ast.NumberExpr)
    assert target.index.value == 2
