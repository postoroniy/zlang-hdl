"""Lower compiler simulation semantics to a language-neutral bit-vector VM.

This module is the architectural boundary between ZLang and the native
runtime.  Everything above this boundary may know about ZLang types, packing,
rules, resets, memories and scheduling.  Everything below it sees only packed
bit vectors, storage slots and explicit edge effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from zlang.simulation_edge_lowering import EdgeProgramLowerer
from zlang.simulation_fixed_lowering import FixedPointPrimitiveLowerer
from zlang.simulation_functional_lowering import FunctionalRegionPrimitiveLowerer
from zlang.simulation_primitive_model import (
    PRIMITIVE_OPS,
    PrimitiveLoweringError,
)
from zlang.simulation_scheduler_lowering import lower_scheduler_actions
from zlang.simulation_storage_lowering import StorageReferencePrimitiveLowerer


@dataclass
class _Builder:
    semantic_nodes: list[dict[str, Any]]
    max_nodes: int
    semantic_memories: dict[str, dict[str, Any]] = field(default_factory=dict)
    semantic_fifos: dict[str, dict[str, Any]] = field(default_factory=dict)
    nodes: list[dict[str, Any]] = field(default_factory=list)
    lowered: dict[int, int] = field(default_factory=dict)
    regions: list[dict[str, Any]] = field(default_factory=list)
    capture_slots: dict[str, int] | None = None
    binder_scope: dict[str, int] = field(default_factory=dict)
    table_scope: dict[str, tuple[int, tuple[int, ...]]] = field(default_factory=dict)
    reset_release_clocks: dict[str, str] = field(default_factory=dict)
    _functional: FunctionalRegionPrimitiveLowerer = field(init=False, repr=False)
    _fixed: FixedPointPrimitiveLowerer = field(init=False, repr=False)
    _storage: StorageReferencePrimitiveLowerer = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._functional = FunctionalRegionPrimitiveLowerer(self)
        self._fixed = FixedPointPrimitiveLowerer(self)
        self._storage = StorageReferencePrimitiveLowerer(self)

    def spawn_region_builder(self, capture_slots: dict[str, int]) -> "_Builder":
        return _Builder(
            self.semantic_nodes,
            self.max_nodes,
            semantic_memories=self.semantic_memories,
            semantic_fifos=self.semantic_fifos,
            regions=self.regions,
            capture_slots=capture_slots,
            reset_release_clocks=self.reset_release_clocks,
        )

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
        result = self._functional.try_lower(node)
        if result is not None:
            self.lowered[identifier] = result
            return result
        operands = tuple(self.lower(item) for item in node["operands"])
        origins = node.get("origins", [])
        result = self._storage.try_lower(node, operands, origins)
        if result is not None:
            self.lowered[identifier] = result
            return result

        if op == "input":
            raw = self.emit(
                "load_input", width, attributes={"name": attrs["name"]}, origins=origins
            )
            name = str(attrs["name"])
            reset = name.removeprefix("$reset:")
            clock = (
                self.reset_release_clocks.get(reset)
                if name.startswith("$reset:")
                else None
            )
            if clock is None:
                result = raw
            else:
                release = self.emit(
                    "load_state", 32, attributes={"name": f"$release:{clock}"}
                )
                result = self.binary("or", raw, self.truthy(release), 1)
        elif op == "register_ref":
            result = self.emit(
                "load_state", width, attributes={"name": attrs["name"]}, origins=origins
            )
        elif op == "constant":
            result = self.emit(
                "constant", width, attributes={"limbs": attrs["limbs"]}, origins=origins
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
            if operator in {"==", "!=", "<", "<=", ">", ">="}:
                comparison_width = int(operand_type["width"])
                left = self.resize(left, comparison_width, signed=signed)
                right = self.resize(right, comparison_width, signed=signed)
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
            result = self._fixed.lower(node, operands[0])
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
            if "compile_time_expression" in attrs:
                index = self._functional.compile_time_value(
                    attrs["compile_time_expression"]
                )
                offset = self.binary(
                    "mul",
                    index,
                    self.constant(width, self.width(index)),
                    self.width(index),
                )
                result = self.extract(operands[0], offset, width)
            else:
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
        signed = self._signed(source_type["element"])
        values = [
            self.resize(
                self.extract(
                    source, self.constant(index * element_width, 32), element_width
                ),
                width,
                signed=signed,
            )
            for index in range(length)
        ]
        operator = {"+": "add", "*": "mul", "&": "and", "|": "or", "^": "xor"}[
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
        reset_release_clocks={
            str(item["reset"]): str(item["clock"])
            for item in payload["domains"]
            if item.get("reset") is not None
            and item.get("reset_release_mode") == "synchronized"
        },
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
            # Consumed during primitive lowering and deliberately omitted from
            # the runtime register layout below.
            # Compiler-synthesized pipeline, FIFO, ROM, and memory-read state
            # predates the per-source-register flag and remains resettable by
            # construction. Source registers always publish the flag.
            "resettable": item.get("resettable", True),
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
    next_by_domain: dict[str, dict[str, int]] = {}
    refresh_by_domain: dict[str, dict[str, int | None]] = {}
    for register in registers:
        domain = register["domain"]
        if domain is None:
            continue
        next_by_domain.setdefault(domain, {})[register["name"]] = builder.emit(
            "load_state", register["width"], attributes={"name": register["name"]}
        )
        if not register["resettable"]:
            refresh_by_domain.setdefault(domain, {})[register["name"]] = None

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
        active = condition(item["activation"])
        next_by_domain[domain][item["target"]] = builder.select(
            active,
            builder.lower(item["node"]),
            old,
            builder.width(old),
        )
        if item["target"] in refresh_by_domain.get(domain, {}):
            previous = refresh_by_domain[domain][item["target"]]
            refresh_by_domain[domain][item["target"]] = (
                active
                if previous is None
                else builder.binary("or", previous, active, 1)
            )

    scheduled_storage_actions = lower_scheduler_actions(
        payload,
        builder,
        next_by_domain,
    )
    for action in scheduled_storage_actions:
        if action["kind"] != "register_write":
            continue
        domain = str(action["domain"])
        target = str(action["target"])
        if target not in refresh_by_domain.get(domain, {}):
            continue
        previous = refresh_by_domain[domain][target]
        refresh_by_domain[domain][target] = (
            int(action["commit"])
            if previous is None
            else builder.binary("or", previous, int(action["commit"]), 1)
        )

    edge_programs = EdgeProgramLowerer(
        payload,
        builder,
        registers,
        memories,
        next_by_domain,
        refresh_by_domain,
        scheduled_storage_actions,
    ).lower()

    output_nodes = {
        item["name"]: builder.lower(item["node"])
        for item in payload["outputs"]
    }
    domain_by_clock = {
        item["clock"]: item for item in payload["domains"]
    }
    output_enabled_cache: dict[str, int] = {}

    def output_enabled(domain: str) -> int:
        cached = output_enabled_cache.get(domain)
        if cached is not None:
            return cached
        metadata = domain_by_clock.get(domain)
        if metadata is None:
            enabled = builder.constant(1, 1)
        else:
            effective_reset = None
            reset = metadata.get("reset")
            if reset is not None:
                effective_reset = builder.emit(
                    "load_input", 1, attributes={"name": f"$reset:{reset}"}
                )
            if metadata.get("reset_release_mode") == "synchronized":
                release = builder.emit(
                    "load_state", 32, attributes={"name": f"$release:{domain}"}
                )
                release = builder.truthy(release)
                effective_reset = (
                    release
                    if effective_reset is None
                    else builder.binary("or", effective_reset, release, 1)
                )
            enabled = (
                builder.constant(1, 1)
                if effective_reset is None
                else builder.unary("not", builder.truthy(effective_reset), 1)
            )
        output_enabled_cache[domain] = enabled
        return enabled

    for action in scheduled_storage_actions:
        if action["kind"] != "output_write":
            continue
        target = action["target"]
        old = output_nodes.get(target)
        if old is None:
            raise PrimitiveLoweringError(
                f"scheduled output '{target}' has no public output declaration"
            )
        commit = builder.binary(
            "and",
            int(action["commit"]),
            output_enabled(str(action["domain"])),
            1,
        )
        output_nodes[target] = builder.select(
            commit,
            builder.lower(action["node"]),
            old,
            builder.width(old),
        )

    result = dict(payload)
    result["ports"] = ports
    result["nodes"] = builder.nodes
    result["regions"] = builder.regions
    result["outputs"] = [
        {"name": item["name"], "node": output_nodes[item["name"]]}
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
