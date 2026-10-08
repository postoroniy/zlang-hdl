# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Strict validation for persisted native-simulation plan payloads."""

from __future__ import annotations

from typing import Any

from zlang.ir.packing import PACKING_LAYOUT_SCHEMA
from zlang.simulation_lowering import PRIMITIVE_OPS
from zlang.simulation_plan_policy import (
    DEFAULT_SIMULATION_PLAN_POLICY,
    SimulationPlanError,
    SimulationPlanPolicy,
)


def _validate_type_payload(type_: object, policy: SimulationPlanPolicy) -> None:
    if not isinstance(type_, dict):
        raise SimulationPlanError("hardware type record must be an object")
    kind = type_.get("kind")
    width = type_.get("width")
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or not 1 <= width <= policy.max_width
    ):
        raise SimulationPlanError("hardware type width is invalid")
    scalar_fields = {"kind", "width"}
    if kind == "bit":
        if set(type_) != scalar_fields or width != 1:
            raise SimulationPlanError("bit type record is invalid")
        return
    if kind in {"uint", "sint", "bits"}:
        if set(type_) != scalar_fields:
            raise SimulationPlanError(f"{kind} type record is invalid")
        return
    if kind in {"fixed", "ufixed"}:
        if set(type_) != {*scalar_fields, "fraction", "overflow"}:
            raise SimulationPlanError(f"{kind} type record is invalid")
        fraction = type_["fraction"]
        if (
            isinstance(fraction, bool)
            or not isinstance(fraction, int)
            or not 0 <= fraction < width
            or type_["overflow"] not in {"wrap", "saturate"}
        ):
            raise SimulationPlanError(f"{kind} type policy is invalid")
        return
    if kind == "enum":
        if set(type_) != {
            *scalar_fields,
            "name",
            "declaration_identity",
            "members",
            "codes",
        }:
            raise SimulationPlanError("enum type record is invalid")
        members = type_["members"]
        codes = type_["codes"]
        if (
            not isinstance(type_["name"], str)
            or not type_["name"]
            or not isinstance(type_["declaration_identity"], str)
            or not type_["declaration_identity"]
            or not isinstance(members, list)
            or not members
            or not all(isinstance(item, str) and item for item in members)
            or len(set(members)) != len(members)
            or not isinstance(codes, list)
            or len(codes) != len(members)
            or any(
                isinstance(item, bool)
                or not isinstance(item, int)
                or not 0 <= item < 1 << width
                for item in codes
            )
            or len(set(codes)) != len(codes)
        ):
            raise SimulationPlanError("enum type declaration is invalid")
        return
    if kind == "vec":
        if set(type_) != {*scalar_fields, "length", "element"}:
            raise SimulationPlanError("vector type record is invalid")
        length = type_["length"]
        if isinstance(length, bool) or not isinstance(length, int) or length < 1:
            raise SimulationPlanError("vector length is invalid")
        _validate_type_payload(type_["element"], policy)
        if width != length * type_["element"]["width"]:
            raise SimulationPlanError("vector packed width is invalid")
        return
    if kind == "tuple":
        if set(type_) != {*scalar_fields, "elements"}:
            raise SimulationPlanError("tuple type record is invalid")
        elements = type_["elements"]
        if not isinstance(elements, list) or not elements:
            raise SimulationPlanError("tuple element table is invalid")
        for element in elements:
            _validate_type_payload(element, policy)
        if width != sum(element["width"] for element in elements):
            raise SimulationPlanError("tuple packed width is invalid")
        return
    if kind == "struct":
        if set(type_) != {*scalar_fields, "name", "fields"}:
            raise SimulationPlanError("struct type record is invalid")
        fields = type_["fields"]
        if (
            not isinstance(type_["name"], str)
            or not type_["name"]
            or not isinstance(fields, list)
            or not fields
        ):
            raise SimulationPlanError("struct declaration is invalid")
        names: list[str] = []
        for field in fields:
            if (
                not isinstance(field, dict)
                or set(field) != {"name", "type"}
                or not isinstance(field["name"], str)
                or not field["name"]
            ):
                raise SimulationPlanError("struct field is invalid")
            names.append(field["name"])
            _validate_type_payload(field["type"], policy)
        if len(names) != len(set(names)) or width != sum(
            field["type"]["width"] for field in fields
        ):
            raise SimulationPlanError("struct packed layout is invalid")
        return
    if kind == "tagged_union":
        if set(type_) != {
            *scalar_fields,
            "name",
            "declaration_identity",
            "tag_width",
            "payload_width",
            "variants",
        }:
            raise SimulationPlanError("tagged-union type record is invalid")
        variants = type_["variants"]
        if (
            not isinstance(type_["name"], str)
            or not type_["name"]
            or not isinstance(type_["declaration_identity"], str)
            or not type_["declaration_identity"]
            or not isinstance(variants, list)
            or not variants
        ):
            raise SimulationPlanError("tagged-union declaration is invalid")
        variant_names: list[str] = []
        payload_widths: list[int] = []
        for variant in variants:
            if (
                not isinstance(variant, dict)
                or set(variant) != {"name", "fields"}
                or not isinstance(variant["name"], str)
                or not variant["name"]
                or not isinstance(variant["fields"], list)
            ):
                raise SimulationPlanError("tagged-union variant is invalid")
            variant_names.append(variant["name"])
            field_names: list[str] = []
            payload_width = 0
            for field in variant["fields"]:
                if (
                    not isinstance(field, dict)
                    or set(field) != {"name", "type"}
                    or not isinstance(field["name"], str)
                    or not field["name"]
                ):
                    raise SimulationPlanError("tagged-union field is invalid")
                field_names.append(field["name"])
                _validate_type_payload(field["type"], policy)
                payload_width += field["type"]["width"]
            if len(field_names) != len(set(field_names)):
                raise SimulationPlanError("tagged-union field names are not unique")
            payload_widths.append(payload_width)
        expected_tag = max(1, (len(variants) - 1).bit_length())
        expected_payload = max(payload_widths)
        if (
            len(variant_names) != len(set(variant_names))
            or type_["tag_width"] != expected_tag
            or type_["payload_width"] != expected_payload
            or width != expected_tag + expected_payload
        ):
            raise SimulationPlanError("tagged-union packed layout is invalid")
        return
    raise SimulationPlanError(f"unsupported hardware type kind '{kind}'")


def _validate_u64_limbs(value: object, width: int) -> bool:
    count = (width + 63) // 64
    if not isinstance(value, list) or len(value) != count:
        return False
    if any(
        isinstance(item, bool) or not isinstance(item, int) or not 0 <= item < 1 << 64
        for item in value
    ):
        return False
    used = width - 64 * (count - 1)
    return used == 64 or value[-1] < 1 << used


def validate_plan_payload(
    payload: dict[str, Any],
    *,
    policy: SimulationPlanPolicy = DEFAULT_SIMULATION_PLAN_POLICY,
) -> None:
    """Validate under one immutable compiler-owned policy."""

    _validate_plan_payload(payload, policy)


def _validate_plan_payload(
    payload: dict[str, Any],
    policy: SimulationPlanPolicy,
) -> None:
    required = {
        "schema",
        "runtime_abi",
        "packing_layout_schema",
        "canonical_ir_identity",
        "native_target",
        "module",
        "identity",
        "ports",
        "nodes",
        "regions",
        "outputs",
        "registers",
        "memories",
        "events",
        "domains",
        "edge_programs",
    }
    if set(payload) != required:
        raise SimulationPlanError("simulation plan fields do not match its schema")
    if payload["schema"] != policy.schema:
        raise SimulationPlanError("unsupported simulation plan schema")
    if payload["runtime_abi"] != policy.runtime_abi:
        raise SimulationPlanError("unsupported native simulation ABI")
    if payload["packing_layout_schema"] != PACKING_LAYOUT_SCHEMA:
        raise SimulationPlanError("unsupported packed-layout schema")
    if (
        not isinstance(payload["canonical_ir_identity"], str)
        or not payload["canonical_ir_identity"]
    ):
        raise SimulationPlanError("canonical IR identity is missing")
    if not isinstance(payload["module"], str) or not payload["module"]:
        raise SimulationPlanError("simulation module name is missing")
    native_target = payload["native_target"]
    if native_target != {
        "triple": "x86_64-unknown-linux-gnu",
        "cranelift": policy.cranelift_version,
        "isa_flags": ["native"],
    }:
        raise SimulationPlanError(
            "native JIT v1 requires the Linux x86-64 Cranelift recipe"
        )
    regions = payload["regions"]
    if not isinstance(regions, list) or len(regions) > policy.max_nodes:
        raise SimulationPlanError("simulation region table is invalid")
    node_count = 0
    limb_work = 0
    region_work: list[int] = []
    region_node_work: list[int] = []
    region_depth: list[int] = []
    for region_id, region in enumerate(regions):
        if not isinstance(region, dict) or set(region) != {
            "start", "stop", "element_width", "width", "capture_widths", "nodes", "root"
        }:
            raise SimulationPlanError("simulation region record is invalid")
        start, stop = region["start"], region["stop"]
        element_width, width = region["element_width"], region["width"]
        capture_widths = region["capture_widths"]
        body = region["nodes"]
        if (
            any(isinstance(value, bool) or not isinstance(value, int) for value in (
                start, stop, element_width, width
            ))
            or not 0 <= start < stop <= 65_536
            or not 1 <= element_width <= policy.max_width
            or width != (stop - start) * element_width
            or width > policy.max_width
            or not isinstance(capture_widths, list)
            or len(capture_widths) > policy.max_nodes
            or any(isinstance(item, bool) or not isinstance(item, int)
                   or not 1 <= item <= policy.max_width for item in capture_widths)
            or not isinstance(body, list)
            or not body
            or isinstance(region["root"], bool)
            or not isinstance(region["root"], int)
            or not 0 <= region["root"] < len(body)
            or not isinstance(body[region["root"]], dict)
            or body[region["root"]].get("width") != element_width
        ):
            raise SimulationPlanError("simulation region shape is invalid")
        count, limbs = _validate_node_table(
            body, regions[:region_id], policy=policy, capture_widths=capture_widths,
            binder_range=(start, stop),
        )
        node_count += count
        limb_work += limbs
        children = [node["attributes"]["region"] for node in body
                    if node["op"] == "loop_region"]
        depth = 1 + max((region_depth[child] for child in children), default=0)
        if depth > policy.max_region_depth:
            raise SimulationPlanError("simulation region nesting exceeds its bound")
        region_depth.append(depth)
        region_work.append((stop - start) * (
            1 + sum(region_work[child] for child in children)
        ))
        region_node_work.append((stop - start) * (
            len(body) + sum(region_node_work[child] for child in children)
        ))
    nodes = payload["nodes"]
    count, limbs = _validate_node_table(nodes, regions, policy=policy)
    node_count += count
    limb_work += limbs
    if node_count > policy.max_nodes:
        raise SimulationPlanError(f"simulation plan exceeds {policy.max_nodes} nodes")
    if limb_work > policy.max_limb_work:
        raise SimulationPlanError(
            f"simulation plan exceeds {policy.max_limb_work} packed node limbs"
        )
    iterations = sum(region_work[node["attributes"]["region"]] for node in nodes
                     if node["op"] == "loop_region")
    if iterations > policy.max_region_iterations:
        raise SimulationPlanError("simulation region work exceeds its bound")
    dynamic_nodes = len(nodes) + sum(
        region_node_work[node["attributes"]["region"]] for node in nodes
        if node["op"] == "loop_region"
    )
    if dynamic_nodes > policy.max_dynamic_node_work:
        raise SimulationPlanError("simulation dynamic node work exceeds its bound")
    expected_fields = {
        "ports": {"name", "direction", "width", "api_type", "domain"},
        "registers": {
            "name",
            "width",
            "initial_limbs",
            "domain",
        },
    }
    names: set[str] = set()
    for table in ("ports", "registers"):
        if not isinstance(payload[table], list):
            raise SimulationPlanError(f"simulation plan {table} table is invalid")
        for item in payload[table]:
            if (
                not isinstance(item, dict)
                or set(item) != expected_fields[table]
                or not isinstance(item.get("name"), str)
                or not item["name"]
            ):
                raise SimulationPlanError(f"simulation plan {table} entry is invalid")
            if item["name"] in names:
                raise SimulationPlanError(
                    f"simulation plan has duplicate signal '{item['name']}'"
                )
            names.add(item["name"])
            width = item.get("width")
            if (
                isinstance(width, bool)
                or not isinstance(width, int)
                or not 1 <= width <= policy.max_width
            ):
                raise SimulationPlanError(f"simulation plan {table} width is invalid")
            if table == "ports":
                _validate_type_payload(item.get("api_type"), policy)
                if item["api_type"]["width"] != width:
                    raise SimulationPlanError(
                        "simulation port API type width is inconsistent"
                    )
            if table == "ports" and item.get("direction") not in {"input", "output"}:
                raise SimulationPlanError("simulation port direction is invalid")
    known_nodes = range(len(nodes))
    output_names = {
        item["name"] for item in payload["ports"] if item["direction"] == "output"
    }
    if not isinstance(payload["outputs"], list):
        raise SimulationPlanError("simulation output table is invalid")
    for item in payload["outputs"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "node"}
            or item.get("name") not in output_names
            or item.get("node") not in known_nodes
        ):
            raise SimulationPlanError("simulation output references an unknown node")
    register_names = {item["name"] for item in payload["registers"]}
    if not isinstance(payload["domains"], list):
        raise SimulationPlanError("simulation clock-domain table is invalid")
    domain_fields = {
        "clock",
        "reset",
        "edge",
        "reset_mode",
        "reset_polarity",
        "reset_release_mode",
        "reset_release_cycles",
    }
    for domain in payload["domains"]:
        if (
            not isinstance(domain, dict)
            or set(domain) != domain_fields
            or not isinstance(domain.get("clock"), str)
            or not domain["clock"]
            or domain.get("edge") not in {"rising", "falling"}
            or domain.get("reset_mode") not in {"synchronous", "asynchronous"}
            or domain.get("reset_polarity") not in {"active_high", "active_low"}
            or domain.get("reset_release_mode") not in {"native", "synchronized"}
            or isinstance(domain.get("reset_release_cycles"), bool)
            or not isinstance(domain.get("reset_release_cycles"), int)
            or domain["reset_release_cycles"] < 0
            or (
                domain.get("reset_release_mode") == "native"
                and domain["reset_release_cycles"] != 0
            )
        ):
            raise SimulationPlanError("simulation clock-domain entry is invalid")
    domain_names = {item.get("clock") for item in payload["domains"]}
    if len(domain_names) != len(payload["domains"]):
        raise SimulationPlanError("simulation clock-domain names are not unique")
    for node in nodes:
        if (
            node["op"] == "load_event"
            and node["attributes"]["name"] not in domain_names
        ):
            raise SimulationPlanError(
                f"primitive event load %{node['id']} names an unknown clock"
            )
    for register in payload["registers"]:
        if (
            not _validate_u64_limbs(register["initial_limbs"], register["width"])
            or register["domain"] not in domain_names
        ):
            raise SimulationPlanError(
                f"simulation register '{register['name']}' metadata is invalid"
            )
    if not isinstance(payload["memories"], list):
        raise SimulationPlanError("simulation memory table is invalid")
    memory_names: set[str] = set()
    memory_fields = {
        "name",
        "width",
        "depth",
        "domain",
        "initial_limbs",
    }
    for memory in payload["memories"]:
        if (
            not isinstance(memory, dict)
            or set(memory) != memory_fields
            or not isinstance(memory.get("name"), str)
            or not memory["name"]
            or memory["name"] in memory_names
            or isinstance(memory.get("depth"), bool)
            or not isinstance(memory.get("depth"), int)
            or memory["depth"] < 1
            or memory["domain"] not in domain_names
        ):
            raise SimulationPlanError("simulation memory entry is invalid")
        memory_names.add(memory["name"])
        if (
            isinstance(memory.get("width"), bool)
            or not isinstance(memory.get("width"), int)
            or not 1 <= memory["width"] <= policy.max_memory_width
            or memory["depth"] * memory["width"] > policy.max_memory_bits
            or not _validate_u64_limbs(memory["initial_limbs"], memory["width"])
        ):
            raise SimulationPlanError(
                f"simulation memory '{memory['name']}' metadata is invalid"
            )
    events = payload["events"]
    if not isinstance(events, list) or len(events) > policy.max_events:
        raise SimulationPlanError("simulation instrumentation event table is invalid")
    for expected, event in enumerate(events):
        if (
            not isinstance(event, dict)
            or set(event) != {"id", "metadata"}
            or event.get("id") != expected
            or not isinstance(event.get("metadata"), dict)
        ):
            raise SimulationPlanError(
                "simulation instrumentation event entry is invalid"
            )
        metadata = event["metadata"]
        category = metadata.get("category")
        expected_metadata = {
            "category",
            "scope_id",
            "scope_name",
            "clause_id",
            "clause_name",
            "hierarchy_path",
            "source_origin",
        }
        if category in {"assertion_failure", "cover_witness"}:
            expected_metadata.add("goal_kind")
        if (
            category
            not in {
                "requirement_violation",
                "assertion_failure",
                "cover_witness",
                "runtime_violation",
            }
            or set(metadata) != expected_metadata
            or any(
                not isinstance(metadata.get(name), str) or not metadata[name]
                for name in ("scope_id", "scope_name", "clause_id", "clause_name")
            )
            or not isinstance(metadata.get("hierarchy_path"), list)
            or not metadata["hierarchy_path"]
            or not all(
                isinstance(item, str) and item
                for item in metadata["hierarchy_path"]
            )
            or (
                "goal_kind" in metadata
                and metadata["goal_kind"] not in {"assert", "ensure", "cover"}
            )
            or (
                metadata.get("source_origin") is not None
                and not isinstance(metadata["source_origin"], dict)
            )
        ):
            raise SimulationPlanError(
                "simulation instrumentation event metadata is invalid"
            )
    _validate_edge_programs(
        payload["edge_programs"],
        nodes,
        domain_names,
        register_names,
        memory_names,
        set(range(len(events))),
    )


def _validate_node_table(
    nodes: object,
    regions: list[dict[str, Any]],
    *,
    policy: SimulationPlanPolicy,
    capture_widths: list[int] | None = None,
    binder_range: tuple[int, int] | None = None,
) -> tuple[int, int]:
    if not isinstance(nodes, list) or len(nodes) > policy.max_nodes:
        raise SimulationPlanError("simulation plan node table is invalid")
    limbs = 0
    for expected, node in enumerate(nodes):
        if (
            not isinstance(node, dict)
            or set(node) != {"id", "op", "width", "operands", "attributes", "origins"}
            or node.get("id") != expected
            or node.get("op") not in PRIMITIVE_OPS
        ):
            raise SimulationPlanError("simulation plan node record is invalid")
        width = node.get("width")
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or not 1 <= width <= policy.max_width
        ):
            raise SimulationPlanError(f"simulation plan node %{expected} has invalid width")
        limbs += (width + 63) // 64
        if not isinstance(node.get("attributes"), dict) or not isinstance(node.get("origins"), list):
            raise SimulationPlanError(f"simulation plan node %{expected} metadata is invalid")
        operands = node.get("operands")
        if not isinstance(operands, list) or any(
            isinstance(item, bool) or not isinstance(item, int) or not 0 <= item < expected
            for item in operands
        ):
            raise SimulationPlanError(f"simulation plan node %{expected} has invalid operand")
        if node["op"] == "constant" and not _validate_u64_limbs(
            node["attributes"].get("limbs"), width
        ):
            raise SimulationPlanError(f"simulation plan constant node %{expected} has invalid limbs")
        _validate_primitive_node(
            node, nodes, regions, policy=policy, capture_widths=capture_widths,
            binder_range=binder_range,
        )
    return len(nodes), limbs


def _validate_primitive_node(
    node: dict[str, Any],
    nodes: list[dict[str, Any]],
    regions: list[dict[str, Any]],
    *,
    policy: SimulationPlanPolicy,
    capture_widths: list[int] | None,
    binder_range: tuple[int, int] | None,
) -> None:
    op = node["op"]
    operands = node["operands"]
    attrs = node["attributes"]
    arity = {
        "constant": 0,
        "load_input": 0,
        "load_state": 0,
        "load_event": 0,
        "load_capture": 0,
        "load_index": 0,
        "load_memory": 1,
        "not": 1,
        "add": 2,
        "sub": 2,
        "mul": 2,
        "and": 2,
        "or": 2,
        "xor": 2,
        "shl": 2,
        "lshr": 2,
        "ashr": 2,
        "eq": 2,
        "ult": 2,
        "ule": 2,
        "slt": 2,
        "sle": 2,
        "extract_bits": 2,
        "select": 3,
        "insert_bits": 3,
    }
    if op in arity and len(operands) != arity[op]:
        raise SimulationPlanError(f"primitive node %{node['id']} has invalid arity")
    if op in {"load_input", "load_state", "load_event", "load_memory"}:
        key = "memory" if op == "load_memory" else "name"
        if set(attrs) != {key} or not isinstance(attrs[key], str) or not attrs[key]:
            raise SimulationPlanError(
                f"primitive node %{node['id']} has invalid storage metadata"
            )
    elif op == "constant":
        if set(attrs) != {"limbs"}:
            raise SimulationPlanError(
                f"primitive node %{node['id']} has invalid constant metadata"
            )
    elif op == "concat_bits":
        widths = attrs.get("operand_widths")
        if (
            set(attrs) != {"operand_widths"}
            or not isinstance(widths, list)
            or len(widths) != len(operands)
            or sum(widths) != node["width"]
        ):
            raise SimulationPlanError(
                f"primitive node %{node['id']} has invalid concat metadata"
            )
    elif op == "load_capture":
        slot = attrs.get("slot")
        if (set(attrs) != {"slot"} or capture_widths is None
            or isinstance(slot, bool) or not isinstance(slot, int)
            or not 0 <= slot < len(capture_widths)
            or node["width"] != capture_widths[slot]):
            raise SimulationPlanError("functional capture slot is invalid")
    elif op == "load_index":
        if attrs or operands or binder_range is None or binder_range[0] < 0 or (
            binder_range[1] - 1 >= (1 << node["width"])
        ):
            raise SimulationPlanError("functional binder value is invalid")
    elif op == "loop_region":
        region_id = attrs.get("region")
        if (set(attrs) != {"region"} or isinstance(region_id, bool)
            or not isinstance(region_id, int) or not 0 <= region_id < len(regions)):
            raise SimulationPlanError("functional region reference is invalid")
        region = regions[region_id]
        if (node["width"] != region["width"]
            or len(operands) != len(region["capture_widths"])
            or any(nodes[value]["width"] != width for value, width in zip(
                operands, region["capture_widths"], strict=True
            ))):
            raise SimulationPlanError("functional region capture shape is invalid")
    elif attrs:
        raise SimulationPlanError(
            f"primitive node %{node['id']} has unexpected metadata"
        )
    if op in {"eq", "ult", "ule", "slt", "sle"} and node["width"] != 1:
        raise SimulationPlanError(
            f"primitive comparison node %{node['id']} must be one bit"
        )
    if op in {
        "add", "sub", "mul", "shl", "lshr", "ashr", "ult", "ule", "slt", "sle",
    } and nodes[operands[0]]["width"] > policy.max_arithmetic_width:
        raise SimulationPlanError(
            f"primitive node %{node['id']} exceeds the "
            f"{policy.max_arithmetic_width}-bit arithmetic bound"
        )
    if op == "select" and nodes[operands[0]]["width"] != 1:
        raise SimulationPlanError(
            f"primitive select node %{node['id']} condition is not one bit"
        )


def _validate_edge_programs(
    programs: object,
    nodes: list[dict[str, Any]],
    domains: set[object],
    registers: set[str],
    memories: set[str],
    events: set[int],
) -> None:
    if not isinstance(programs, list) or len(programs) != len(domains):
        raise SimulationPlanError("primitive edge-program table is invalid")
    seen: set[str] = set()
    for program in programs:
        if (
            not isinstance(program, dict)
            or set(program) != {"clock", "effects", "error", "probes"}
            or program.get("clock") not in domains
            or program["clock"] in seen
            or program.get("error") not in range(len(nodes))
            or not isinstance(program.get("effects"), list)
            or not isinstance(program.get("probes"), list)
        ):
            raise SimulationPlanError("primitive edge-program entry is invalid")
        seen.add(program["clock"])
        for probe in program["probes"]:
            if (
                not isinstance(probe, dict)
                or set(probe) != {"kind", "event", "condition", "once"}
                or probe.get("kind") not in {"check", "cover"}
                or probe.get("event") not in events
                or probe.get("condition") not in range(len(nodes))
                or nodes[probe["condition"]]["width"] != 1
                or not isinstance(probe.get("once"), bool)
                or (probe["kind"] == "check" and probe["once"])
            ):
                raise SimulationPlanError("primitive instrumentation probe is invalid")
        for effect in program["effects"]:
            if not isinstance(effect, dict) or effect.get("op") not in {
                "commit_state",
                "store_memory",
                "fill_memory",
            }:
                raise SimulationPlanError("primitive edge effect is invalid")
            if effect["op"] == "commit_state":
                if (
                    set(effect) != {"op", "target", "node"}
                    or effect.get("target") not in registers
                    or effect.get("node") not in range(len(nodes))
                ):
                    raise SimulationPlanError("primitive state commit is invalid")
            elif effect["op"] == "store_memory":
                if (
                    set(effect) != {"op", "memory", "address", "node", "enable"}
                    or effect.get("memory") not in memories
                    or any(
                        effect.get(key) not in range(len(nodes))
                        for key in ("address", "node", "enable")
                    )
                ):
                    raise SimulationPlanError("primitive memory store is invalid")
            elif (
                set(effect) != {"op", "memory", "node", "enable"}
                or effect.get("memory") not in memories
                or any(
                    effect.get(key) not in range(len(nodes))
                    for key in ("node", "enable")
                )
            ):
                raise SimulationPlanError("primitive memory fill is invalid")


__all__ = ["validate_plan_payload"]
