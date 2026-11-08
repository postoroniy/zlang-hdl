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
    try:
        value = cell["parameters"][name]
    except (KeyError, TypeError) as error:
        raise ArithmeticProofUnavailable(f"missing RTL parameter {name}") from error
    if isinstance(value, str) and value and set(value) <= {"0", "1"}:
        return int(value, 2)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise ArithmeticProofUnavailable(f"invalid RTL parameter {name}")


class RtlArithmeticCones:
    """Own one flattened module's exact driver index and retained word DAG."""

    def __init__(
        self,
        module,
        *,
        max_cells=4096,
        max_width=128,
        context=None,
        logic=False,
        input_words=(),
    ):
        self.logic = logic
        self.input_words = frozenset(tuple(bits) for bits in input_words)
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
        consumed, boundaries = set(), set()
        for cell in cells.values():
            for port, direction in cell["port_directions"].items():
                if direction != "input":
                    continue
                for bit in cell["connections"][port]:
                    producer = self._drivers.get(bit)
                    if producer is not None and producer[0][0] == "cell":
                        (
                            consumed if cell["type"] in ARITHMETIC_CELLS else boundaries
                        ).add(producer[0][1])
        for port in self.module["ports"].values():
            if port["direction"] == "output":
                for bit in port["bits"]:
                    producer = self._drivers.get(bit)
                    if producer is not None and producer[0][0] == "cell":
                        boundaries.add(producer[0][1])
        # A sum used by both more arithmetic and a sign/rounding predicate is
        # itself a boundary. Rewriting only terminal consumers leaves the
        # predicate connected to the original expensive cone. Both helpers
        # remain independently checked against the actual connected RTL.
        return tuple(
            name
            for name, cell in sorted(cells.items())
            if cell["type"] in ARITHMETIC_CELLS
            and (name not in consumed or name in boundaries)
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
            bits in self.input_words
            or identity[0] != "cell"
            or (
                self.module["cells"][identity[1]]["type"] not in ARITHMETIC_CELLS
                and not self.logic
            )
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
            if cell["type"] not in ARITHMETIC_CELLS:
                result = self._logic_word(cell, bits)
                self._active.remove(identity)
                self._memo[identity] = result
                return result
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

    def _logic_word(self, cell, bits):
        """Closed combinational subset for independently checked quantizers."""
        kind = cell["type"]
        ports = cell["connections"]
        width = len(bits)
        if kind == "$mux":
            if (
                len(ports["S"]) != 1
                or len(ports["A"]) != width
                or len(ports["B"]) != width
            ):
                raise ArithmeticProofUnavailable("invalid quantizer mux shape")
            return z3.If(
                self.vector(tuple(ports["S"])) == 1,
                self.vector(tuple(ports["B"])),
                self.vector(tuple(ports["A"])),
            )
        if width != _parameter(cell, "Y_WIDTH"):
            raise ArithmeticProofUnavailable("invalid quantizer result width")
        values = []
        for port in ("A", "B"):
            if port in ports:
                if len(ports[port]) != _parameter(cell, port + "_WIDTH"):
                    raise ArithmeticProofUnavailable("invalid quantizer operand width")
                values.append(self.vector(tuple(ports[port])))

        def boolean(value):
            return z3.If(
                value,
                z3.BitVecVal(1, width, ctx=self.context),
                z3.BitVecVal(0, width, ctx=self.context),
            )

        if kind in {"$logic_not", "$reduce_or", "$reduce_bool", "$reduce_and"}:
            a = values[0]
            return boolean(
                a == 0
                if kind == "$logic_not"
                else a == (1 << a.size()) - 1
                if kind == "$reduce_and"
                else a != 0
            )
        if kind in {"$logic_and", "$logic_or"}:
            a, b = values
            return boolean(
                z3.And(a != 0, b != 0)
                if kind == "$logic_and"
                else z3.Or(a != 0, b != 0)
            )
        compares = {"$eq", "$ne", "$lt", "$le", "$gt", "$ge"}
        if kind in compares | {"$and", "$or", "$xor", "$not"}:
            signed = bool(_parameter(cell, "A_SIGNED")) and (
                len(values) == 1 or bool(_parameter(cell, "B_SIGNED"))
            )
            carrier = max(v.size() for v in values) if kind in compares else width

            def resize(value):
                if value.size() > carrier:
                    return z3.Extract(carrier - 1, 0, value)
                return (z3.SignExt if signed else z3.ZeroExt)(
                    carrier - value.size(), value
                )

            values = [resize(v) for v in values]
            a = values[0]
            if kind == "$not":
                return ~a
            b = values[1]
            if kind == "$and":
                return a & b
            if kind == "$or":
                return a | b
            if kind == "$xor":
                return a ^ b
            relation = {
                "$eq": lambda: a == b,
                "$ne": lambda: a != b,
                "$lt": lambda: a < b if signed else z3.ULT(a, b),
                "$le": lambda: a <= b if signed else z3.ULE(a, b),
                "$gt": lambda: a > b if signed else z3.UGT(a, b),
                "$ge": lambda: a >= b if signed else z3.UGE(a, b),
            }[kind]()
            return boolean(relation)
        if kind in {"$shr", "$sshr", "$shl", "$sshl"}:
            a, b = values
            signed = bool(_parameter(cell, "A_SIGNED"))
            common = max(width, a.size())
            a = (z3.SignExt if signed else z3.ZeroExt)(common - a.size(), a)
            too_big = (
                z3.UGE(b, z3.BitVecVal(common, b.size(), ctx=self.context))
                if (1 << b.size()) > common
                else z3.BoolVal(False, ctx=self.context)
            )
            amount = (
                z3.Extract(common - 1, 0, b)
                if b.size() > common
                else z3.ZeroExt(common - b.size(), b)
            )
            arithmetic_right = signed and kind == "$sshr"
            result = (
                (a >> amount if arithmetic_right else z3.LShR(a, amount))
                if kind in {"$shr", "$sshr"}
                else a << amount
            )
            fill = (
                z3.SignExt(common - 1, z3.Extract(common - 1, common - 1, a))
                if arithmetic_right
                else z3.BitVecVal(0, common, ctx=self.context)
            )
            return z3.Extract(width - 1, 0, z3.If(too_big, fill, result))
        raise ArithmeticProofUnavailable(f"unqualified quantizer cell {kind}")

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
