# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Primitive lowering for bounded functional regions."""

from __future__ import annotations

from typing import Any

from zlang.simulation_primitive_model import (
    PrimitiveBuilder,
    PrimitiveLowerer,
    PrimitiveLoweringError,
)


class FunctionalRegionPrimitiveLowerer(PrimitiveLowerer):
    """Own binder, capture, table, and loop-region primitive construction."""

    def try_lower(self, node: dict[str, Any]) -> int | None:
        """Lower one functional semantic node, or decline ordinary nodes."""

        op = node["op"]
        attrs = node["attributes"]
        width = int(node["type"]["width"])
        if op == "functional_region":
            return self.lower(node)
        if op == "functional_capture":
            return self.lower_capture(attrs, width)
        if op == "functional_value":
            return self.lower_value(attrs, width)
        if op == "functional_table_lookup":
            return self.lower_table_lookup(attrs, width)
        return None

    def _hoist_primitive_invariants(
        self,
        body: PrimitiveBuilder,
        captures: tuple[int, ...],
        capture_widths: list[int],
        root: int,
    ) -> tuple[list[dict[str, Any]], tuple[int, ...], list[int], int]:
        """Evaluate binder-independent primitive work once in the parent scope."""

        builder = self._builder
        dependent: list[bool] = []
        parent_ids: dict[int, int] = {}
        local_ids: dict[int, int] = {}
        local_nodes: list[dict[str, Any]] = []
        capture_nodes = list(captures)
        widths = list(capture_widths)
        boundary_slots: dict[int, int] = {}
        boundary_local_ids: dict[int, int] = {}

        def local_operand(identifier: int) -> int:
            if dependent[identifier]:
                return local_ids[identifier]
            if identifier not in boundary_slots:
                boundary_slots[identifier] = len(capture_nodes)
                capture_nodes.append(parent_ids[identifier])
                widths.append(body.width(identifier))
                boundary_local_ids[identifier] = len(local_nodes)
                local_nodes.append({
                    "id": len(local_nodes),
                    "op": "load_capture",
                    "width": body.width(identifier),
                    "operands": [],
                    "attributes": {"slot": boundary_slots[identifier]},
                    "origins": [],
                })
            return boundary_local_ids[identifier]

        for node in body.nodes:
            identifier = node["id"]
            is_dependent = node["op"] == "load_index" or any(
                dependent[operand] for operand in node["operands"]
            )
            dependent.append(is_dependent)
            if not is_dependent:
                if node["op"] == "load_capture":
                    parent_ids[identifier] = captures[node["attributes"]["slot"]]
                else:
                    parent_ids[identifier] = builder.emit(
                        node["op"],
                        node["width"],
                        tuple(parent_ids[item] for item in node["operands"]),
                        node["attributes"],
                        node["origins"],
                    )
                continue
            operands = [local_operand(item) for item in node["operands"]]
            local_ids[identifier] = len(local_nodes)
            local_nodes.append({
                **node,
                "id": len(local_nodes),
                "operands": operands,
            })

        result = local_operand(root) if not dependent[root] else local_ids[root]
        return local_nodes, tuple(capture_nodes), widths, result

    def _invariant_values(self, template: int, binder: str) -> tuple[int, ...]:
        """Find maximal semantic sub-DAGs independent of one binder."""

        semantic_nodes = self._builder.semantic_nodes
        dependencies: list[bool] = []
        for node in semantic_nodes:
            dependencies.append(
                (
                    node["op"] == "functional_value"
                    and node["attributes"].get("binder") == binder
                )
                or node["attributes"].get("binder") == binder
                or binder in node["attributes"].get("binders", [])
                or any(dependencies[item] for item in node["operands"])
            )
        selected: list[int] = []
        visited: set[int] = set()
        pending = [template]
        while pending:
            identifier = pending.pop()
            if identifier in visited:
                continue
            visited.add(identifier)
            node = semantic_nodes[identifier]
            if not dependencies[identifier]:
                selected.append(identifier)
            elif node["op"] != "functional_region":
                pending.extend(reversed(node["operands"]))
        return tuple(selected)

    def compile_time_value(self, value: dict[str, Any]) -> int:
        builder = self._builder
        if "binder" in value:
            binder = builder.binder_scope.get(str(value["binder"]))
            if binder is None:
                raise PrimitiveLoweringError("unbound functional binder value")
            return binder
        if "literal" in value:
            return builder.constant(int(value["literal"]), 64)
        operator = value.get("operator")
        operands = tuple(
            self.compile_time_value(item) for item in value.get("operands", [])
        )
        if operator in {"literal", "binder"} and len(operands) == 1:
            return operands[0]
        if operator in {"add", "subtract", "multiply"} and len(operands) == 2:
            primitive = {
                "add": "add",
                "subtract": "sub",
                "multiply": "mul",
            }[str(operator)]
            return builder.binary(primitive, operands[0], operands[1], 64)
        if operator == "negate" and len(operands) == 1:
            return builder.binary(
                "sub",
                builder.constant(0, 64),
                operands[0],
                64,
            )
        raise PrimitiveLoweringError(
            f"unsupported functional compile-time operator '{operator}'"
        )

    def lower_capture(self, attributes: dict[str, Any], width: int) -> int:
        builder = self._builder
        identity = attributes["identity"]
        capture_slots = builder.capture_slots
        if capture_slots is None or identity not in capture_slots:
            raise PrimitiveLoweringError("unbound functional capture")
        return builder.emit(
            "load_capture",
            width,
            attributes={"slot": capture_slots[identity]},
        )

    def lower_value(self, attributes: dict[str, Any], width: int) -> int:
        return self._builder.resize(
            self.compile_time_value(attributes["compile_time_expression"]),
            width,
        )

    def lower_table_lookup(self, attributes: dict[str, Any], width: int) -> int:
        builder = self._builder
        table = builder.table_scope.get(str(attributes["table_name"]))
        binder = builder.binder_scope.get(str(attributes["binder"]))
        if table is None or binder is None or not table[1]:
            raise PrimitiveLoweringError("unbound functional table lookup")
        start, values = table
        result = values[0]
        for index, value in reversed(tuple(enumerate(values))):
            match = builder.binary(
                "eq",
                binder,
                builder.constant(start + index, builder.width(binder)),
                1,
            )
            result = builder.select(match, value, result, width)
        return result

    def lower(self, node: dict[str, Any]) -> int:
        builder = self._builder
        attrs = node["attributes"]
        tables = attrs.get("tables", [])
        captures = attrs["captures"]
        table_count = sum(int(item["count"]) for item in tables)
        if len(node["operands"]) != 1 + table_count + len(captures):
            raise PrimitiveLoweringError("functional region has invalid captures")
        table_nodes = tuple(
            builder.lower(item)
            for item in node["operands"][1 : 1 + table_count]
        )
        declared_capture_nodes = tuple(
            builder.lower(item) for item in node["operands"][1 + table_count :]
        )
        capture_nodes = (*table_nodes, *declared_capture_nodes)
        capture_widths = [
            int(item["width"])
            for item in tables
            for _ in range(int(item["count"]))
        ] + [int(item["width"]) for item in captures]
        if any(
            builder.width(value) != width
            for value, width in zip(capture_nodes, capture_widths, strict=True)
        ):
            raise PrimitiveLoweringError("functional region capture width mismatch")
        capture_by_identity = {
            item["identity"]: declared_capture_nodes[index]
            for index, item in enumerate(captures)
        }
        if len(capture_by_identity) != len(captures):
            raise PrimitiveLoweringError(
                "functional region repeats a capture identity"
            )
        for identifier, semantic in enumerate(builder.semantic_nodes):
            if semantic["op"] != "functional_capture":
                continue
            resolved = capture_by_identity.get(semantic["attributes"]["identity"])
            if resolved is None:
                continue
            existing = builder.lowered.get(identifier)
            if existing is not None and existing != resolved:
                raise PrimitiveLoweringError(
                    "functional capture has inconsistent lexical binding"
                )
            builder.lowered[identifier] = resolved

        inherited_binders = tuple(builder.binder_scope.items())
        capture_nodes += tuple(value for _identity, value in inherited_binders)
        capture_widths.extend(64 for _ in inherited_binders)
        hoisted = self._invariant_values(node["operands"][0], attrs["binder"])
        capture_nodes += tuple(builder.lower(identifier) for identifier in hoisted)
        capture_widths.extend(
            int(builder.semantic_nodes[identifier]["type"]["width"])
            for identifier in hoisted
        )
        body = builder.spawn_region_builder({
            item["identity"]: table_count + index
            for index, item in enumerate(captures)
        })
        table_offset = 0
        for table in tables:
            values = tuple(
                body.emit(
                    "load_capture",
                    int(table["width"]),
                    attributes={"slot": table_offset + index},
                )
                for index in range(int(table["count"]))
            )
            body.table_scope[str(table["name"])] = (int(table["start"]), values)
            table_offset += int(table["count"])
        for ordinal, (identity, _value) in enumerate(
            inherited_binders,
            start=table_count + len(captures),
        ):
            body.binder_scope[identity] = body.emit(
                "load_capture",
                64,
                attributes={"slot": ordinal},
            )
        body.binder_scope[attrs["binder"]] = body.emit("load_index", 64)
        for ordinal, identifier in enumerate(
            hoisted,
            start=table_count + len(captures) + len(inherited_binders),
        ):
            body.lowered[identifier] = body.emit(
                "load_capture",
                int(builder.semantic_nodes[identifier]["type"]["width"]),
                attributes={"slot": ordinal},
            )
        root = body.lower(node["operands"][0])
        width = int(node["type"]["width"])
        element_width = body.width(root)
        if width != (int(attrs["stop"]) - int(attrs["start"])) * element_width:
            raise PrimitiveLoweringError("functional region result width mismatch")
        local_nodes, capture_nodes, capture_widths, root = (
            self._hoist_primitive_invariants(
                body,
                capture_nodes,
                capture_widths,
                root,
            )
        )
        region = len(builder.regions)
        builder.regions.append({
            "start": attrs["start"],
            "stop": attrs["stop"],
            "element_width": element_width,
            "width": width,
            "capture_widths": capture_widths,
            "nodes": local_nodes,
            "root": root,
        })
        return builder.emit(
            "loop_region",
            width,
            capture_nodes,
            {"region": region},
            node.get("origins", []),
        )


__all__ = ["FunctionalRegionPrimitiveLowerer"]
