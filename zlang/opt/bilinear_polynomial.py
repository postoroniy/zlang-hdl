# SPDX-License-Identifier: Apache-2.0
"""Independent bounded exact-integer interpretation of joint value terms.

No search, e-graph execution, target model or nominal type knowledge belongs
here. Every intermediate is checked, including lossless narrowing after
algebraic cancellation. Non-exact arithmetic and conversion barriers reject.
"""

from __future__ import annotations

from dataclasses import dataclass

from zlang.ir import expressions as e
from zlang.ir.module import Assignment, Module, Port, PortDirection
from zlang.ir.types import BitsType, SIntType
from zlang.opt.rewrite_model import Term, term_to_expression, term_from_nodes


@dataclass(frozen=True)
class BilinearPolynomial:
    inputs: tuple[e.InputRef, ...]
    components: tuple[tuple[tuple[tuple[str, ...], int], ...], ...]
    widths: tuple[int, ...]

    @property
    def signature(self) -> str:
        return repr(
            (
                "exact-bilinear-v1",
                tuple((item.name, item.type.width) for item in self.inputs),
                self.widths,
                self.components,
            )
        )


def expression_term(expression: e.Expression) -> Term:
    from zlang.candidate_expression import candidate_input_refs
    from zlang.opt.module_lowering import lower

    inputs = candidate_input_refs(expression)
    output = Port(PortDirection.OUTPUT, "__joint_result", expression.type)
    module = Module(
        "JointValue",
        (
            *tuple(
                Port(PortDirection.INPUT, name, type_) for name, type_ in inputs.items()
            ),
            output,
        ),
        (Assignment(output, expression),),
    )
    canonical = lower(module)
    return term_from_nodes(canonical.expressions, canonical.assignments[0].expression)


def read_bilinear(root: Term) -> BilinearPolynomial:
    value = term_to_expression(root)
    if not isinstance(value, e.Concat) or not 2 <= len(value.operands) <= 4:
        raise ValueError("bilinear certificate requires 2..4 joint components")
    inputs: dict[str, e.InputRef] = {}
    memo: dict[int, tuple[e.Expression, dict[tuple[str, ...], int]]] = {}

    def bounds(poly):
        lower = upper = 0
        for monomial, coefficient in poly.items():
            low = high = 1
            for name in monomial:
                width = inputs[name].type.width
                minimum, maximum = -(1 << (width - 1)), (1 << (width - 1)) - 1
                products = (
                    low * minimum,
                    low * maximum,
                    high * minimum,
                    high * maximum,
                )
                low, high = min(products), max(products)
            low, high = sorted((coefficient * low, coefficient * high))
            lower += low
            upper += high
        return lower, upper

    def polynomial(node):
        if id(node) in memo:
            return memo[id(node)][1]
        if len(memo) >= 96:
            raise ValueError("bilinear certificate exceeds 96 nodes")
        if not isinstance(node.type, SIntType):
            raise ValueError("bilinear arithmetic requires signed integers")
        if isinstance(node, e.InputRef):
            if not 1 <= node.type.width <= 16:
                raise ValueError("bilinear input width is outside 1..16")
            previous = inputs.setdefault(node.name, node)
            if previous.type != node.type or len(inputs) > 6:
                raise ValueError("bilinear inputs exceed the typed input bound")
            result = {(node.name,): 1}
        elif isinstance(node, e.Constant):
            result = {(): node.value}
        elif isinstance(node, (e.Extend, e.Truncate)):
            result = polynomial(node.expression)
        elif isinstance(node, (e.Add, e.Binary)):
            left, right = polynomial(node.left), polynomial(node.right)
            if (
                isinstance(node, e.Binary)
                and node.operator is e.BinaryOperator.MULTIPLY
            ):
                result = {}
                for a, ac in left.items():
                    for b, bc in right.items():
                        monomial = tuple(sorted((*a, *b)))
                        if len(monomial) > 2:
                            raise ValueError(
                                "bilinear equality excludes degree above two"
                            )
                        result[monomial] = result.get(monomial, 0) + ac * bc
            else:
                if (
                    isinstance(node, e.Binary)
                    and node.operator is not e.BinaryOperator.SUBTRACT
                ):
                    raise ValueError("unsupported operator in bilinear certificate")
                sign = 1 if isinstance(node, e.Add) else -1
                result = dict(left)
                for monomial, coefficient in right.items():
                    result[monomial] = result.get(monomial, 0) + sign * coefficient
        else:
            raise ValueError("unsupported value in bilinear certificate")
        result = {
            key: coefficient for key, coefficient in result.items() if coefficient
        }
        if len(result) > 16:
            raise ValueError("bilinear polynomial exceeds 16 monomials")
        low, high = bounds(result)
        if low < -(1 << (node.type.width - 1)) or high >= 1 << (node.type.width - 1):
            raise ValueError("bilinear rewrite loses finite-width signed information")
        memo[id(node)] = (node, result)
        return result

    components, widths = [], []
    for component in value.operands:
        if not (
            isinstance(component, e.Bitcast)
            and isinstance(component.type, BitsType)
            and component.type.width == component.expression.type.width
        ):
            raise ValueError("joint result requires an exact representation boundary")
        poly = polynomial(component.expression)
        if any(
            len(key) != 2 or key[0] == key[1] or abs(coefficient) > 4
            for key, coefficient in poly.items()
        ):
            raise ValueError("search requires homogeneous bilinear components")
        components.append(tuple(sorted(poly.items())))
        widths.append(component.type.width)
    return BilinearPolynomial(
        tuple(inputs[name] for name in sorted(inputs)), tuple(components), tuple(widths)
    )
