# SPDX-License-Identifier: Apache-2.0
"""Bounded, solver-checked widening of word-level RTL arithmetic.

Only unconditional bit-vector equalities may cross a signed/unsigned resize.
An overflowing or unknown lemma is never admitted. This owner knows neither
compiler expressions nor bilinear search certificates.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from functools import reduce
import operator

import z3


class ArithmeticProofUnavailable(ValueError):
    """The exact RTL cone cannot be normalized within this proof budget."""


@dataclass(frozen=True)
class BitvectorLemma:
    source: str
    replacement: str
    query: str


_ARITHMETIC = frozenset((z3.Z3_OP_BADD, z3.Z3_OP_BSUB, z3.Z3_OP_BMUL, z3.Z3_OP_BNEG))


def _arithmetic(kind, operands):
    if kind == z3.Z3_OP_BNEG:
        return -operands[0]
    operation = {
        z3.Z3_OP_BADD: operator.add,
        z3.Z3_OP_BSUB: operator.sub,
        z3.Z3_OP_BMUL: operator.mul,
    }[kind]
    return reduce(operation, operands)


class CheckedBitvectorRewriter:
    """Own verified substitutions and the work budget of one RTL proof."""

    def __init__(self, *, deadline: float, max_nodes: int = 4096, max_lemmas: int = 64):
        self.deadline = deadline
        self.max_nodes = max_nodes
        self.max_lemmas = max_lemmas
        self.lemmas: list[BitvectorLemma] = []
        self._memo: dict[int, tuple[z3.ExprRef, z3.ExprRef]] = {}
        self._rewrites: dict[int, tuple[z3.ExprRef, z3.ExprRef]] = {}
        self._context = None

    def remaining_ms(self) -> int:
        remaining = int((self.deadline - time.monotonic()) * 1000)
        if remaining < 1:
            raise ArithmeticProofUnavailable("RTL arithmetic proof budget exhausted")
        return remaining

    def normalize(self, expression: z3.ExprRef) -> z3.ExprRef:
        if self._context is None:
            self._context = expression.ctx
        if expression.ctx is not self._context:
            raise ArithmeticProofUnavailable(
                "one RTL rewrite owner requires one Z3 context"
            )
        rewritten = self._visit(expression)
        # These are Z3's exact finite-bitvector rewrites, not integer relaxation.
        return z3.simplify(rewritten, som=True, bv_extract_prop=True)

    def _visit(self, expression: z3.ExprRef, depth: int = 0) -> z3.ExprRef:
        if depth > 256:
            raise ArithmeticProofUnavailable(
                "RTL arithmetic normalization depth bound exceeded"
            )
        key = expression.get_id()
        if key in self._memo:
            return self._memo[key][1]
        self.remaining_ms()
        if len(self._memo) >= self.max_nodes:
            raise ArithmeticProofUnavailable(
                "RTL arithmetic normalization node bound exceeded"
            )
        # Normalize the actual outer carrier before Z3 turns zero extension
        # into concat. This retains the exact narrowing/overflow boundary.
        replacement = self._outer_widening(expression)
        if replacement is not None:
            value = self._visit(replacement, depth + 1)
            if len(self._memo) >= self.max_nodes:
                raise ArithmeticProofUnavailable("RTL arithmetic normalization node bound exceeded")
            self._memo[key] = (expression, value)
            return value
        children = tuple(
            self._visit(child, depth + 1) for child in expression.children()
        )
        if len(self._memo) >= self.max_nodes:
            raise ArithmeticProofUnavailable(
                "RTL arithmetic normalization node bound exceeded"
            )
        value = expression.decl()(*children) if children else expression
        # Reduce low-bit extraction before considering widening. This is a
        # solver-owned modular identity and does not require a no-overflow claim.
        value = z3.simplify(value, elim_sign_ext=False, bv_extract_prop=True)
        kind = value.decl().kind()
        if (kind == z3.Z3_OP_CONCAT and value.num_args() == 2
                and z3.is_bv_value(value.arg(0)) and value.arg(0).as_long() == 0):
            # Recover the same exact unsigned carrier, not a range assumption.
            value = z3.ZeroExt(value.arg(0).size(), value.arg(1))
        replacement = self._outer_widening(value)
        if replacement is not None:
            value = self._visit(replacement, depth + 1)
        if len(self._memo) >= self.max_nodes:
            raise ArithmeticProofUnavailable("RTL arithmetic normalization node bound exceeded")
        self._memo[key] = (expression, value)
        return value

    def _outer_widening(self, value):
        kind = value.decl().kind()
        if kind not in (z3.Z3_OP_SIGN_EXT, z3.Z3_OP_ZERO_EXT):
            return None
        child = value.arg(0)
        extend = z3.SignExt if kind == z3.Z3_OP_SIGN_EXT else z3.ZeroExt
        if child.decl().kind() == z3.Z3_OP_CONCAT:
            # Extend only the connected high field, never a lower field.
            parts = child.children()
            candidate = z3.Concat(extend(value.size()-child.size(), parts[0]), *parts[1:])
        elif child.decl().kind() == kind:
            candidate = extend(value.size()-child.arg(0).size(), child.arg(0))
        elif child.decl().kind() in _ARITHMETIC:
            operands = tuple(
                z3.simplify(extend(value.size()-item.size(), item), elim_sign_ext=False)
                for item in child.children()
            )
            candidate = _arithmetic(child.decl().kind(), operands)
        else:
            return None
        return self._checked_widening(value, candidate)

    def _checked_widening(self, source, replacement):
        key = source.get_id()
        if key in self._rewrites:
            return self._rewrites[key][1]
        if len(self.lemmas) >= self.max_lemmas:
            raise ArithmeticProofUnavailable("RTL widening lemma bound exceeded")
        solver = z3.SolverFor("QF_BV", ctx=source.ctx)
        solver.set(timeout=min(3000, self.remaining_ms()))
        solver.add(source != replacement)
        query = solver.sexpr() + "\n(check-sat)\n"
        if solver.check() != z3.unsat:
            return None
        self.lemmas.append(BitvectorLemma(source.sexpr(), replacement.sexpr(), query))
        self._rewrites[key] = (source, replacement)
        return replacement
