"""Python-owned flattening of scalar hierarchy into one primitive plan.

The native runtime must not know about modules, instances, or port bindings.
This composer lowers every hierarchy frame independently through the ordinary
leaf SimulationPlan path, then links those language-neutral machines into one
topologically ordered primitive graph.  Protocol composition deliberately
remains outside this slice.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
import hashlib
import json
from typing import Callable

from zlang.ir import expressions as expr
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import Assignment, Module, Port, PortDirection


class HierarchicalSimulationError(ValueError):
    """A hierarchy cannot be represented by the scalar primitive composer."""


@dataclass(frozen=True)
class _ShellBoundary:
    child_outputs: dict[tuple[str, str], str]
    child_inputs: dict[tuple[str, str], str]
    original_inputs: frozenset[str]
    original_outputs: frozenset[str]


@dataclass
class _Frame:
    path: tuple[str, ...]
    module: Module
    plan: object
    boundary: _ShellBoundary
    domain_map: dict[str, str]
    reset_map: dict[str, str]
    parent: "_Frame | None" = None
    parent_instance: str | None = None
    children: tuple["_Frame", ...] = ()


def _private_name(kind: str, path: tuple[str, ...], owner: str, port: str) -> str:
    payload = json.dumps(
        (kind, path, owner, port), separators=(",", ":"), ensure_ascii=True
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"__zlang_hier_{kind}_{digest}"


def _allocate_private_name(
    kind: str,
    path: tuple[str, ...],
    owner: str,
    port: str,
    used: set[str],
) -> str:
    base = _private_name(kind, path, owner, port)
    candidate = base
    suffix = 0
    while candidate in used:
        suffix += 1
        candidate = f"{base}_{suffix}"
    used.add(candidate)
    return candidate


def _rewrite_instance_outputs(
    value: object,
    names: dict[tuple[str, str], str],
    memo: dict[int, tuple[object, object]],
) -> object:
    cached = memo.get(id(value))
    if cached is not None and cached[0] is value:
        return cached[1]
    if isinstance(value, expr.InstanceOutputRef):
        try:
            name = names[(value.instance, value.port)]
        except KeyError as error:
            raise HierarchicalSimulationError(
                f"unresolved hierarchical output '{value.instance}.{value.port}'"
            ) from error
        result: object = expr.InputRef(
            name,
            value.type,
            origin=value.origin,
        )
    elif isinstance(value, tuple):
        rewritten = tuple(
            _rewrite_instance_outputs(item, names, memo) for item in value
        )
        result = value if all(a is b for a, b in zip(rewritten, value, strict=True)) else rewritten
    elif isinstance(value, list):
        rewritten_list = [
            _rewrite_instance_outputs(item, names, memo) for item in value
        ]
        result = (
            value
            if all(a is b for a, b in zip(rewritten_list, value, strict=True))
            else rewritten_list
        )
    elif isinstance(value, dict):
        rewritten_dict = {
            key: _rewrite_instance_outputs(item, names, memo)
            for key, item in value.items()
        }
        result = (
            value
            if all(rewritten_dict[key] is item for key, item in value.items())
            else rewritten_dict
        )
    elif is_dataclass(value) and not isinstance(value, type):
        updates = {}
        for field in fields(value):
            if not field.init or field.name in {"origin", "source_origin"}:
                continue
            current = getattr(value, field.name)
            rewritten = _rewrite_instance_outputs(current, names, memo)
            if rewritten is not current:
                updates[field.name] = rewritten
        result = replace(value, **updates) if updates else value
    else:
        result = value
    memo[id(value)] = (value, result)
    return result


def _scalar_shell(
    module: Module,
    path: tuple[str, ...],
) -> tuple[Module, _ShellBoundary]:
    if any(port.protocol is not InterfaceProtocol.WIRE for port in module.ports):
        raise HierarchicalSimulationError(
            f"hierarchical module '{module.name}' has protocol ports"
        )
    if any(
        (
            module.connections,
            module.hierarchical_connections,
            module.request_response_connections,
            module.aggregate_protocol_connections,
            module.protocol_endpoints,
            module.aggregate_protocol_endpoints,
        )
    ):
        raise HierarchicalSimulationError(
            "protocol hierarchy must be lowered before primitive plan construction"
        )
    if module.instances and not module.elaborated_instances:
        raise HierarchicalSimulationError(
            f"module '{module.name}' has instances without physical elaboration"
        )
    if len(module.elaborated_instances) != len(module.children):
        raise HierarchicalSimulationError(
            f"module '{module.name}' hierarchy records are incomplete"
        )

    child_outputs: dict[tuple[str, str], str] = {}
    child_inputs: dict[tuple[str, str], str] = {}
    synthetic_ports: list[Port] = []
    synthetic_assignments: list[Assignment] = []
    used_port_names = {port.name for port in module.ports}
    child_by_name = {}
    for elaborated, child in zip(
        module.elaborated_instances, module.children, strict=True
    ):
        owner = elaborated.instance.name
        if owner in child_by_name:
            raise HierarchicalSimulationError(
                f"duplicate physical child instance '{owner}'"
            )
        child_by_name[owner] = child
        for port in child.outputs:
            name = _allocate_private_name(
                "out", path, owner, port.name, used_port_names
            )
            child_outputs[(owner, port.name)] = name
            synthetic_ports.append(
                Port(PortDirection.INPUT, name, port.type, domain=port.domain)
            )

    memo: dict[int, tuple[object, object]] = {}
    bindings = {(item.instance, item.port): item for item in module.instance_bindings}
    if len(bindings) != len(module.instance_bindings):
        raise HierarchicalSimulationError(
            f"module '{module.name}' has duplicate instance input bindings"
        )
    for owner, child in child_by_name.items():
        for port in child.inputs:
            binding = bindings.get((owner, port.name))
            if binding is None:
                raise HierarchicalSimulationError(
                    f"instance input '{owner}.{port.name}' is unbound"
                )
            if binding.expression.type != port.type:
                raise HierarchicalSimulationError(
                    f"instance input '{owner}.{port.name}' changed type"
                )
            name = _allocate_private_name(
                "in", path, owner, port.name, used_port_names
            )
            child_inputs[(owner, port.name)] = name
            target = Port(PortDirection.OUTPUT, name, port.type, domain=port.domain)
            synthetic_ports.append(target)
            expression = _rewrite_instance_outputs(
                binding.expression, child_outputs, memo
            )
            assert isinstance(expression, expr.Expression)
            synthetic_assignments.append(Assignment(target, expression))
    extra = set(bindings) - set(child_inputs)
    if extra:
        owner, port = sorted(extra)[0]
        raise HierarchicalSimulationError(
            f"unknown instance input binding '{owner}.{port}'"
        )

    skip = {
        "ports",
        "assignments",
        "instances",
        "instance_bindings",
        "children",
        "elaborated_instances",
        "hierarchical_connections",
        "request_response_connections",
        "aggregate_protocol_connections",
        "instance_output_timings",
    }
    updates = {}
    for field in fields(module):
        if not field.init or field.name in skip:
            continue
        current = getattr(module, field.name)
        rewritten = _rewrite_instance_outputs(current, child_outputs, memo)
        if rewritten is not current:
            updates[field.name] = rewritten
    assignments = tuple(
        _rewrite_instance_outputs(item, child_outputs, memo)
        for item in module.assignments
    )
    shell = replace(
        module,
        **updates,
        ports=(*module.ports, *synthetic_ports),
        assignments=(*assignments, *synthetic_assignments),
        instances=(),
        instance_bindings=(),
        children=(),
        elaborated_instances=(),
        hierarchical_connections=(),
        request_response_connections=(),
        aggregate_protocol_connections=(),
        instance_output_timings=(),
    )
    return shell, _ShellBoundary(
        child_outputs,
        child_inputs,
        frozenset(port.name for port in module.inputs),
        frozenset(port.name for port in module.outputs),
    )


def _build_frames(
    module: Module,
    build_leaf: Callable[[Module], object],
    *,
    path: tuple[str, ...],
    domain_map: dict[str, str],
    reset_map: dict[str, str],
    parent: _Frame | None = None,
    parent_instance: str | None = None,
) -> _Frame:
    shell, boundary = _scalar_shell(module, path)
    plan = build_leaf(shell)
    frame = _Frame(
        path,
        module,
        plan,
        boundary,
        domain_map,
        reset_map,
        parent,
        parent_instance,
    )
    children = []
    for elaborated, child in zip(
        module.elaborated_instances, module.children, strict=True
    ):
        child_domain_map: dict[str, str] = {}
        child_reset_map: dict[str, str] = {}
        child_domains = child.clock_domains
        if child_domains:
            for child_domain in child_domains:
                local_clock = child_domain.clock
                if local_clock not in domain_map:
                    raise HierarchicalSimulationError(
                        f"child '{elaborated.instance.name}' clock "
                        f"'{local_clock}' has no mapped physical clock"
                    )
                child_domain_map[local_clock] = domain_map[local_clock]
                local_reset = child_domain.reset
                if local_reset is None:
                    continue
                if local_reset not in reset_map:
                    raise HierarchicalSimulationError(
                        f"child '{elaborated.instance.name}' reset "
                        f"'{local_reset}' has no mapped physical reset"
                    )
                child_reset_map[local_reset] = reset_map[local_reset]
        elif child.clock is not None:
            physical_clock = elaborated.clock
            if physical_clock is None or physical_clock not in domain_map:
                raise HierarchicalSimulationError(
                    f"child '{elaborated.instance.name}' has no mapped physical clock"
                )
            child_domain_map[child.clock] = domain_map[physical_clock]
            local_reset = child.reset
            if local_reset is not None:
                physical_reset = elaborated.reset
                if physical_reset is None or physical_reset not in reset_map:
                    raise HierarchicalSimulationError(
                        f"child '{elaborated.instance.name}' has no mapped physical reset"
                    )
                child_reset_map[local_reset] = reset_map[physical_reset]
        children.append(
            _build_frames(
                child,
                build_leaf,
                path=(*path, elaborated.instance.name),
                domain_map=child_domain_map,
                reset_map=child_reset_map,
                parent=frame,
                parent_instance=elaborated.instance.name,
            )
        )
    frame.children = tuple(children)
    return frame


def _preorder(root: _Frame) -> tuple[_Frame, ...]:
    result = []
    pending = [root]
    while pending:
        current = pending.pop()
        result.append(current)
        pending.extend(reversed(current.children))
    return tuple(result)


def _state_name(frame: _Frame, name: str) -> str:
    if frame.parent is None:
        return name
    return ".".join((*frame.path[1:], name))


def _domain(frame: _Frame, name: str | None) -> str | None:
    if name is None:
        return None
    try:
        return frame.domain_map[name]
    except KeyError as error:
        raise HierarchicalSimulationError(
            f"hierarchy frame {'.'.join(frame.path)} has unmapped clock '{name}'"
        ) from error


def compose_hierarchical_primitive_payload(
    module: Module,
    build_leaf: Callable[[Module], object],
    *,
    max_nodes: int,
) -> dict[str, object]:
    """Return one primitive payload with all scalar hierarchy erased."""

    root_domains = tuple(domain.clock for domain in module.clock_domains)
    if not root_domains and module.clock:
        root_domains = (module.clock,)
    root_resets = tuple(
        domain.reset for domain in module.clock_domains if domain.reset is not None
    )
    if not root_resets and module.reset:
        root_resets = (module.reset,)
    root = _build_frames(
        module,
        build_leaf,
        path=(module.name,),
        domain_map={name: name for name in root_domains},
        reset_map={name: name for name in root_resets},
    )
    frames = _preorder(root)
    output_nodes = {
        (frame.path, str(item["name"])): int(item["node"])
        for frame in frames
        for item in frame.plan.payload["outputs"]
    }
    child_by_owner = {
        (frame.path, child.parent_instance): child
        for frame in frames
        for child in frame.children
    }
    node_cache: dict[tuple[tuple[str, ...], int], int] = {}
    pure_cache: dict[str, int] = {}
    origin_cache: set[tuple[int, str]] = set()
    active: set[tuple[tuple[str, ...], int]] = set()
    nodes: list[dict[str, object]] = []

    def linked_input(frame: _Frame, name: str) -> tuple[_Frame, int] | None:
        for (owner, port), private in frame.boundary.child_outputs.items():
            if private != name:
                continue
            child = child_by_owner[(frame.path, owner)]
            return child, output_nodes[(child.path, port)]
        if frame.parent is not None and name in frame.boundary.original_inputs:
            assert frame.parent_instance is not None
            private = frame.parent.boundary.child_inputs[
                (frame.parent_instance, name)
            ]
            return frame.parent, output_nodes[(frame.parent.path, private)]
        return None

    def origin_with_path(origin: object, frame: _Frame) -> object:
        if isinstance(origin, dict):
            return {**origin, "hierarchy_path": list(frame.path)}
        return origin

    def lower(frame: _Frame, identifier: int) -> int:
        root = (frame.path, identifier)
        stack = [(frame, identifier, False)]
        while stack:
            current, current_id, ready = stack.pop()
            key = (current.path, current_id)
            if key in node_cache:
                continue
            source = current.plan.payload["nodes"][current_id]
            linked = (
                linked_input(current, str(source["attributes"]["name"]))
                if source["op"] == "load_input"
                else None
            )
            if not ready:
                if key in active:
                    raise HierarchicalSimulationError(
                        "hierarchical combinational dependency graph contains a cycle"
                    )
                active.add(key)
                stack.append((current, current_id, True))
                dependencies = (linked,) if linked is not None else tuple(
                    (current, int(item)) for item in source["operands"]
                )
                for dependency, dependency_id in reversed(dependencies):
                    if (dependency.path, dependency_id) not in node_cache:
                        stack.append((dependency, dependency_id, False))
                continue
            if linked is not None:
                node_cache[key] = node_cache[(linked[0].path, linked[1])]
                active.remove(key)
                continue
            operands = [
                node_cache[(current.path, int(item))]
                for item in source["operands"]
            ]
            attributes = dict(source["attributes"])
            if source["op"] == "load_state":
                name = str(attributes["name"])
                if name.startswith("$release:"):
                    clock = name.removeprefix("$release:")
                    attributes["name"] = "$release:" + str(_domain(current, clock))
                else:
                    attributes["name"] = _state_name(current, name)
            elif source["op"] == "load_event":
                attributes["name"] = _domain(current, str(attributes["name"]))
            elif source["op"] == "load_memory":
                attributes["memory"] = _state_name(
                    current, str(attributes["memory"])
                )
            elif source["op"] == "load_input":
                name = str(attributes["name"])
                if name.startswith("$reset:"):
                    local = name.removeprefix("$reset:")
                    try:
                        attributes["name"] = "$reset:" + current.reset_map[local]
                    except KeyError as error:
                        raise HierarchicalSimulationError(
                            f"hierarchy frame {'.'.join(current.path)} has unmapped "
                            f"reset '{local}'"
                        ) from error
                elif current.parent is not None:
                    raise HierarchicalSimulationError(
                        f"unlinked child input '{'.'.join(current.path)}.{name}'"
                    )
            content = {
                "op": source["op"],
                "width": source["width"],
                "operands": operands,
                "attributes": attributes,
            }
            identity = json.dumps(
                content, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            result = pure_cache.get(identity)
            if result is None:
                result = len(nodes)
                pure_cache[identity] = result
                nodes.append({**content, "id": result, "origins": []})
                if len(nodes) > max_nodes:
                    raise HierarchicalSimulationError(
                        f"flattened hierarchy exceeds the {max_nodes}-node plan bound"
                    )
            for origin in source.get("origins", []):
                projected = origin_with_path(origin, current)
                origin_identity = json.dumps(
                    projected, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                )
                if (result, origin_identity) not in origin_cache:
                    nodes[result]["origins"].append(projected)
                    origin_cache.add((result, origin_identity))
            node_cache[key] = result
            active.remove(key)
        return node_cache[root]

    registers = []
    memories = []
    events: list[dict[str, object]] = []
    event_ids: dict[tuple[tuple[str, ...], int], int] = {}
    for frame in frames:
        for event in frame.plan.payload["events"]:
            identifier = len(events)
            event_ids[(frame.path, int(event["id"]))] = identifier
            metadata = dict(event["metadata"])
            metadata["hierarchy_path"] = list(frame.path)
            events.append({"id": identifier, "metadata": metadata})
    effects_by_clock: dict[str, list[dict[str, object]]] = {
        name: [] for name in root_domains
    }
    probes_by_clock: dict[str, list[dict[str, object]]] = {
        name: [] for name in root_domains
    }
    errors_by_clock: dict[str, list[int]] = {name: [] for name in root_domains}
    for frame in frames:
        for item in frame.plan.payload["registers"]:
            registers.append(
                {
                    **item,
                    "name": _state_name(frame, str(item["name"])),
                    "domain": _domain(frame, item["domain"]),
                }
            )
        for item in frame.plan.payload["memories"]:
            memories.append(
                {
                    **item,
                    "name": _state_name(frame, str(item["name"])),
                    "domain": _domain(frame, str(item["domain"])),
                }
            )
        for program in frame.plan.payload["edge_programs"]:
            clock = _domain(frame, str(program["clock"]))
            assert clock is not None
            errors_by_clock.setdefault(clock, []).append(
                lower(frame, int(program["error"]))
            )
            destination = effects_by_clock.setdefault(clock, [])
            for effect in program["effects"]:
                rewritten = dict(effect)
                if "target" in rewritten:
                    rewritten["target"] = _state_name(
                        frame, str(rewritten["target"])
                    )
                if "memory" in rewritten:
                    rewritten["memory"] = _state_name(
                        frame, str(rewritten["memory"])
                    )
                for name in ("node", "address", "enable"):
                    if name in rewritten:
                        rewritten[name] = lower(frame, int(rewritten[name]))
                destination.append(rewritten)
            probe_destination = probes_by_clock.setdefault(clock, [])
            for probe in program["probes"]:
                probe_destination.append(
                    {
                        **probe,
                        "event": event_ids[(frame.path, int(probe["event"]))],
                        "condition": lower(frame, int(probe["condition"])),
                    }
                )

    def combine_errors(clock: str) -> int:
        errors = errors_by_clock.get(clock, [])
        if not errors:
            result = len(nodes)
            nodes.append(
                {
                    "id": result,
                    "op": "constant",
                    "width": 1,
                    "operands": [],
                    "attributes": {"limbs": [0]},
                    "origins": [],
                }
            )
            return result
        result = errors[0]
        for error in errors[1:]:
            identifier = len(nodes)
            nodes.append(
                {
                    "id": identifier,
                    "op": "or",
                    "width": 1,
                    "operands": [result, error],
                    "attributes": {},
                    "origins": [],
                }
            )
            result = identifier
        return result

    root_payload = root.plan.payload
    real_ports = [
        item
        for item in root_payload["ports"]
        if item["name"] in root.boundary.original_inputs
        or item["name"] in root.boundary.original_outputs
    ]
    outputs = [
        {
            "name": name,
            "node": lower(root, output_nodes[(root.path, name)]),
        }
        for name in sorted(root.boundary.original_outputs)
    ]
    edge_programs = [
        {
            "clock": str(item["clock"]),
            "effects": effects_by_clock.get(str(item["clock"]), []),
            "error": combine_errors(str(item["clock"])),
            "probes": probes_by_clock.get(str(item["clock"]), []),
        }
        for item in root_payload["domains"]
    ]
    if len(nodes) > max_nodes:
        raise HierarchicalSimulationError(
            f"flattened hierarchy exceeds the {max_nodes}-node plan bound"
        )
    frame_identities = [
        {
            "path": list(frame.path),
            "canonical_ir_identity": frame.plan.payload[
                "canonical_ir_identity"
            ],
        }
        for frame in frames
    ]
    composite_identity = hashlib.sha256(
        json.dumps(
            frame_identities,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        **root_payload,
        "canonical_ir_identity": f"hierarchical:{composite_identity}",
        "identity": "",
        "ports": real_ports,
        "nodes": nodes,
        "outputs": outputs,
        "registers": registers,
        "memories": memories,
        "events": events,
        "domains": list(root_payload["domains"]),
        "edge_programs": edge_programs,
    }


__all__ = [
    "HierarchicalSimulationError",
    "compose_hierarchical_primitive_payload",
]
