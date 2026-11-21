"""Python-owned flattening of scalar hierarchy into one primitive plan.

The native runtime must not know about modules, instances, or port bindings.
This composer lowers every hierarchy frame independently through the ordinary
leaf SimulationPlan path, then links those language-neutral machines into one
topologically ordered primitive graph.  Protocol composition deliberately
remains outside this slice.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
import hashlib
import json
from typing import Callable

from zlang.ir import expressions as expr
from zlang.ir.hierarchy import csr_access_constants
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import Assignment, InstancePortBinding, Module, Port, PortDirection
from zlang.ir.static_projection import StaticView, project_static_bits, static_parts, static_view
from zlang.simulation_rewrite import SimulationExpressionRewriter
from zlang.simulation_csr import lower_csr_module


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


class _InstanceOutputRewriter(SimulationExpressionRewriter):
    """Resolve child outputs through the shared immutable rewrite owner."""

    def __init__(self, names: dict[tuple[str, str], str]) -> None:
        super().__init__()
        self._names = names

    def rewrite_special(
        self,
        value: expr.Expression,
    ) -> expr.Expression | None:
        if not isinstance(value, expr.InstanceOutputRef):
            return None
        try:
            name = self._names[(value.instance, value.port)]
        except KeyError as error:
            raise HierarchicalSimulationError(
                f"unresolved hierarchical output '{value.instance}.{value.port}'"
            ) from error
        return expr.InputRef(name, value.type, origin=value.origin)


class _ChildOutputProjectionRewriter(SimulationExpressionRewriter):
    """Materialize observed child slices before linking scalar leaf plans."""

    def __init__(
        self, module: Module, path: tuple[str, ...], children: dict[str, Module],
    ) -> None:
        super().__init__()
        self.children = children
        self.locals = {item.name: item.expression for item in module.locals}
        self.path = path
        self.projections: dict[str, dict[Port, tuple[str, int, int]]] = {}
        self.ports: dict[tuple[str, str, int, int, object], Port] = {}
        self.used_names: dict[str, set[str]] = {}
        self.output_assignments = {
            (owner, item.target.name): item.expression
            for owner, child in children.items()
            for item in child.assignments
            if isinstance(item.target, Port) and item.target.direction is PortDirection.OUTPUT
            and not item.target.registered
            and item.signal is None and item.channel is None
        }

    def rewrite_special(self, value: expr.Expression) -> expr.Expression | None:
        selected = static_view(value)
        if selected is None:
            return None
        parts = iter(static_parts(selected, self.locals))
        head = next(parts, None)
        if head is None:
            return None
        reference, msb, lsb = head
        if not isinstance(reference, expr.InstanceOutputRef) or next(parts, None) is not None:
            return None
        owner = reference.instance
        child = self.children.get(owner)
        if child is None:
            return None
        csr_read = child.csr_access is not None and reference.port == child.csr_access.read_data_port
        if not csr_read and (owner, reference.port) not in self.output_assignments:
            return None
        if not csr_read and (lsb, msb) == (0, reference.type.width - 1):
            return None
        key = (owner, reference.port, msb, lsb, value.type)
        port = self.ports.get(key)
        if port is None:
            names = self.used_names.setdefault(owner, {item.name for item in child.ports})
            kind = "csr_read" if csr_read else "view"
            label = (
                f"{(msb, lsb)}:{value.type}" if csr_read
                else f"{reference.port}:{msb}:{lsb}:{value.type}"
            )
            name = _allocate_private_name(
                kind, self.path, owner, label, names,
            )
            port = Port(PortDirection.OUTPUT, name, value.type, domain=reference.domain)
            self.ports[key] = port
            self.projections.setdefault(owner, {})[port] = (reference.port, msb, lsb)
        return expr.InstanceOutputRef(
            owner, port.name, port.type, domain=port.domain, origin=value.origin,
        )


class _ProjectedChildInputs(SimulationExpressionRewriter):
    """Bind a slice of a child's input without demanding its other bits."""

    def __init__(
        self,
        child: Module,
        owner: str,
        parent: _ChildOutputProjectionRewriter,
        bindings: dict[tuple[str, str], expr.Expression],
    ) -> None:
        super().__init__()
        self.owner = owner
        self.parent = parent
        self.bindings = bindings
        self.inputs = {port.name: port for port in child.inputs}
        self.locals = {item.name: item.expression for item in child.locals}
        self.projected: dict[tuple[str, int, int, object], Port] = {}
        self.new_bindings: list[InstancePortBinding] = []
        self.used_names = {port.name for port in child.ports}

    def rewrite_special(self, value: expr.Expression) -> expr.Expression | None:
        selected = static_view(value)
        if selected is None:
            return None
        parts = iter(static_parts(selected, self.locals))
        head = next(parts, None)
        if head is None:
            return None
        reference, msb, lsb = head
        if (
            not isinstance(reference, expr.InputRef)
            or reference.name not in self.inputs
            or next(parts, None) is not None
            or (lsb, msb) == (0, reference.type.width - 1)
        ):
            return None
        key = (reference.name, msb, lsb, value.type)
        port = self.projected.get(key)
        if port is None:
            bound = self.bindings[(self.owner, reference.name)]
            if isinstance(bound, (expr.InputRef, expr.RegisterRef, expr.Constant)) and (
                not isinstance(bound, expr.InputRef) or bound.name not in self.parent.locals
            ):
                # The full scalar leaf is already a current-cycle boundary.
                return None
            narrowed = project_static_bits(
                StaticView(bound, msb, lsb), self.parent.locals, value.type,
            )
            name = _allocate_private_name(
                "view_in", self.parent.path, self.owner,
                f"{reference.name}:{msb}:{lsb}:{value.type}", self.used_names,
            )
            port = Port(
                PortDirection.INPUT, name, value.type,
                domain=self.inputs[reference.name].domain,
            )
            self.projected[key] = port
            self.new_bindings.append(InstancePortBinding(
                self.owner, port.name, self.parent.expression(narrowed),
            ))
        return expr.InputRef(port.name, port.type, origin=value.origin)


def _prepare_child_projections(module: Module, path: tuple[str, ...]) -> Module:
    children = {
        instance.instance.name: child
        for instance, child in zip(module.elaborated_instances, module.children, strict=True)
    }
    if not children:
        return module
    rewriter = _ChildOutputProjectionRewriter(module, path, children)
    updates = {}
    for field in fields(module):
        if not field.init or field.name in {"children", "elaborated_instances", "instances"}:
            continue
        value = getattr(module, field.name)
        rewritten = rewriter.value(value)
        if rewritten is not value:
            updates[field.name] = rewritten
    bindings = {(item.instance, item.port): item.expression for item in module.instance_bindings}
    constants = {
        owner: csr_access_constants(child, owner, bindings, rewriter.locals, {})
        for owner, child in children.items()
    }
    prepared = {}
    by_owner_bindings: dict[str, list[InstancePortBinding]] = {}
    projection_counts: dict[str, int] = {}
    # Narrowing an input binding may request another child's output. Revisit
    # only owners that acquire new views, and keep source bindings immutable.
    while any(
        projection_counts.get(owner) != len(rewriter.projections.get(owner, ()))
        for owner in children
    ):
        for owner, child in children.items():
            projections = rewriter.projections.get(owner, {})
            if projection_counts.get(owner) == len(projections):
                continue
            projection_counts[owner] = len(projections)
            read_data = child.csr_access.read_data_port if child.csr_access else None
            csr = {p: (msb, lsb) for p, (name, msb, lsb) in projections.items() if name == read_data}
            lowered = (
                lower_csr_module(child, access_constants=constants[owner], read_projections=csr)
                if constants[owner] or csr else child
            )
            locals_ = {item.name: item.expression for item in child.locals}
            ordinary = tuple(
                Assignment(port, project_static_bits(
                    StaticView(rewriter.output_assignments[owner, name], msb, lsb), locals_, port.type,
                ))
                for port, (name, msb, lsb) in projections.items() if name != read_data
            )
            inputs = _ProjectedChildInputs(child, owner, rewriter, bindings)
            projected = set(projections) | {
                item.target for item in child.assignments
                if (owner, item.target.name) in rewriter.output_assignments
            }
            prepared[owner] = replace(
                lowered,
                ports=(*lowered.ports, *(item.target for item in ordinary)),
                assignments=tuple(
                    inputs.value(item) if item.target in projected else item
                    for item in (*lowered.assignments, *ordinary)
                ),
            )
            if inputs.projected:
                prepared[owner] = replace(
                    prepared[owner], ports=(*prepared[owner].ports, *inputs.projected.values()),
                )
            by_owner_bindings[owner] = inputs.new_bindings
    updated_bindings = updates.pop("instance_bindings", module.instance_bindings)
    return replace(
        module, **updates, children=tuple(prepared[owner] for owner in children),
        instance_bindings=(*updated_bindings, *(
            item for owner in children for item in by_owner_bindings[owner]
        )),
    )


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

    output_rewriter = _InstanceOutputRewriter(child_outputs)
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
            expression = output_rewriter.expression(binding.expression)
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
        rewritten = output_rewriter.value(current)
        if rewritten is not current:
            updates[field.name] = rewritten
    assignments = tuple(
        output_rewriter.value(item)
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
    prepared = _prepare_child_projections(module, path)
    shell, boundary = _scalar_shell(prepared, path)
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
        prepared.elaborated_instances, prepared.children, strict=True
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
    regions: list[dict[str, object]] = []
    region_ids: dict[tuple[tuple[str, ...], int], int] = {}

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
        if name in frame.boundary.original_outputs:
            # Child binding expressions may read a driven output of this frame,
            # including the destination of an explicit clock crossing.
            return frame, output_nodes[(frame.path, name)]
        return None

    def origin_with_path(origin: object, frame: _Frame) -> object:
        if isinstance(origin, dict):
            return {**origin, "hierarchy_path": list(frame.path)}
        return origin

    # Functional-region identifiers are local to each leaf plan.  Preserve the
    # already validated region body and remap only nested region references;
    # captures remain operands of the owning loop node.  Region tables are
    # ordered bottom-up by construction, so every nested reference is already
    # available when its parent is copied.
    for frame in frames:
        for local_id, source_region in enumerate(frame.plan.payload["regions"]):
            body: list[dict[str, object]] = []
            for source_node in source_region["nodes"]:
                attributes = dict(source_node["attributes"])
                if source_node["op"] == "loop_region":
                    nested = int(attributes["region"])
                    try:
                        attributes["region"] = region_ids[(frame.path, nested)]
                    except KeyError as error:
                        raise HierarchicalSimulationError(
                            "functional region references a non-preceding local region"
                        ) from error
                body.append(
                    {
                        **source_node,
                        "attributes": attributes,
                        "origins": [
                            origin_with_path(origin, frame)
                            for origin in source_node.get("origins", [])
                        ],
                    }
                )
            region_ids[(frame.path, local_id)] = len(regions)
            regions.append({**source_region, "nodes": body})

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
            elif source["op"] == "loop_region":
                attributes["region"] = region_ids[
                    (current.path, int(attributes["region"]))
                ]
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
                for name in ("node", "address", "enable", "refresh"):
                    if name in rewritten and rewritten[name] is not None:
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
        "regions": regions,
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
