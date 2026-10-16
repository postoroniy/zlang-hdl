# SPDX-License-Identifier: Apache-2.0
"""Exact word-level arithmetic cones read from Yosys's actual RTL netlist.

Unknown cells are retained as connected cut *inputs* of each rewrite, never
replaced or assumed. Sequential/control cells and formal checks stay intact.
"""

from __future__ import annotations

from dataclasses import dataclass

import z3

from zlang.formal_bitvector_rewrite import ArithmeticProofUnavailable


ARITHMETIC_CELLS = frozenset(("$add", "$sub", "$mul", "$neg", "$pos"))


@dataclass(frozen=True)
class RtlConeLeaf:
    name: str
    bits: tuple[int | str, ...]
    expression: z3.BitVecRef


def _parameter(cell, name):
    value = cell["parameters"][name]
    if isinstance(value, str) and value and set(value) <= {"0", "1"}:
        return int(value, 2)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise ArithmeticProofUnavailable(f"invalid RTL parameter {name}")


class RtlArithmeticCones:
    """Own one flattened module's exact driver index and retained word DAG."""

    def __init__(self, module, *, max_cells=4096, max_width=128, context=None):
        self.module = module
        self.max_width = max_width
        self.context = context if context is not None else z3.Context()
        cells = module["cells"]
        if len(cells) > max_cells:
            raise ArithmeticProofUnavailable(
                "RTL arithmetic module cell bound exceeded"
            )
        self._drivers = {}
        self._memo = {}
        self._active = set()
        self._leaves = {}
        self._sources = {}
        for name, port in sorted(module["ports"].items()):
            if port["direction"] == "input":
                self._source(("port", name), tuple(port["bits"]))
        for name, cell in sorted(cells.items()):
            for port, direction in cell["port_directions"].items():
                if direction == "output":
                    self._source(("cell", name, port), tuple(cell["connections"][port]))

    @property
    def leaves(self):
        return tuple(self._leaves.values())

    def _source(self, identity, bits):
        self._sources[identity] = bits
        for index, bit in enumerate(bits):
            if isinstance(bit, int):
                if bit in self._drivers:
                    raise ArithmeticProofUnavailable(
                        "RTL arithmetic has overlapping drivers"
                    )
                self._drivers[bit] = (identity, index)

    def roots(self):
        cells = self.module["cells"]
        consumed = set()
        for cell in cells.values():
            if cell["type"] in ARITHMETIC_CELLS:
                for port, direction in cell["port_directions"].items():
                    if direction != "input":
                        continue
                    for bit in cell["connections"][port]:
                        producer = self._drivers.get(bit)
                        if producer is not None and producer[0][0] == "cell":
                            consumed.add(producer[0][1])
        return tuple(
            name
            for name, cell in sorted(cells.items())
            if cell["type"] in ARITHMETIC_CELLS and name not in consumed
        )

    def _word(self, identity):
        if identity in self._memo:
            return self._memo[identity]
        if identity in self._active:
            raise ArithmeticProofUnavailable("cyclic RTL arithmetic cone")
        if len(self._active) >= 128:
            raise ArithmeticProofUnavailable("RTL arithmetic cone depth bound exceeded")
        self._active.add(identity)
        bits = self._sources[identity]
        if not 1 <= len(bits) <= self.max_width:
            raise ArithmeticProofUnavailable("RTL arithmetic word width bound exceeded")
        if (
            identity[0] != "cell"
            or self.module["cells"][identity[1]]["type"] not in ARITHMETIC_CELLS
        ):
            leaf = self._leaves.get(bits)
            if leaf is None:
                if len(self._leaves) >= 16:
                    raise ArithmeticProofUnavailable(
                        "RTL arithmetic input bound exceeded"
                    )
                name = f"v{len(self._leaves)}"
                leaf = RtlConeLeaf(
                    name, bits, z3.BitVec(name, len(bits), ctx=self.context)
                )
                self._leaves[bits] = leaf
            result = leaf.expression
        else:
            cell = self.module["cells"][identity[1]]
            if identity[2] != "Y" or len(bits) != _parameter(cell, "Y_WIDTH"):
                raise ArithmeticProofUnavailable("invalid RTL arithmetic result width")
            width = len(bits)
            signed = bool(_parameter(cell, "A_SIGNED")) and (
                cell["type"] in ("$neg", "$pos") or bool(_parameter(cell, "B_SIGNED"))
            )

            def operand(port):
                raw = tuple(cell["connections"][port])
                if len(raw) != _parameter(cell, f"{port}_WIDTH"):
                    raise ArithmeticProofUnavailable(
                        "invalid RTL arithmetic operand width"
                    )
                value = self.vector(raw)
                if value.size() > width:
                    return z3.Extract(width - 1, 0, value)
                if value.size() < width:
                    return (z3.SignExt if signed else z3.ZeroExt)(
                        width - value.size(), value
                    )
                return value

            a = operand("A")
            if cell["type"] in ("$neg", "$pos"):
                result = -a if cell["type"] == "$neg" else a
            else:
                b = operand("B")
                result = {
                    "$add": lambda: a + b,
                    "$sub": lambda: a - b,
                    "$mul": lambda: a * b,
                }[cell["type"]]()
        self._active.remove(identity)
        self._memo[identity] = result
        return result

    def root_expression(self, name):
        return self._word(("cell", name, "Y"))

    def vector(self, bits):
        if not bits or len(bits) > self.max_width:
            raise ArithmeticProofUnavailable("invalid RTL arithmetic connection width")
        first = bits[0]
        if isinstance(first, int):
            if first not in self._drivers:
                raise ArithmeticProofUnavailable("undriven RTL arithmetic bit")
            identity, offset = self._drivers[first]
            source = self._sources[identity]
            count = 1
            while (
                count < len(bits)
                and offset + count < len(source)
                and bits[count] == source[offset + count]
            ):
                count += 1
            word = self._word(identity)
            low = (
                word
                if offset == 0 and count == word.size()
                else z3.Extract(offset + count - 1, offset, word)
            )
            if count == len(bits):
                return low
            upper = bits[count:]
            if all(bit == bits[count - 1] for bit in upper):
                return z3.SignExt(len(upper), low)
            if all(bit == "0" for bit in upper):
                return z3.ZeroExt(len(upper), low)
            return z3.Concat(self.vector(upper), low)
        if first not in ("0", "1"):
            raise ArithmeticProofUnavailable("undefined bit in RTL arithmetic cone")
        count = 1
        while count < len(bits) and bits[count] in ("0", "1"):
            count += 1
        low = z3.BitVecVal(
            int("".join(reversed(bits[:count])), 2), count, ctx=self.context
        )
        return low if count == len(bits) else z3.Concat(self.vector(bits[count:]), low)
