"""Lower compiler simulation semantics to a language-neutral bit-vector VM.

This module is the architectural boundary between ZLang and the native
runtime.  Everything above this boundary may know about ZLang types, packing,
rules, resets, memories and scheduling.  Everything below it sees only packed
bit vectors, storage slots and explicit edge effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class PrimitiveLoweringError(ValueError):
    """The compiler produced a semantic plan that cannot be made primitive."""


PRIMITIVE_OPS = frozenset(
    {
        "constant",
        "load_input",
        "load_state",
        "load_event",
        "load_memory",
        "add",
        "sub",
        "mul",
        "and",
        "or",
        "xor",
        "not",
        "shl",
        "lshr",
        "ashr",
        "eq",
        "ult",
        "ule",
        "slt",
        "sle",
        "select",
        "extract_bits",
        "insert_bits",
        "concat_bits",
    }
)


@dataclass
class _Builder:
    semantic_nodes: list[dict[str, Any]]
    max_nodes: int
    semantic_memories: dict[str, dict[str, Any]] = field(default_factory=dict)
    semantic_fifos: dict[str, dict[str, Any]] = field(default_factory=dict)
    nodes: list[dict[str, Any]] = field(default_factory=list)
    lowered: dict[int, int] = field(default_factory=dict)

    def emit(
        self,
        op: str,
        width: int,
        operands: tuple[int, ...] = (),
        attributes: dict[str, Any] | None = None,
        origins: list[dict[str, Any]] | None = None,
    ) -> int:
        if op not in PRIMITIVE_OPS:
            raise PrimitiveLoweringError(f"unknown primitive operation '{op}'")
        if len(self.nodes) >= self.max_nodes:
            raise PrimitiveLoweringError(
                "primitive simulation plan exceeds "
                f"the {self.max_nodes}-node compilation bound"
            )
        identifier = len(self.nodes)
        self.nodes.append(
            {
                "id": identifier,
                "op": op,
                "width": width,
                "operands": list(operands),
                "attributes": attributes or {},
                "origins": origins or [],
            }
        )
        return identifier

    def width(self, node: int) -> int:
        return int(self.nodes[node]["width"])

    def constant(self, value: int, width: int) -> int:
        value &= (1 << width) - 1
        limbs = [(value >> offset) & ((1 << 64) - 1) for offset in range(0, width, 64)]
        return self.emit("constant", width, attributes={"limbs": limbs})

    def event(self, clock: str) -> int:
        return self.emit("load_event", 1, attributes={"name": clock})

    def unary(self, op: str, value: int, width: int | None = None) -> int:
        return self.emit(op, width or self.width(value), (value,))

    def binary(self, op: str, left: int, right: int, width: int) -> int:
        return self.emit(op, width, (left, right))

    def resize(self, value: int, width: int, *, signed: bool = False) -> int:
        source_width = self.width(value)
        if source_width == width:
            return value
        if source_width > width:
            return self.extract(value, self.constant(0, 1), width)
        high_width = width - source_width
        if signed:
            sign = self.extract(value, self.constant(source_width - 1, 32), 1)
            high = self.select(
                sign,
                self.constant((1 << high_width) - 1, high_width),
                self.constant(0, high_width),
                high_width,
            )
        else:
            high = self.constant(0, high_width)
        return self.concat((high, value), width)

    def truthy(self, value: int) -> int:
        return self.unary(
            "not", self.binary("eq", value, self.constant(0, self.width(value)), 1), 1
        )

    def select(self, condition: int, yes: int, no: int, width: int) -> int:
        return self.emit("select", width, (self.truthy(condition), yes, no))

    def extract(self, value: int, offset: int, width: int) -> int:
        return self.emit("extract_bits", width, (value, offset))

    def insert(self, base: int, value: int, offset: int) -> int:
        return self.emit("insert_bits", self.width(base), (base, value, offset))

    def concat(self, values: tuple[int, ...], width: int) -> int:
        return self.emit(
            "concat_bits",
            width,
            values,
            {"operand_widths": [self.width(value) for value in values]},
        )

    def merge_masked_value(
        self,
        old: int,
        new: int,
        mask: int | None,
        *,
        width: int,
        mask_width: int | None,
    ) -> int:
        if mask is None:
            return new
        merged = old
        for byte in range(int(mask_width or 0)):
            byte_width = min(8, width - byte * 8)
            if byte_width <= 0:
                break
            bit = self.extract(mask, self.constant(byte, 32), 1)
            replacement = self.extract(
                new, self.constant(byte * 8, 32), byte_width
            )
            updated = self.insert(
                merged, replacement, self.constant(byte * 8, 32)
            )
            merged = self.select(bit, updated, merged, width)
        return merged

    def _type(self, node: dict[str, Any]) -> dict[str, Any]:
        return node["type"]

    def _semantic_width(self, identifier: int) -> int:
        return int(self.semantic_nodes[identifier]["type"]["width"])

    def _signed(self, type_: dict[str, Any]) -> bool:
        return type_.get("kind") in {"sint", "fixed"}

    def _attr_type(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or not isinstance(
            value.get("hardware_type"), dict
        ):
            raise PrimitiveLoweringError(
                "semantic node has invalid hardware-type metadata"
            )
        return value["hardware_type"]

    def lower(self, identifier: int) -> int:  # noqa: C901, PLR0912, PLR0915
        if identifier in self.lowered:
            return self.lowered[identifier]
        node = self.semantic_nodes[identifier]
        op = node["op"]
        width = int(node["type"]["width"])
        attrs = node["attributes"]
        operands = tuple(self.lower(item) for item in node["operands"])
        origins = node.get("origins", [])

        if op == "input":
            result = self.emit(
                "load_input", width, attributes={"name": attrs["name"]}, origins=origins
            )
        elif op == "register_ref":
            result = self.emit(
                "load_state", width, attributes={"name": attrs["name"]}, origins=origins
            )
        elif op == "constant":
            result = self.emit(
                "constant", width, attributes={"limbs": attrs["limbs"]}, origins=origins
            )
        elif op == "memory_port_read":
            memory_name = attrs.get("memory")
            memory = self.semantic_memories.get(memory_name)
            if memory is None:
                raise PrimitiveLoweringError(
                    f"memory read references unknown memory '{memory_name}'"
                )
            address = operands[0]
            reset = (
                self.emit(
                    "load_input",
                    1,
                    attributes={"name": f"$reset:{memory['reset']}"},
                )
                if memory.get("reset") is not None
                else self.constant(0, 1)
            )
            old = self.emit(
                "load_memory", width, (address,), {"memory": memory_name}, origins
            )
            result = old
            if memory["collision"] == "write_first":
                ports = {item["name"]: item for item in memory["ports"]}
                priority = memory["write_priority"] or [
                    item["name"]
                    for item in memory["ports"]
                    if item["write_enable"] is not None
                ]
                # Reverse nested selects so the first priority entry wins.
                for writer_name in reversed(priority):
                    writer = ports[writer_name]
                    if writer["write_enable"] is None:
                        continue
                    write_address = self.lower(writer["write_address"])
                    write_enable = self.truthy(self.lower(writer["write_enable"]))
                    write_enable = self.binary(
                        "and", self.unary("not", reset, 1), write_enable, 1
                    )
                    same_address = self.binary("eq", address, write_address, 1)
                    active = self.binary("and", write_enable, same_address, 1)
                    new = self.lower(writer["write_data"])
                    mask_id = writer["write_mask"]
                    merged = self.merge_masked_value(
                        old,
                        new,
                        self.lower(mask_id) if mask_id is not None else None,
                        width=width,
                        mask_width=memory["write_mask_width"],
                    )
                    result = self.select(active, merged, result, width)
            if memory["read_data_reset"] == "clear":
                result = self.select(reset, self.constant(0, width), result, width)
        elif op == "fifo_ref":
            fifo_name = attrs.get("fifo")
            signal = attrs.get("signal")
            fifo = self.semantic_fifos.get(fifo_name)
            if fifo is None:
                raise PrimitiveLoweringError(
                    f"FIFO reference names unknown FIFO '{fifo_name}'"
                )
            count = self.emit(
                "load_state",
                int(fifo["count_width"]),
                attributes={"name": fifo["count"]},
            )
            read_pointer = self.emit(
                "load_state",
                int(fifo["pointer_width"]),
                attributes={"name": fifo["read_pointer"]},
            )
            empty = self.binary(
                "eq", count, self.constant(0, self.width(count)), 1
            )
            full = self.binary(
                "eq",
                count,
                self.constant(int(fifo["depth"]), self.width(count)),
                1,
            )
            reset = (
                self.emit(
                    "load_input", 1, attributes={"name": f"$reset:{fifo['reset']}"}
                )
                if fifo["reset"] is not None
                else self.constant(0, 1)
            )
            not_reset = self.unary("not", reset, 1)
            if signal in {"front", "data"}:
                front = self.emit(
                    "load_memory",
                    width,
                    (read_pointer,),
                    {"memory": fifo["memory"]},
                    origins,
                )
                result = self.select(empty, self.constant(0, width), front, width)
            elif signal == "count":
                result = self.resize(count, width)
            elif signal == "empty":
                result = empty
            elif signal == "full":
                result = full
            elif signal == "valid":
                result = self.binary("and", not_reset, self.unary("not", empty, 1), 1)
            elif signal == "ready":
                if fifo["scheduled"]:
                    pop = self.constant(0, 1)
                else:
                    pop_request = self.truthy(self.lower(int(fifo["pop"])))
                    pop = self.binary(
                        "and",
                        not_reset,
                        self.binary(
                            "and", pop_request, self.unary("not", empty, 1), 1
                        ),
                        1,
                    )
                result = self.binary(
                    "and",
                    not_reset,
                    self.binary("or", self.unary("not", full, 1), pop, 1),
                    1,
                )
            elif signal == "push":
                result = (
                    self.constant(0, 1)
                    if fifo["scheduled"]
                    else self.truthy(self.lower(int(fifo["push"])))
                )
            elif signal == "pop":
                result = (
                    self.constant(0, 1)
                    if fifo["scheduled"]
                    else self.truthy(self.lower(int(fifo["pop"])))
                )
            elif signal == "overflow":
                if fifo["scheduled"]:
                    result = self.constant(0, 1)
                    self.lowered[identifier] = result
                    return result
                pop_request = self.truthy(self.lower(int(fifo["pop"])))
                pop = self.binary(
                    "and",
                    not_reset,
                    self.binary(
                        "and", pop_request, self.unary("not", empty, 1), 1
                    ),
                    1,
                )
                push_request = self.truthy(self.lower(int(fifo["push"])))
                result = self.binary(
                    "and",
                    not_reset,
                    self.binary(
                        "and", push_request, self.binary("and", full, self.unary("not", pop, 1), 1), 1
                    ),
                    1,
                )
            elif signal == "underflow":
                if fifo["scheduled"]:
                    result = self.constant(0, 1)
                    self.lowered[identifier] = result
                    return result
                pop_request = self.truthy(self.lower(int(fifo["pop"])))
                result = self.binary(
                    "and", not_reset, self.binary("and", pop_request, empty, 1), 1
                )
            else:
                raise PrimitiveLoweringError(
                    f"FIFO '{fifo_name}' has unknown signal '{signal}'"
                )
        elif op == "add":
            operand_type = self.semantic_nodes[node["operands"][0]]["type"]
            left = self.resize(operands[0], width, signed=self._signed(operand_type))
            right = self.resize(operands[1], width, signed=self._signed(operand_type))
            result = self.binary("add", left, right, width)
        elif op == "binary":
            operator = attrs["operator"]
            operand_type = self._attr_type(attrs["operand_type"])
            signed = self._signed(operand_type)
            operation = {
                "-": "sub",
                "*": "mul",
                "&": "and",
                "|": "or",
                "^": "xor",
                "<<": "shl",
                ">>": "ashr" if signed else "lshr",
                "==": "eq",
                "<": "slt" if signed else "ult",
                "<=": "sle" if signed else "ule",
            }.get(operator)
            left, right = operands
            if operator in {"-", "*", "&", "|", "^", "<<", ">>"}:
                left = self.resize(left, width, signed=signed)
            if operator in {"-", "*", "&", "|", "^"}:
                right = self.resize(right, width, signed=signed)
            if operator == "!=":
                result = self.unary("not", self.binary("eq", left, right, 1), 1)
            elif operator in {">", ">="}:
                reverse = "slt" if signed else "ult"
                if operator == ">=":
                    reverse = "sle" if signed else "ule"
                result = self.binary(reverse, right, left, 1)
            elif operation is None:
                raise PrimitiveLoweringError(
                    f"unsupported semantic binary operator '{operator}'"
                )
            else:
                result = self.binary(operation, left, right, width)
        elif op == "extend":
            source_type = self.semantic_nodes[node["operands"][0]]["type"]
            result = self.resize(operands[0], width, signed=self._signed(source_type))
        elif op in {"truncate", "enum_encode", "bitcast", "pack", "unpack", "reshape"}:
            result = self.resize(operands[0], width)
        elif op == "fixed_convert":
            result = self._fixed_convert(node, operands[0])
        elif op == "mux":
            result = self.select(operands[0], operands[1], operands[2], width)
        elif op == "switch":
            selected = operands[-1]
            selector = operands[0]
            for key, value in reversed(
                tuple(zip(attrs["keys"], operands[1:-1], strict=True))
            ):
                condition = self.binary(
                    "eq", selector, self.constant(int(key), self.width(selector)), 1
                )
                selected = self.select(condition, value, selected, width)
            result = selected
        elif op == "enum_valid" or op == "enum_decode":
            enum_type = self._attr_type(attrs["enum_type"])
            source = operands[0]
            valid = self.constant(0, 1)
            for code in enum_type["codes"]:
                equal = self.binary(
                    "eq", source, self.constant(int(code), self.width(source)), 1
                )
                valid = self.binary("or", valid, equal, 1)
            result = (
                valid
                if op == "enum_valid"
                else self.select(valid, source, operands[1], width)
            )
        elif op == "union_construct":
            result = self._union_construct(node, operands)
        elif op == "union_tag":
            source_type = self.semantic_nodes[node["operands"][0]]["type"]
            result = self.extract(
                operands[0], self.constant(int(source_type["payload_width"]), 32), width
            )
        elif op == "union_field":
            result = self._union_field(node, operands[0])
        elif op == "slice":
            result = self.extract(
                operands[0], self.constant(int(attrs["lsb"]), 32), width
            )
        elif op == "concat":
            result = self.concat(operands, width)
        elif op in {"vector_concat", "generate", "map"}:
            result = self.concat(tuple(reversed(operands)), width)
        elif op == "dot":
            result = self.concat(tuple(reversed(operands[2:])), width)
        elif op == "reduce":
            result = self._reduce(node, operands[0])
        elif op == "vector_index":
            offset = int(attrs["index"]) * width
            result = self.extract(operands[0], self.constant(offset, 32), width)
        elif op == "runtime_index":
            index = self.resize(operands[1], 64)
            offset = self.binary("mul", index, self.constant(width, 64), 64)
            result = self.extract(operands[0], offset, width)
        elif op == "vector_update":
            element_width = self.width(operands[2])
            index = self.resize(operands[1], 64)
            offset = self.binary("mul", index, self.constant(element_width, 64), 64)
            result = self.insert(operands[0], operands[2], offset)
        elif op == "tuple_construct":
            result = self.concat(tuple(reversed(operands)), width)
        elif op == "tuple_project":
            source_type = self.semantic_nodes[node["operands"][0]]["type"]
            index = int(attrs["index"])
            offset = sum(int(item["width"]) for item in source_type["elements"][:index])
            result = self.extract(operands[0], self.constant(offset, 32), width)
        elif op == "struct_construct":
            result = self.concat(operands, width)
        elif op == "field":
            source_type = self.semantic_nodes[node["operands"][0]]["type"]
            offset = 0
            for field in reversed(source_type["fields"]):
                if field["name"] == attrs["field"]:
                    break
                offset += int(field["type"]["width"])
            else:
                raise PrimitiveLoweringError(f"unknown struct field '{attrs['field']}'")
            result = self.extract(operands[0], self.constant(offset, 32), width)
        elif op == "rom_lookup":
            selected = operands[1]
            address = operands[0]
            for index, value in enumerate(operands[2:], start=1):
                equal = self.binary(
                    "eq", address, self.constant(index, self.width(address)), 1
                )
                selected = self.select(equal, value, selected, width)
            result = selected
        else:
            raise PrimitiveLoweringError(
                f"unsupported semantic simulation operation '{op}'"
            )
        self.lowered[identifier] = result
        return result

    def _reduce(self, node: dict[str, Any], source: int) -> int:
        source_type = self.semantic_nodes[node["operands"][0]]["type"]
        element_width = int(source_type["element"]["width"])
        length = int(source_type["length"])
        width = int(node["type"]["width"])
        values = [
            self.resize(
                self.extract(
                    source, self.constant(index * element_width, 32), element_width
                ),
                width,
            )
            for index in range(length)
        ]
        operator = {"+": "add", "&": "and", "|": "or", "^": "xor"}[
            node["attributes"]["operator"]
        ]
        result = values[0]
        for value in values[1:]:
            result = self.binary(operator, result, value, width)
        return result

    def _union_construct(self, node: dict[str, Any], operands: tuple[int, ...]) -> int:
        type_ = node["type"]
        variant_name = node["attributes"]["variant"]
        for tag, variant in enumerate(type_["variants"]):
            if variant["name"] == variant_name:
                break
        else:
            raise PrimitiveLoweringError(
                f"unknown tagged-union variant '{variant_name}'"
            )
        payload_width = int(type_["payload_width"])
        fields = list(variant["fields"])
        used = sum(int(field["type"]["width"]) for field in fields)
        pieces = [self.constant(tag, int(type_["tag_width"]))]
        if payload_width > used:
            pieces.append(self.constant(0, payload_width - used))
        pieces.extend(operands)
        return self.concat(tuple(pieces), int(type_["width"]))

    def _union_field(self, node: dict[str, Any], source: int) -> int:
        source_type = self.semantic_nodes[node["operands"][0]]["type"]
        payload_width = int(source_type["payload_width"])
        variant = next(
            item
            for item in source_type["variants"]
            if item["name"] == node["attributes"]["variant"]
        )
        offset = payload_width
        for variant_field in variant["fields"]:
            offset -= int(variant_field["type"]["width"])
            if variant_field["name"] == node["attributes"]["field"]:
                return self.extract(
                    source, self.constant(offset, 32), int(node["type"]["width"])
                )
        raise PrimitiveLoweringError(
            f"unknown tagged-union field '{node['attributes']['field']}'"
        )

    def _fixed_convert(self, node: dict[str, Any], source: int) -> int:  # noqa: C901
        attrs = node["attributes"]
        width = int(node["type"]["width"])
        if attrs["conversion_kind"] != "rescale":
            return self.resize(source, width)
        source_type = self.semantic_nodes[node["operands"][0]]["type"]
        source_fraction = int(source_type["fraction"])
        target_fraction = int(node["type"]["fraction"])
        source_signed = self._signed(source_type)
        target_signed = self._signed(node["type"])
        left_shift = max(0, target_fraction - source_fraction)
        work_width = max(self.width(source) + left_shift + 2, width + 2)
        extended = self.resize(source, work_width, signed=source_signed)
        zero = self.constant(0, work_width)
        negative = (
            self.binary("slt", extended, zero, 1)
            if source_signed
            else self.constant(0, 1)
        )
        if target_fraction >= source_fraction:
            converted = self.binary(
                "shl", extended, self.constant(left_shift, 32), work_width
            )
        else:
            shift = source_fraction - target_fraction
            magnitude = self.select(
                negative,
                self.binary("sub", zero, extended, work_width),
                extended,
                work_width,
            )
            quotient = self.binary(
                "lshr", magnitude, self.constant(shift, 32), work_width
            )
            remainder = self.binary(
                "and",
                magnitude,
                self.constant((1 << shift) - 1, work_width),
                work_width,
            )
            discarded = self.truthy(remainder)
            rounding = attrs["rounding"]
            if rounding == "toward_zero":
                increment = self.constant(0, 1)
            elif rounding == "floor":
                increment = self.binary("and", negative, discarded, 1)
            elif rounding == "away_zero":
                increment = discarded
            elif rounding == "nearest_even":
                half = self.constant(1 << (shift - 1), work_width)
                greater = self.unary("not", self.binary("ule", remainder, half, 1), 1)
                equal = self.binary("eq", remainder, half, 1)
                odd = self.truthy(
                    self.binary(
                        "and", quotient, self.constant(1, work_width), work_width
                    )
                )
                increment = self.binary(
                    "or", greater, self.binary("and", equal, odd, 1), 1
                )
            else:
                raise PrimitiveLoweringError(f"unknown rounding policy '{rounding}'")
            rounded = self.binary(
                "add", quotient, self.resize(increment, work_width), work_width
            )
            converted = self.select(
                negative,
                self.binary("sub", zero, rounded, work_width),
                rounded,
                work_width,
            )
        if attrs["overflow"] == "wrap":
            return self.resize(converted, width)
        minimum = self.constant(-(1 << (width - 1)) if target_signed else 0, work_width)
        maximum = self.constant(
            (1 << (width - 1)) - 1 if target_signed else (1 << width) - 1, work_width
        )
        below = self.binary("slt", converted, minimum, 1)
        above = self.unary("not", self.binary("sle", converted, maximum, 1), 1)
        return self.resize(
            self.select(
                below,
                minimum,
                self.select(above, maximum, converted, work_width),
                work_width,
            ),
            width,
        )


def lower_to_primitive_plan(
    payload: dict[str, Any],
    *,
    max_nodes: int,
) -> dict[str, Any]:  # noqa: C901, PLR0915
    """Return a plan whose executable surface is only the primitive VM."""

    semantic_nodes = payload["nodes"]
    if len(semantic_nodes) > max_nodes:
        raise PrimitiveLoweringError(
            f"semantic simulation plan exceeds the {max_nodes}-node compilation bound"
        )
    builder = _Builder(
        semantic_nodes,
        max_nodes,
        semantic_memories={item["name"]: item for item in payload["memories"]},
        semantic_fifos={item["name"]: item for item in payload.get("fifos", [])},
    )
    ports = [
        {
            "name": item["name"],
            "direction": item["direction"],
            "width": item["type"]["width"],
            "api_type": item["type"],
            "domain": item["domain"],
        }
        for item in payload["ports"]
    ]
    registers = [
        {
            "name": item["name"],
            "width": item["type"]["width"],
            "initial_limbs": item["initial_limbs"],
            "domain": item["domain"],
        }
        for item in payload["registers"]
    ]
    memories = [
        {
            "name": item["name"],
            "width": item["type"]["width"],
            "depth": item["depth"],
            "domain": item["domain"],
            "initial_limbs": item["initial_limbs"],
        }
        for item in payload["memories"]
    ]
    register_by_name = {item["name"]: item for item in registers}
    next_by_domain: dict[str, dict[str, int]] = {}
    for register in registers:
        domain = register["domain"]
        if domain is None:
            continue
        next_by_domain.setdefault(domain, {})[register["name"]] = builder.emit(
            "load_state", register["width"], attributes={"name": register["name"]}
        )

    def condition(identifier: int | None) -> int:
        return (
            builder.constant(1, 1)
            if identifier is None
            else builder.truthy(builder.lower(identifier))
        )

    for item in payload["direct_next"]:
        domain = item["domain"]
        if domain is None or item["target"] not in next_by_domain.get(domain, {}):
            continue
        old = next_by_domain[domain][item["target"]]
        next_by_domain[domain][item["target"]] = builder.select(
            condition(item["activation"]),
            builder.lower(item["node"]),
            old,
            builder.width(old),
        )

    # The compiler-owned scheduler has already minimized its exact finite
    # truth table into selection cubes.  Rebuild those predicates as primitive
    # nodes; the native runtime never sees rules, priorities, resources, or
    # transition conflicts.
    scheduler_counts = [
        builder.emit(
            "load_state",
            int(builder.semantic_fifos[name]["count_width"]),
            attributes={"name": builder.semantic_fifos[name]["count"]},
        )
        for name in payload.get("scheduler_fifos", [])
    ]
    transition_by_name = {item["name"]: item for item in payload["transitions"]}
    scheduler_guards = [
        builder.truthy(builder.lower(transition_by_name[name]["guard"]))
        for name in payload.get("scheduler_guards", [])
    ]
    scheduler_activations = [
        builder.truthy(builder.lower(identifier))
        for identifier in payload.get("scheduler_activations", [])
    ]
    scheduler_inputs = [
        *scheduler_counts,
        *scheduler_guards,
        *scheduler_activations,
    ]
    scheduler_fifo_depths = [
        int(builder.semantic_fifos[name]["depth"])
        for name in payload.get("scheduler_fifos", [])
    ]
    scheduler_atom_cache: dict[tuple[int, object], int] = {}

    def scheduler_atom(index: int, expected: object, actual: int) -> int:
        key = (index, expected)
        cached = scheduler_atom_cache.get(key)
        if cached is not None:
            return cached
        if index < len(scheduler_counts):
            width = builder.width(actual)
            empty = builder.binary("eq", actual, builder.constant(0, width), 1)
            full = builder.binary(
                "eq",
                actual,
                builder.constant(scheduler_fifo_depths[index], width),
                1,
            )
            if expected == "empty":
                result = empty
            elif expected == "full":
                result = full
            elif expected == "middle":
                result = builder.unary(
                    "not", builder.binary("or", empty, full, 1), 1
                )
            else:
                raise PrimitiveLoweringError(
                    "scheduler region has an invalid FIFO occupancy"
                )
        elif isinstance(expected, bool):
            result = (
                builder.truthy(actual)
                if expected
                else builder.unary("not", builder.truthy(actual), 1)
            )
        else:
            raise PrimitiveLoweringError(
                "scheduler region has an invalid boolean predicate"
            )
        scheduler_atom_cache[key] = result
        return result

    scheduled_storage_actions: list[dict[str, Any]] = []
    for group in payload["transitions"]:
        domain = group["domain"]
        if domain is None:
            continue
        fire = builder.constant(0, 1)
        for region in group["selection_regions"]:
            if len(region) != len(scheduler_inputs):
                raise PrimitiveLoweringError(
                    f"scheduler region for rule '{group['name']}' has invalid arity"
                )
            matches = builder.constant(1, 1)
            for index, (expected, actual) in enumerate(
                zip(region, scheduler_inputs, strict=True)
            ):
                if expected is None:
                    continue
                matches = builder.binary(
                    "and", matches, scheduler_atom(index, expected, actual), 1
                )
            fire = builder.binary("or", fire, matches, 1)
        for action in group["actions"]:
            active = condition(action["activation"])
            commit = builder.binary("and", fire, active, 1)
            if action["kind"] != "register_write":
                scheduled_storage_actions.append(
                    {**action, "domain": domain, "commit": commit}
                )
                continue
            target = action["target"]
            old = next_by_domain[domain][target]
            next_by_domain[domain][target] = builder.select(
                commit, builder.lower(action["node"]), old, builder.width(old)
            )

    edge_programs = []
    memory_by_name = {item["name"]: item for item in memories}
    for domain in payload["domains"]:
        clock = domain["clock"]
        next_values = next_by_domain.setdefault(clock, {})
        reset_node = None
        if domain["reset"] is not None:
            reset_node = builder.emit(
                "load_input", 1, attributes={"name": f"$reset:{domain['reset']}"}
            )
        release_node = None
        if domain["reset_release_mode"] == "synchronized":
            release_node = builder.emit(
                "load_state", 32, attributes={"name": f"$release:{clock}"}
            )
            release_node = builder.truthy(release_node)
        effective_reset = reset_node
        if release_node is not None:
            effective_reset = (
                release_node
                if effective_reset is None
                else builder.binary("or", effective_reset, release_node, 1)
            )

        effects: list[dict[str, Any]] = []
        invalid = builder.constant(0, 1)
        for semantic_memory in payload["memories"]:
            active_port_domains = {
                port["domain"] for port in semantic_memory.get("ports", [])
            }
            if (
                semantic_memory["domain"] != clock
                and clock not in active_port_domains
            ):
                continue
            if semantic_memory.get("managed_by") == "fifo":
                continue
            memory = memory_by_name[semantic_memory["name"]]
            if semantic_memory.get("managed_by") == "scheduled_memory":
                actions = [
                    action
                    for action in scheduled_storage_actions
                    if action["domain"] == clock
                    and action["target"] == semantic_memory["name"]
                ]
                address_width = max(
                    (
                        builder.width(builder.lower(action["operands"][0]))
                        for action in actions
                    ),
                    default=max(1, (int(memory["depth"]) - 1).bit_length()),
                )
                zero_address = builder.constant(0, address_width)
                read_fire = builder.constant(0, 1)
                read_address = zero_address
                write_fire = builder.constant(0, 1)
                write_address = zero_address
                write_data = builder.constant(0, memory["width"])
                write_mask = None
                for action in actions:
                    commit = int(action["commit"])
                    operands = action["operands"]
                    if action["kind"] == "memory_read_request":
                        read_fire = builder.binary("or", read_fire, commit, 1)
                        read_address = builder.select(
                            commit,
                            builder.resize(builder.lower(operands[0]), address_width),
                            read_address,
                            address_width,
                        )
                    elif action["kind"] == "memory_write":
                        write_fire = builder.binary("or", write_fire, commit, 1)
                        write_address = builder.select(
                            commit,
                            builder.resize(builder.lower(operands[0]), address_width),
                            write_address,
                            address_width,
                        )
                        write_data = builder.select(
                            commit,
                            builder.lower(operands[1]),
                            write_data,
                            memory["width"],
                        )
                        if len(operands) == 3:
                            candidate_mask = builder.lower(operands[2])
                            if write_mask is None:
                                write_mask = builder.constant(
                                    0, builder.width(candidate_mask)
                                )
                            write_mask = builder.select(
                                commit,
                                candidate_mask,
                                write_mask,
                                builder.width(candidate_mask),
                            )
                comparison_width = address_width + 1
                depth = builder.constant(memory["depth"], comparison_width)
                read_valid = builder.binary(
                    "ult", builder.resize(read_address, comparison_width), depth, 1
                )
                write_valid = builder.binary(
                    "ult", builder.resize(write_address, comparison_width), depth, 1
                )
                safe_read = builder.select(
                    read_valid, read_address, zero_address, address_width
                )
                safe_write = builder.select(
                    write_valid, write_address, zero_address, address_width
                )
                old_read = builder.emit(
                    "load_memory",
                    memory["width"],
                    (safe_read,),
                    {"memory": memory["name"]},
                )
                old_write = builder.emit(
                    "load_memory",
                    memory["width"],
                    (safe_write,),
                    {"memory": memory["name"]},
                )
                merged_write = builder.merge_masked_value(
                    old_write,
                    write_data,
                    write_mask,
                    width=memory["width"],
                    mask_width=semantic_memory["write_mask_width"],
                )
                collision = builder.binary(
                    "and",
                    write_fire,
                    builder.binary("eq", read_address, write_address, 1),
                    1,
                )
                captured = (
                    builder.select(
                        collision,
                        merged_write,
                        old_read,
                        memory["width"],
                    )
                    if semantic_memory["collision"] == "write_first"
                    else old_read
                )
                read_names = semantic_memory["scheduled_read_registers"]
                if not read_names:
                    raise PrimitiveLoweringError(
                        f"scheduled memory '{semantic_memory['name']}' has no read state"
                    )
                old_reads = [next_values[name] for name in read_names]
                next_values[read_names[0]] = builder.select(
                    read_fire, captured, old_reads[0], memory["width"]
                )
                for index in range(1, len(read_names)):
                    next_values[read_names[index]] = builder.select(
                        read_fire,
                        old_reads[index - 1],
                        old_reads[index],
                        memory["width"],
                    )
                store_enable = builder.binary("and", write_fire, write_valid, 1)
                if effective_reset is not None:
                    store_enable = builder.binary(
                        "and",
                        store_enable,
                        builder.unary("not", effective_reset, 1),
                        1,
                    )
                effects.append(
                    {
                        "op": "store_memory",
                        "memory": memory["name"],
                        "address": safe_write,
                        "node": merged_write,
                        "enable": store_enable,
                    }
                )
                if (
                    semantic_memory["contents_reset"] == "clear"
                    and effective_reset is not None
                ):
                    effects.append(
                        {
                            "op": "fill_memory",
                            "memory": memory["name"],
                            "node": builder.constant(
                                sum(
                                    int(limb) << (64 * i)
                                    for i, limb in enumerate(memory["initial_limbs"])
                                ),
                                memory["width"],
                            ),
                            "enable": effective_reset,
                        }
                    )
                invalid_read = builder.binary(
                    "and", read_fire, builder.unary("not", read_valid, 1), 1
                )
                invalid_write = builder.binary(
                    "and", write_fire, builder.unary("not", write_valid, 1), 1
                )
                invalid = builder.binary(
                    "or",
                    invalid,
                    builder.binary("or", invalid_read, invalid_write, 1),
                    1,
                )
                continue
            memory_ports = semantic_memory["ports"]
            port_by_name = {item["name"]: item for item in memory_ports}
            writer_names = semantic_memory["write_priority"] or [
                item["name"]
                for item in memory_ports
                if item["write_enable"] is not None
            ]
            writers: list[dict[str, int | str | None]] = []
            claimed: list[tuple[int, int]] = []
            for writer_name in writer_names:
                port = port_by_name[writer_name]
                local_writer = port["domain"] == clock
                remote_collision_writer = (
                    bool(semantic_memory.get("async_memory"))
                    and semantic_memory["collision"] != "read_first"
                    and clock in active_port_domains
                )
                if not local_writer and not remote_collision_writer:
                    continue
                write_address = builder.lower(port["write_address"])
                address_width = builder.width(write_address)
                comparison_width = address_width + 1
                depth = builder.constant(memory["depth"], comparison_width)
                valid_address = builder.binary(
                    "ult", builder.resize(write_address, comparison_width), depth, 1
                )
                safe_address = builder.select(
                    valid_address,
                    write_address,
                    builder.constant(0, address_width),
                    address_width,
                )
                requested = builder.truthy(builder.lower(port["write_enable"]))
                if not local_writer:
                    requested = builder.binary(
                        "and", requested, builder.event(port["domain"]), 1
                    )
                    writer_reset = semantic_memory.get("reset")
                    if writer_reset is not None:
                        reset_active = builder.emit(
                            "load_input",
                            1,
                            attributes={"name": f"$reset:{writer_reset}"},
                        )
                        requested = builder.binary(
                            "and", requested, builder.unary("not", reset_active, 1), 1
                        )
                if claimed:
                    unclaimed = builder.constant(1, 1)
                    for higher_address, higher_active in claimed:
                        same = builder.binary("eq", write_address, higher_address, 1)
                        unclaimed = builder.binary(
                            "and",
                            unclaimed,
                            builder.unary(
                                "not",
                                builder.binary("and", higher_active, same, 1),
                                1,
                            ),
                            1,
                        )
                    active = builder.binary("and", requested, unclaimed, 1)
                else:
                    active = requested
                old = builder.emit(
                    "load_memory",
                    memory["width"],
                    (safe_address,),
                    {"memory": memory["name"]},
                )
                value = builder.merge_masked_value(
                    old,
                    builder.lower(port["write_data"]),
                    (
                        builder.lower(port["write_mask"])
                        if port["write_mask"] is not None
                        else None
                    ),
                    width=memory["width"],
                    mask_width=semantic_memory["write_mask_width"],
                )
                store_enable = builder.binary("and", active, valid_address, 1)
                if effective_reset is not None:
                    store_enable = builder.binary(
                        "and",
                        store_enable,
                        builder.unary("not", effective_reset, 1),
                        1,
                    )
                if local_writer:
                    effects.append(
                        {
                            "op": "store_memory",
                            "memory": memory["name"],
                            "address": safe_address,
                            "node": value,
                            "enable": store_enable,
                        }
                    )
                writers.append(
                    {
                        "address": write_address,
                        "active": active,
                        "value": value,
                    }
                )
                claimed.append((write_address, active))
                invalid = builder.binary(
                    "or",
                    invalid,
                    builder.binary(
                        "and", requested, builder.unary("not", valid_address, 1), 1
                    ),
                    1,
                )

            for port in memory_ports:
                if port["domain"] != clock:
                    continue
                reads = port["read_registers"]
                if not reads:
                    continue
                read_address = builder.lower(port["address"])
                address_width = builder.width(read_address)
                comparison_width = address_width + 1
                depth = builder.constant(memory["depth"], comparison_width)
                valid_address = builder.binary(
                    "ult", builder.resize(read_address, comparison_width), depth, 1
                )
                safe_address = builder.select(
                    valid_address,
                    read_address,
                    builder.constant(0, address_width),
                    address_width,
                )
                old = builder.emit(
                    "load_memory",
                    memory["width"],
                    (safe_address,),
                    {"memory": memory["name"]},
                )
                read_enable = (
                    builder.constant(1, 1)
                    if port["read_enable"] is None
                    else builder.truthy(builder.lower(port["read_enable"]))
                )
                collision = None
                write_first_value = old
                for writer in reversed(writers):
                    same = builder.binary(
                        "eq", read_address, int(writer["address"]), 1
                    )
                    hit = builder.binary("and", int(writer["active"]), same, 1)
                    collision = (
                        hit
                        if collision is None
                        else builder.binary("or", collision, hit, 1)
                    )
                    write_first_value = builder.select(
                        hit,
                        int(writer["value"]),
                        write_first_value,
                        memory["width"],
                    )
                current_read = next_values[reads[0]]
                collision_value = {
                    "read_first": old,
                    "write_first": write_first_value,
                    "no_change": current_read,
                }[semantic_memory["collision"]]
                sampled = (
                    old
                    if collision is None
                    else builder.select(
                        collision, collision_value, old, memory["width"]
                    )
                )
                if port["read_enable"] is not None:
                    sampled = builder.select(
                        read_enable, sampled, current_read, memory["width"]
                    )
                old_reads = [next_values[name] for name in reads]
                next_values[reads[0]] = sampled
                for index in range(1, len(reads)):
                    next_values[reads[index]] = old_reads[index - 1]
                invalid = builder.binary(
                    "or",
                    invalid,
                    builder.binary(
                        "and", read_enable, builder.unary("not", valid_address, 1), 1
                    ),
                    1,
                )
            if (
                semantic_memory["contents_reset"] == "clear"
                and semantic_memory["domain"] == clock
                and effective_reset is not None
            ):
                effects.append(
                    {
                        "op": "fill_memory",
                        "memory": memory["name"],
                        "node": builder.constant(
                            sum(
                                int(limb) << (64 * i)
                                for i, limb in enumerate(memory["initial_limbs"])
                            ),
                            memory["width"],
                        ),
                        "enable": effective_reset,
                    }
                )

        for fifo in payload.get("fifos", []):
            if fifo["domain"] != clock:
                continue
            count = next_values[fifo["count"]]
            read_pointer = next_values[fifo["read_pointer"]]
            write_pointer = next_values[fifo["write_pointer"]]
            empty = builder.binary(
                "eq", count, builder.constant(0, builder.width(count)), 1
            )
            full = builder.binary(
                "eq",
                count,
                builder.constant(int(fifo["depth"]), builder.width(count)),
                1,
            )
            not_reset = (
                builder.constant(1, 1)
                if effective_reset is None
                else builder.unary("not", effective_reset, 1)
            )
            if fifo["scheduled"]:
                matching = [
                    action
                    for action in scheduled_storage_actions
                    if action["domain"] == clock and action["target"] == fifo["name"]
                ]
                pop = builder.constant(0, 1)
                push = builder.constant(0, 1)
                push_data = builder.constant(0, int(fifo["type"]["width"]))
                for action in matching:
                    commit = int(action["commit"])
                    if action["kind"] == "fifo_pop":
                        pop = builder.binary("or", pop, commit, 1)
                    elif action["kind"] == "fifo_push":
                        push = builder.binary("or", push, commit, 1)
                        push_data = builder.select(
                            commit,
                            builder.lower(action["node"]),
                            push_data,
                            int(fifo["type"]["width"]),
                        )
                pop = builder.binary("and", not_reset, pop, 1)
                push = builder.binary("and", not_reset, push, 1)
                pop_request = pop
                push_request = push
            else:
                pop_request = builder.truthy(builder.lower(fifo["pop"]))
                push_request = builder.truthy(builder.lower(fifo["push"]))
                pop = builder.binary(
                    "and",
                    not_reset,
                    builder.binary(
                        "and", pop_request, builder.unary("not", empty, 1), 1
                    ),
                    1,
                )
                push = builder.binary(
                    "and",
                    not_reset,
                    builder.binary(
                        "and",
                        push_request,
                        builder.binary("or", builder.unary("not", full, 1), pop, 1),
                        1,
                    ),
                    1,
                )
                push_data = builder.lower(fifo["data"])
            effects.append(
                {
                    "op": "store_memory",
                    "memory": fifo["memory"],
                    "address": write_pointer,
                    "node": push_data,
                    "enable": push,
                }
            )

            one_count = builder.constant(1, builder.width(count))
            incremented_count = builder.binary("add", count, one_count, builder.width(count))
            decremented_count = builder.binary("sub", count, one_count, builder.width(count))
            next_count = builder.select(
                builder.binary("and", push, builder.unary("not", pop, 1), 1),
                incremented_count,
                builder.select(
                    builder.binary("and", pop, builder.unary("not", push, 1), 1),
                    decremented_count,
                    count,
                    builder.width(count),
                ),
                builder.width(count),
            )

            def advance(pointer: int) -> int:
                last = builder.constant(int(fifo["depth"]) - 1, builder.width(pointer))
                at_last = builder.binary("eq", pointer, last, 1)
                incremented = builder.binary(
                    "add",
                    pointer,
                    builder.constant(1, builder.width(pointer)),
                    builder.width(pointer),
                )
                return builder.select(
                    at_last,
                    builder.constant(0, builder.width(pointer)),
                    incremented,
                    builder.width(pointer),
                )

            next_values[fifo["count"]] = next_count
            next_values[fifo["read_pointer"]] = builder.select(
                pop, advance(read_pointer), read_pointer, builder.width(read_pointer)
            )
            next_values[fifo["write_pointer"]] = builder.select(
                push,
                advance(write_pointer),
                write_pointer,
                builder.width(write_pointer),
            )
            if not fifo["scheduled"]:
                overflow = builder.binary(
                    "and",
                    not_reset,
                    builder.binary(
                        "and",
                        push_request,
                        builder.binary("and", full, builder.unary("not", pop, 1), 1),
                        1,
                    ),
                    1,
                )
                underflow = builder.binary(
                    "and", not_reset, builder.binary("and", pop_request, empty, 1), 1
                )
                invalid = builder.binary(
                    "or", invalid, builder.binary("or", overflow, underflow, 1), 1
                )

        for name, value in sorted(next_values.items()):
            register = register_by_name[name]
            if effective_reset is not None:
                initial = sum(
                    int(limb) << (64 * i)
                    for i, limb in enumerate(register["initial_limbs"])
                )
                value = builder.select(
                    effective_reset,
                    builder.constant(initial, register["width"]),
                    value,
                    register["width"],
                )
            effects.append({"op": "commit_state", "target": name, "node": value})
        probes: list[dict[str, Any]] = []
        not_reset = (
            builder.constant(1, 1)
            if effective_reset is None
            else builder.unary("not", effective_reset, 1)
        )
        for scope in payload.get("instrumentation_scopes", []):
            if scope["clock"] != clock:
                continue
            requirements_hold = builder.constant(1, 1)
            for requirement in scope["requirements"]:
                holds = builder.truthy(builder.lower(requirement["node"]))
                probes.append(
                    {
                        "kind": "cover",
                        "event": requirement["event"],
                        "condition": builder.binary(
                            "and", not_reset, builder.unary("not", holds, 1), 1
                        ),
                        "once": False,
                    }
                )
                requirements_hold = builder.binary(
                    "and", requirements_hold, holds, 1
                )
            active = builder.binary("and", not_reset, requirements_hold, 1)
            for goal in scope["goals"]:
                holds = builder.truthy(builder.lower(goal["node"]))
                if goal["kind"] == "cover":
                    probes.append(
                        {
                            "kind": "cover",
                            "event": goal["event"],
                            "condition": builder.binary("and", active, holds, 1),
                            "once": True,
                        }
                    )
                else:
                    probes.append(
                        {
                            "kind": "check",
                            "event": goal["event"],
                            "condition": builder.binary(
                                "or", builder.unary("not", active, 1), holds, 1
                            ),
                            "once": False,
                        }
                    )
        edge_programs.append(
            {
                "clock": clock,
                "effects": effects,
                "error": invalid,
                "probes": probes,
            }
        )

    result = dict(payload)
    result["ports"] = ports
    result["nodes"] = builder.nodes
    result["outputs"] = [
        {"name": item["name"], "node": builder.lower(item["node"])}
        for item in payload["outputs"]
    ]
    result["registers"] = registers
    result["memories"] = memories
    result["edge_programs"] = edge_programs
    result.pop("direct_next")
    result.pop("transitions")
    result.pop("fifos", None)
    result.pop("scheduler_fifos", None)
    result.pop("scheduler_guards", None)
    result.pop("scheduler_activations", None)
    result.pop("instrumentation_scopes", None)
    return result


__all__ = ["PRIMITIVE_OPS", "PrimitiveLoweringError", "lower_to_primitive_plan"]
