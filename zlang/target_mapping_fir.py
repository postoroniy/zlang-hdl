"""Deterministic target architecture selection and mapping."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

from zlang.ir import expressions as expr
from zlang.ir import module as ir_module
from zlang.ir import target as target_ir
from zlang.ir.types import FixedType, VecType
from zlang.ir.timing import TimingKnowledge

from zlang import target_catalog as catalog
from zlang import target_mapping_identity as mapping_identity
from zlang import target_mapping_storage as storage_mapping


def map_manual_architecture(module, target, family, resources, template,
                            policy=catalog.ArchitectureSelectionMode.REQUIRED) -> target_ir.ImplementationGraph:
    if template.operation == "async_fifo_memory":
        return storage_mapping._map_async_fifo_memory(module, target, family, resources, template, policy)
    if template.operation == "synchronous_memory":
        return storage_mapping._map_synchronous_memory(module, target, family, resources, template, policy)
    if template.operation != "symmetric_fir_cascade":
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' has unsupported manual operation '{template.operation}'"
        )
    resource = mapping_identity.require_named_resource(resources, template)
    if resource.identity not in family.resource_identities:
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' resource '{resource.identity}' is unavailable on target '{target.identity}'"
        )
    inventory = dict(target.inventory).get(resource.identity, 0)
    if inventory < template.resource_count:
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' resource inventory exceeded: requires "
            f"{template.resource_count} {resource.name}, target provides {inventory}"
        )
    if template.resource_count != 4:
        raise catalog.TargetArchitectureError("the bounded symmetric FIR template requires exactly four resources")
    link = next((item for item in resource.dedicated_links
                 if item.name == template.dedicated_link), None)
    if link is None:
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' requires unavailable dedicated connection "
            f"'{template.dedicated_link}'"
        )
    capacity = dict(((rid, kind), count) for rid, kind, count in target.dedicated_capacities).get(
        (resource.identity, link.name), 0
    )


    required_edges = template.resource_count - 1
    if capacity < required_edges:
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' dedicated connection '{link.name}' "
            f"requires capacity {required_edges}, target provides {capacity}"
        )
    for name, value in template.register_configuration:
        site = next((item for item in resource.register_sites if item.name == name), None)
        if site is None or not site.minimum <= value <= site.maximum:
            supported = "unavailable" if site is None else f"{site.minimum}..{site.maximum}"
            raise catalog.TargetArchitectureError(
                f"architecture '{template.identity}' register site '{name}' value {value} is illegal; supported {supported}"
            )
    selected_pipeline = None
    if template.pipeline_configuration is not None:
        try:
            selected_pipeline = resource.pipeline_configuration(template.pipeline_configuration)
        except ValueError as error:
            raise catalog.TargetArchitectureError(str(error)) from error
        if selected_pipeline.initiation_interval != template.initiation_interval:
            raise catalog.TargetArchitectureError(
                f"architecture '{template.identity}' II {template.initiation_interval} does not match "
                f"pipeline configuration II {selected_pipeline.initiation_interval}"
            )
    pairs, quantization, accumulator, output_register, semantic_latency = _recognize_symmetric_fir(module, template)
    evidence = _validate_widths(template, resource, pairs, accumulator)
    if template.latency != semantic_latency:
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' latency {template.latency} does not match "
            f"semantic fixed latency {semantic_latency}"
        )
    configuration_latency = selected_pipeline.latency if selected_pipeline is not None else 0
    if semantic_latency != configuration_latency + (1 if output_register is not None else 0):
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' pipeline configuration contributes "
            f"{configuration_latency} cycles but semantic output boundary requires {semantic_latency}"
        )
    nodes: list[target_ir.ResourceInstance] = []
    previous = "constant:zero"
    for index, (left, right, coefficient) in enumerate(pairs):
        identity = f"dsp{index}"
        stage_result = "value:" + sha256(
            f"{previous}|{mapping_identity._expression_identity(coefficient)}|{mapping_identity._expression_identity(left)}|{mapping_identity._expression_identity(right)}".encode()
        ).hexdigest()
        mappings = (
            target_ir.SemanticPortMapping("a", mapping_identity._semantic_mapping_identity(left), left),
            target_ir.SemanticPortMapping("d", mapping_identity._semantic_mapping_identity(right), right),
            target_ir.SemanticPortMapping("b", mapping_identity._semantic_mapping_identity(coefficient), coefficient),
            target_ir.SemanticPortMapping("pcin", previous),
            target_ir.SemanticPortMapping("p", stage_result),
            target_ir.SemanticPortMapping("pcout", stage_result),
        )
        physical_configuration = (
            selected_pipeline.physical_settings if selected_pipeline is not None
            else template.register_configuration
        )
        configuration = tuple((*physical_configuration,
                               ("pipeline_configuration", selected_pipeline.name if selected_pipeline else "legacy"),
                               ("sample_left_index", left.index),
                               ("sample_right_index", right.index),
                               ("coefficient_index", coefficient.index)))
        nodes.append(target_ir.ResourceInstance(
            identity, resource.identity, resource.operation, configuration, mappings,
        ))
        previous = stage_result
    edges = tuple(target_ir.DedicatedPhysicalEdge(
        f"pcascade:{index}:{index + 1}", link.name,
        f"dsp{index}", link.source_port, f"dsp{index + 1}",
        link.destination_port, link.width, link.placement_relation, 0,
        link.fabric_fallback,
    ) for index in range(3))
    semantic_identity = mapping_identity._expression_identity(accumulator)
    return target_ir.ImplementationGraph(
        semantic_region_identity=semantic_identity,
        architecture_template_identity=template.identity,
        target_identity=target.identity, target_hash=target.source_hash,
        resource_definition_hashes=((resource.identity, resource.source_hash),),
        resources=tuple(nodes), dedicated_edges=edges,
        latency=template.latency,
        initiation_interval=template.initiation_interval,
        realization_backend="direct_systemverilog",
        latency_knowledge=TimingKnowledge.KNOWN.value,
        quantization=quantization, source_origin=quantization.origin,
        legality_evidence=tuple(evidence),
        architecture_template_hash=template.source_hash,
        target_family_identity=family.identity,
        target_dependency_hashes=target.dependency_hashes,
        architecture_dependency_hashes=template.dependency_hashes,
        selection_policy=catalog.ArchitectureSelectionMode(policy).value,
        target_part=target.part,
        pipeline_configuration_identity=(
            f"{resource.identity}.{selected_pipeline.name}" if selected_pipeline else None
        ),
        active_pipeline_sites=selected_pipeline.sites if selected_pipeline else (),
        physical_binding_identities=tuple(
            f"{resource.identity}:{item.backend}:{item.emitter}"
            for item in resource.physical_bindings
        ),
    )


def map_auto_symmetric_configuration(
    module: ir_module.Module,
    target: target_ir.TargetInstance,
    family: target_ir.TargetFamilyDefinition,
    resources: tuple[target_ir.ResourceDefinition, ...],
    template: target_ir.ArchitectureTemplate,
    configuration: target_ir.PipelineConfiguration,
    *,
    exact_latency: int | None = None,
) -> target_ir.ImplementationGraph:
    """Map one resource-published configuration for the bounded auto FIR slice.

    The temporary register chain is an adapter into the already-validated typed
    manual mapper.  It is not backend RTL and is never emitted.  The returned
    graph receives its real resource-local/compensation timing DAG below.
    """
    explorations = tuple(
        item for item in module.pipeline_explorations
        if isinstance(item.source_expression, expr.FixedConvert)
    )
    if len(explorations) != 1:
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' requires one typed fixed implementation region"
        )
    exploration = explorations[0]
    conversion = exploration.source_expression
    output = next((item for item in module.outputs if item.name == exploration.output), None)
    if output is None:
        raise catalog.TargetArchitectureError(
            f"implementation output '{exploration.output}' is unavailable"
        )
    useful_latency = 1 + configuration.latency
    registers = tuple(ir_module.Register(
        f"__target_auto_q{index}", conversion.type,
        expr.Constant(0, conversion.type),
    ) for index in range(useful_latency))
    next_assignments = [ir_module.NextAssignment(registers[0], conversion)]
    next_assignments.extend(
        ir_module.NextAssignment(registers[index], expr.RegisterRef(
            registers[index - 1].name, conversion.type,
        ))
        for index in range(1, useful_latency)
    )
    assignments = tuple(
        item for item in module.assignments
        if not (hasattr(item.target, "name") and item.target.name == output.name)
    ) + (ir_module.Assignment(output, expr.RegisterRef(registers[-1].name, conversion.type)),)
    adapter = replace(
        module,
        assignments=assignments,
        registers=(*module.registers, *registers),
        next_assignments=(*module.next_assignments, *next_assignments),
    )
    configured_template = replace(
        template,
        latency=useful_latency,
        initiation_interval=configuration.initiation_interval,
        pipeline_configuration=configuration.name,
    )
    graph = map_manual_architecture(
        adapter, target, family, resources, configured_template,
        catalog.ArchitectureSelectionMode.PREFERRED,
    )
    resource = next(
        item for item in resources
        if item.identity == graph.resources[0].resource_definition_identity
    )
    from zlang.target_timing import build_dsp_cascade_timing_dag
    timing_dag = build_dsp_cascade_timing_dag(
        graph, resource, configuration, structural_latency=1,
        output_width=conversion.type.width, exact_latency=exact_latency,
    )
    return replace(
        graph,
        latency=timing_dag.output_latency,
        timing_dag=timing_dag,
        selection_policy="auto",
    )


def _recognize_symmetric_fir(module: ir_module.Module, template: target_ir.ArchitectureTemplate):
    conversions: list[tuple[expr.FixedConvert, ir_module.Register | None]] = []
    for item in module.next_assignments:
        if isinstance(item.expression, expr.FixedConvert):
            conversions.append((item.expression, item.target if isinstance(item.target, ir_module.Register) else None))
    for item in module.assignments:
        if isinstance(item.expression, expr.FixedConvert):
            conversions.append((item.expression, None))
    if len(conversions) != 1:
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' does not cover semantic region: "
            "expected exactly one final FixedConvert"
        )
    conversion, output_register = conversions[0]
    if not (
        isinstance(conversion.type, FixedType)
        and conversion.type.width == 16 and conversion.type.fraction == 14
        and conversion.rounding is expr.FixedRounding.NEAREST_EVEN
        and conversion.overflow is expr.FixedOverflow.SATURATE
    ):
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' requires one final nearest_even/saturating fixed<16,14> quantization"
        )
    terms = _flatten_add(conversion.expression)
    if len(terms) != 8:
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' does not cover semantic region: expected eight products, got {len(terms)}"
        )
    grouped: dict[expr.VectorIndex, list[expr.VectorIndex]] = {}
    for term in terms:
        if not isinstance(term, expr.Binary) or term.operator is not expr.BinaryOperator.MULTIPLY:
            raise catalog.TargetArchitectureError(
                f"architecture '{template.identity}' does not cover semantic region: reduction term is not a product"
            )
        vector_items = tuple(item for item in (term.left, term.right) if isinstance(item, expr.VectorIndex))
        if len(vector_items) != 2:
            raise catalog.TargetArchitectureError(
                f"architecture '{template.identity}' requires vector-indexed sample/coefficient products"
            )
        sample = next((item for item in vector_items if isinstance(item.expression.type, VecType) and item.expression.type.length == 8), None)
        coefficient = next((item for item in vector_items if isinstance(item.expression.type, VecType) and item.expression.type.length == 4), None)
        if sample is None or coefficient is None:
            raise catalog.TargetArchitectureError(
                f"architecture '{template.identity}' requires eight samples and four semantically reused coefficients"
            )
        grouped.setdefault(coefficient, []).append(sample)
    if len(grouped) != 4 or any(len(samples) != 2 for samples in grouped.values()):
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' cannot prove coefficient symmetry from semantic identities"
        )
    pairs = []
    seen = set()
    for coefficient, samples in sorted(grouped.items(), key=lambda item: item[0].index):
        ordered = tuple(sorted(samples, key=lambda item: item.index))
        if ordered[0].index + ordered[1].index != 7:
            raise catalog.TargetArchitectureError(
                f"architecture '{template.identity}' coefficient index {coefficient.index} is not used at mirrored sample taps"
            )
        seen.update(item.index for item in ordered)
        pairs.append((ordered[0], ordered[1], coefficient))
    if seen != set(range(8)):
        raise catalog.TargetArchitectureError(f"architecture '{template.identity}' does not cover all sample taps exactly once")
    semantic_latency = _conversion_output_latency(module, conversion, output_register)
    return tuple(pairs), conversion, conversion.expression, output_register, semantic_latency


def _conversion_output_latency(
    module: ir_module.Module, conversion: expr.FixedConvert, first_register: ir_module.Register | None,
) -> int:
    if first_register is None:
        if any(item.expression == conversion for item in module.assignments):
            return 0
        raise catalog.TargetArchitectureError("fixed conversion is not connected to a module output")
    latency = 1
    current = first_register.name
    visited = {current}
    while True:
        if any(
            isinstance(item.expression, expr.RegisterRef)
            and item.expression.name == current
            for item in module.assignments
        ):
            return latency
        followers = tuple(
            item.target for item in module.next_assignments
            if isinstance(item.target, ir_module.Register)
            and isinstance(item.expression, expr.RegisterRef)
            and item.expression.name == current
        )
        if len(followers) != 1 or followers[0].name in visited:
            raise catalog.TargetArchitectureError(
                "registered fixed conversion must have one deterministic output-delay chain"
            )
        current = followers[0].name
        visited.add(current)
        latency += 1


def _validate_widths(template, resource, pairs, accumulator):
    preadder_limit = resource.limit("preadder")
    b_limit = resource.limit("multiplier_b")
    product_limit = resource.limit("product")
    accumulator_limit = resource.limit("accumulator")
    evidence = []
    for index, (left, right, coefficient) in enumerate(pairs):
        if not isinstance(left.type, FixedType) or not isinstance(right.type, FixedType) or not isinstance(coefficient.type, FixedType):
            raise catalog.TargetArchitectureError(f"architecture '{template.identity}' requires signed fixed-point operands")
        if left.type.fraction != right.type.fraction:
            raise catalog.TargetArchitectureError(f"architecture '{template.identity}' preadder operands require identical scale")
        preadder_width = max(left.type.width, right.type.width) + 1
        if preadder_width > preadder_limit:
            raise catalog.TargetArchitectureError(
                f"architecture '{template.identity}' preadder width exceeded at stage {index}: "
                f"semantic required width {preadder_width}, resource port supports {preadder_limit}"
            )
        if coefficient.type.width > b_limit:
            raise catalog.TargetArchitectureError(
                f"architecture '{template.identity}' multiplier input B width exceeded at stage {index}: "
                f"semantic required width {coefficient.type.width}, resource port supports {b_limit}"
            )
        product_width = preadder_width + coefficient.type.width
        if product_width > product_limit:
            raise catalog.TargetArchitectureError(
                f"architecture '{template.identity}' multiplication width exceeded at stage {index}: "
                f"semantic required width {product_width}, resource supports {product_limit}"
            )
        evidence.append(f"stage {index}: preadder {preadder_width}<={preadder_limit}, coefficient {coefficient.type.width}<={b_limit}, product {product_width}<={product_limit}")
    if not isinstance(accumulator.type, FixedType):
        raise catalog.TargetArchitectureError(f"architecture '{template.identity}' requires a signed fixed-point accumulator")
    if accumulator.type.width > accumulator_limit:
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' accumulator width exceeded: semantic required width "
            f"{accumulator.type.width}, resource P/PCIN supports {accumulator_limit}"
        )
    evidence.append(f"accumulator {accumulator.type.width}<={accumulator_limit}")
    evidence.append("one final FixedConvert remains outside the resource cascade")
    return evidence


def _flatten_add(value: expr.Expression) -> tuple[expr.Expression, ...]:
    if isinstance(value, expr.Add):
        return (*_flatten_add(value.left), *_flatten_add(value.right))
    return (value,)
