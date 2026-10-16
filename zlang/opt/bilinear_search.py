# SPDX-License-Identifier: Apache-2.0
"""Deterministic bounded rank-one exploration of small bilinear value maps.

Only integer coefficient tensors are examined. Nominal types, field names,
targets, DSPs, latency and protocols are deliberately absent. The returned
values still require independent certificate admission by the e-graph owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations, product

from zlang.ir import expressions as e
from zlang.ir.expression_graph import ExpressionDagIndex
from zlang.ir.types import BitsType, SIntType
from zlang.opt.bilinear_polynomial import expression_term, read_bilinear
from zlang.ir.joint_values import arithmetic
from zlang.opt.rewrite_model import Term, term_to_expression


@dataclass(frozen=True)
class BilinearSearchLimits:
    max_basis_tests: int = 4096
    max_alternatives: int = 8
    max_rank: int = 3

    def __post_init__(self):
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= ceiling
            for value, ceiling in (
                (self.max_basis_tests, 4096),
                (self.max_alternatives, 8),
                (self.max_rank, 3),
            )
        ):
            raise ValueError("bilinear search limits exceed deterministic hard bounds")


@dataclass(frozen=True)
class BilinearSearchResult:
    alternatives: tuple[Term, ...]
    basis_tests: int
    truncated: bool


def _partition(polynomial):
    adjacency = {item.name: set() for item in polynomial.inputs}
    for component in polynomial.components:
        for (left, right), _ in component:
            adjacency[left].add(right)
            adjacency[right].add(left)
    colors = {}
    for start in sorted(adjacency):
        if start in colors:
            continue
        colors[start] = 0
        queue = [start]
        for node in queue:
            for other in sorted(adjacency[node]):
                expected = 1 - colors[node]
                if other in colors and colors[other] != expected:
                    return None
                if other not in colors:
                    colors[other] = expected
                    queue.append(other)
    left = tuple(name for name in sorted(colors) if colors[name] == 0)
    right = tuple(name for name in sorted(colors) if colors[name] == 1)
    if not left or not right or max(len(left), len(right)) > 3:
        return None
    return left, right


def _linear_forms(size):
    values = [
        value
        for value in product((-1, 0, 1), repeat=size)
        if any(value) and next(item for item in value if item) == 1
    ]
    return tuple(sorted(values, key=lambda item: (sum(abs(x) for x in item), item)))


def _linear_value(terms, coefficients):
    selected = [
        (value, coefficient)
        for value, coefficient in zip(terms, coefficients, strict=True)
        if coefficient
    ]
    first, sign = selected[0]
    current = first if sign == 1 else arithmetic("-", e.Constant(0, first.type), first)
    for value, coefficient in selected[1:]:
        current = arithmetic("+" if coefficient == 1 else "-", current, value)
    return current


def _materialize(polynomial, partition, basis, coefficients):
    inputs = {item.name: item for item in polynomial.inputs}
    left, right = (tuple(inputs[name] for name in side) for side in partition)
    products = tuple(
        arithmetic("*", _linear_value(left, a), _linear_value(right, b))
        for a, b, _ in basis
    )
    outputs = []
    for weights, width in zip(coefficients, polynomial.widths, strict=True):
        value = _linear_value(products, weights)
        type_ = SIntType(width)
        if value.type.width != width:
            value = (e.Truncate if value.type.width > width else e.Extend)(value, type_)
        outputs.append(e.Bitcast(value, BitsType(width)))
    return e.Concat(tuple(outputs), BitsType(sum(polynomial.widths)))


def explore_bilinear(
    source: Term, limits: BilinearSearchLimits = BilinearSearchLimits()
):
    try:
        polynomial = read_bilinear(source)
    except (TypeError, ValueError):
        return BilinearSearchResult((), 0, False)
    partition = _partition(polynomial)
    if partition is None or any(not component for component in polynomial.components):
        return BilinearSearchResult((), 0, False)
    source_products = sum(
        isinstance(item, e.Binary) and item.operator is e.BinaryOperator.MULTIPLY
        for item in ExpressionDagIndex((term_to_expression(source),))
    )
    left, right = partition
    monomials = tuple(tuple(sorted((a, b))) for a in left for b in right)
    targets = tuple(
        tuple(dict(component).get(key, 0) for key in monomials)
        for component in polynomial.components
    )
    factors = tuple(
        (a, b, tuple(x * y for x in a for y in b))
        for a in _linear_forms(len(left))
        for b in _linear_forms(len(right))
    )
    alternatives, tested = [], 0
    for rank in range(1, min(limits.max_rank, source_products - 1) + 1):
        for basis in combinations(factors, rank):
            if tested == limits.max_basis_tests:
                return BilinearSearchResult(tuple(alternatives), tested, True)
            tested += 1
            attainable = {}
            weights = sorted(
                product((-1, 0, 1), repeat=rank),
                key=lambda item: (sum(abs(x) for x in item), item),
            )
            for coefficients in weights:
                tensor = tuple(
                    sum(coefficients[k] * basis[k][2][i] for k in range(rank))
                    for i in range(len(monomials))
                )
                attainable.setdefault(tensor, coefficients)
            if not all(target in attainable for target in targets):
                continue
            coefficients = tuple(attainable[target] for target in targets)
            if any(not any(row[k] for row in coefficients) for k in range(rank)):
                continue
            candidate = expression_term(
                _materialize(polynomial, partition, basis, coefficients)
            )
            # Independent width/range checking, not integer algebra alone.
            if read_bilinear(candidate).signature != polynomial.signature:
                raise ValueError("bilinear materialization changed the exact value")
            alternatives.append(candidate)
            if len(alternatives) == limits.max_alternatives:
                return BilinearSearchResult(tuple(alternatives), tested, True)
    return BilinearSearchResult(tuple(alternatives), tested, False)
