"""Deterministic target architecture selection and mapping."""

from __future__ import annotations

from dataclasses import replace

from zlang.ir import expressions as expr
from zlang.ir.module import Module
from zlang.ir import target as target_ir
from zlang.ir.timing import TimingKnowledge
from zlang.ir import signed_reductions
from zlang.ir import scheduled as scheduled_ir
from zlang.ir.traversal import walk_expression

from zlang import target_catalog as catalog
from zlang import target_mapping_identity as mapping_identity


def map_auto_signed_product_configuration(
    module: Module,
    target: target_ir.TargetInstance,
    family: target_ir.TargetFamilyDefinition,
    resources: tuple[target_ir.ResourceDefinition, ...],
    template: target_ir.ArchitectureTemplate,
    configuration: target_ir.PipelineConfiguration,
    *,
    exact_latency: int | None = None,
    source_expression: expr.Expression | None = None,
) -> target_ir.ImplementationGraph:
    """Map an ordered exact signed-product reduction without changing its tree."""

    if template.operation != "signed_product_reduction":
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' is not a signed-product reduction"
        )
    if source_expression is None:
        explorations = tuple(
            item for item in module.pipeline_explorations
            if isinstance(item.source_expression, expr.FixedConvert)
        )
        if len(explorations) != 1:
            raise catalog.TargetArchitectureError(
                f"architecture '{template.identity}' requires one typed fixed implementation region"
            )
        conversion = explorations[0].source_expression
    else:
        conversion = source_expression
    arithmetic, boundary, boundary_evidence = catalog._dsp_value_expression(conversion)
    reduction = signed_reductions.recognize_signed_product_reduction(arithmetic)
    if reduction is None:
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' requires one exact signed-product reduction"
        )
    if not isinstance(reduction.result_type, catalog._DSP_NUMERIC_TYPES) or any(
        not isinstance(term.product_type, catalog._DSP_NUMERIC_TYPES)
        for term in reduction.terms
    ):
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' requires exact integer/fixed product arithmetic"
        )
    if template.resource_count != len(reduction.terms):
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' requires {template.resource_count} products, "
            f"typed reduction has {len(reduction.terms)}"
        )

    resource = mapping_identity.require_named_resource(resources, template)
    if resource.identity not in family.resource_identities:
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' resource '{resource.identity}' is unavailable on target '{target.identity}'"
        )
    catalog.validate_inventory(target, ((resource.identity, len(reduction.terms)),))
    capabilities = dict(resource.capabilities)
    modes = set(capabilities.get("accumulator_modes", "").split("."))
    required_modes = {"accumulator_plus_product"}
    if reduction.has_subtraction:
        required_modes.add("accumulator_minus_product")
    missing_modes = sorted(required_modes - modes)
    if missing_modes:
        raise catalog.TargetArchitectureError(
            f"resource '{resource.identity}' lacks signed reduction capability '{missing_modes[0]}'"
        )
    if capabilities.get("signed") != "true":
        raise catalog.TargetArchitectureError(
            f"resource '{resource.identity}' does not advertise signed arithmetic"
        )
    configuration = catalog.validate_pipeline_configuration(resource, configuration.name)

    link = next((item for item in resource.dedicated_links
                 if item.name == template.dedicated_link), None)
    if len(reduction.terms) > 1 and link is None:
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' requires unavailable dedicated connection "
            f"'{template.dedicated_link}'"
        )
    required_edges = len(reduction.terms) - 1
    capacity = dict(((rid, kind), count)
                    for rid, kind, count in target.dedicated_capacities).get(
        (resource.identity, link.name if link else ""), 0
    )
    if capacity < required_edges:
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' dedicated connection requires capacity "
            f"{required_edges}, target provides {capacity}"
        )

    multiplier_a = resource.limit("multiplier_a")
    multiplier_b = resource.limit("multiplier_b")
    product_limit = resource.limit("product")
    accumulator_limit = resource.limit("accumulator")
    evidence: list[str] = []
    nodes: list[target_ir.ResourceInstance] = []
    previous = "constant:zero"
    for index, term in enumerate(reduction.terms):
        product = term.product_expression
        left, right = product.left, product.right
        left_width = catalog._dsp_signed_width(left.type)
        right_width = catalog._dsp_signed_width(right.type)
        if left_width <= multiplier_a and right_width <= multiplier_b:
            a_value, b_value = left, right
        elif right_width <= multiplier_a and left_width <= multiplier_b:
            a_value, b_value = right, left
        else:
            raise catalog.TargetArchitectureError(
                f"architecture '{template.identity}' product {index} operands "
                f"{left.type.width}x{right.type.width} exceed multiplier ports "
                f"{multiplier_a}x{multiplier_b}"
            )
        product_width = catalog._dsp_signed_width(product.type)
        if product_width > product_limit:
            raise catalog.TargetArchitectureError(
                f"architecture '{template.identity}' product {index} width exceeded: "
                f"physical signed width {product_width}, resource supports {product_limit}"
            )
        stage_type = (
            product.type if index == 0 else reduction.joins[index - 1].result_type
        )
        stage_width = catalog._dsp_signed_width(stage_type)
        if stage_width > accumulator_limit:
            raise catalog.TargetArchitectureError(
                f"architecture '{template.identity}' accumulator width exceeded at stage {index}: "
                f"physical signed width {stage_width}, resource supports {accumulator_limit}"
            )
        mode = (
            "accumulator_minus_product"
            if term.sign is signed_reductions.ProductTermSign.SUBTRACT
            else "accumulator_plus_product"
        )
        stage_semantic_identity = (
            term.semantic_identity if index == 0
            else reduction.joins[index - 1].semantic_identity
        )
        stage_expression = (
            product if index == 0 else reduction.joins[index - 1].original_expression
        )
        mappings = (
            target_ir.SemanticPortMapping("a", mapping_identity._semantic_mapping_identity(a_value), a_value),
            target_ir.SemanticPortMapping("b", mapping_identity._semantic_mapping_identity(b_value), b_value),
            target_ir.SemanticPortMapping("pcin", previous),
            target_ir.SemanticPortMapping("p", stage_semantic_identity, stage_expression),
            target_ir.SemanticPortMapping("pcout", stage_semantic_identity, stage_expression),
        )
        node_configuration = tuple((
            *configuration.physical_settings,
            ("pipeline_configuration", configuration.name),
            ("accumulator_mode", mode),
            ("term_ordinal", index),
        ))
        nodes.append(target_ir.ResourceInstance(
            f"product_accumulator{index}", resource.identity, resource.operation,
            node_configuration, mappings,
        ))
        previous = stage_semantic_identity
        evidence.append(
            f"stage {index}: {mode}, product {product_width}<={product_limit}, "
            f"accumulator {stage_width}<={accumulator_limit}"
        )

    edges = tuple(target_ir.DedicatedPhysicalEdge(
        f"signed_product_cascade:{index}:{index + 1}", link.name,
        f"product_accumulator{index}", link.source_port,
        f"product_accumulator{index + 1}", link.destination_port,
        link.width, link.placement_relation, link.latency, link.fabric_fallback,
    ) for index in range(required_edges))
    useful_latency = 1 + configuration.latency
    graph = target_ir.ImplementationGraph(
        semantic_region_identity=reduction.semantic_identity,
        architecture_template_identity=template.identity,
        target_identity=target.identity, target_hash=target.source_hash,
        resource_definition_hashes=((resource.identity, resource.source_hash),),
        resources=tuple(nodes), dedicated_edges=edges,
        latency=useful_latency, initiation_interval=configuration.initiation_interval,
        realization_backend="direct_systemverilog",
        latency_knowledge=TimingKnowledge.KNOWN.value,
        quantization=boundary, source_origin=reduction.source_origin,
        legality_evidence=(*evidence, boundary_evidence),
        architecture_template_hash=template.source_hash,
        target_family_identity=family.identity,
        target_dependency_hashes=target.dependency_hashes,
        architecture_dependency_hashes=template.dependency_hashes,
        selection_policy="auto", target_part=target.part,
        pipeline_configuration_identity=f"{resource.identity}.{configuration.name}",
        active_pipeline_sites=configuration.sites,
        physical_binding_identities=tuple(
            f"{resource.identity}:{item.backend}:{item.emitter}"
            for item in resource.physical_bindings
        ),
    )
    from zlang.target_timing import build_dsp_cascade_timing_dag
    timing_dag = build_dsp_cascade_timing_dag(
        graph, resource, configuration, structural_latency=1,
        output_width=boundary.type.width, exact_latency=exact_latency,
    )
    graph = replace(graph, latency=timing_dag.output_latency, timing_dag=timing_dag)
    return _attach_scheduled_value_graph(
        module,
        graph,
        source_expression if source_expression is not None else boundary,
    )


def map_auto_multiply_configuration(
    module: Module,
    target: target_ir.TargetInstance,
    family: target_ir.TargetFamilyDefinition,
    resources: tuple[target_ir.ResourceDefinition, ...],
    template: target_ir.ArchitectureTemplate,
    configuration: target_ir.PipelineConfiguration,
    *,
    exact_latency: int | None = None,
    source_expression: expr.Expression | None = None,
) -> target_ir.ImplementationGraph:
    """Cover one exact full-width scalar product with one catalog DSP."""

    if template.operation != "multiply":
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' is not multiply"
        )
    value, boundary, boundary_evidence = catalog._dsp_value_expression(
        source_expression
    )
    if not (
        isinstance(value, expr.Binary)
        and value.operator is expr.BinaryOperator.MULTIPLY
        and isinstance(value.left.type, catalog._DSP_NUMERIC_TYPES)
        and type(value.left.type) is type(value.right.type) is type(value.type)
        and value.type.width == value.left.type.width + value.right.type.width
    ):
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' requires one exact full-width "
            "uniform integer or fixed-point multiply"
        )
    resource = mapping_identity.require_named_resource(resources, template)
    catalog.validate_inventory(target, ((resource.identity, 1),))
    configuration = catalog.validate_pipeline_configuration(resource, configuration.name)
    left, right = value.left, value.right
    a_limit = resource.limit("multiplier_a")
    b_limit = resource.limit("multiplier_b")
    if (
        catalog._dsp_signed_width(left.type) <= a_limit
        and catalog._dsp_signed_width(right.type) <= b_limit
    ):
        a_value, b_value = left, right
    elif (
        catalog._dsp_signed_width(right.type) <= a_limit
        and catalog._dsp_signed_width(left.type) <= b_limit
    ):
        a_value, b_value = right, left
    else:
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' product operands "
            f"{left.type.width}x{right.type.width} exceed multiplier ports "
            f"{a_limit}x{b_limit}"
        )
    product_width = catalog._dsp_signed_width(value.type)
    if product_width > resource.limit("product"):
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' physical product width "
            f"{product_width} exceeds {resource.limit('product')}"
        )
    semantic = mapping_identity._expression_identity(value)
    node = target_ir.ResourceInstance(
        "multiply0",
        resource.identity,
        resource.operation,
        tuple((
            *configuration.physical_settings,
            ("pipeline_configuration", configuration.name),
            ("accumulator_mode", "accumulator_plus_product"),
            ("preadd_physical_width", catalog._dsp_signed_width(a_value.type)),
            ("term_ordinal", 0),
        )),
        (
            target_ir.SemanticPortMapping(
                "a", mapping_identity._semantic_mapping_identity(a_value), a_value
            ),
            target_ir.SemanticPortMapping(
                "b", mapping_identity._semantic_mapping_identity(b_value), b_value
            ),
            target_ir.SemanticPortMapping("p", semantic, value),
            target_ir.SemanticPortMapping("pcout", semantic, value),
        ),
    )
    graph = target_ir.ImplementationGraph(
        semantic_region_identity=semantic,
        architecture_template_identity=template.identity,
        target_identity=target.identity,
        target_hash=target.source_hash,
        resource_definition_hashes=((resource.identity, resource.source_hash),),
        resources=(node,),
        dedicated_edges=(),
        latency=1 + configuration.latency,
        initiation_interval=configuration.initiation_interval,
        realization_backend="direct_systemverilog",
        latency_knowledge=TimingKnowledge.KNOWN.value,
        quantization=boundary,
        source_origin=value.origin,
        legality_evidence=(
            f"exact product {product_width}<={resource.limit('product')}",
            boundary_evidence,
        ),
        architecture_template_hash=template.source_hash,
        target_family_identity=family.identity,
        target_dependency_hashes=target.dependency_hashes,
        architecture_dependency_hashes=template.dependency_hashes,
        selection_policy="auto",
        target_part=target.part,
        pipeline_configuration_identity=f"{resource.identity}.{configuration.name}",
        active_pipeline_sites=configuration.sites,
        physical_binding_identities=tuple(
            f"{resource.identity}:{item.backend}:{item.emitter}"
            for item in resource.physical_bindings
        ),
    )
    from zlang.target_timing import build_dsp_cascade_timing_dag

    timing = build_dsp_cascade_timing_dag(
        graph,
        resource,
        configuration,
        structural_latency=1,
        output_width=boundary.type.width,
        exact_latency=exact_latency,
    )
    graph = replace(graph, latency=timing.output_latency, timing_dag=timing)
    return _attach_scheduled_value_graph(module, graph, source_expression)


def map_auto_multiply_add_configuration(
    module: Module,
    target: target_ir.TargetInstance,
    family: target_ir.TargetFamilyDefinition,
    resources: tuple[target_ir.ResourceDefinition, ...],
    template: target_ir.ArchitectureTemplate,
    configuration: target_ir.PipelineConfiguration,
    *,
    exact_latency: int | None = None,
    source_expression: expr.Expression | None = None,
) -> target_ir.ImplementationGraph:
    """Cover one exact MAC or preadd-product with one DSP resource."""

    if template.operation != "multiply_add":
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' is not multiply_add"
        )
    value, boundary, boundary_evidence = catalog._dsp_value_expression(source_expression)
    product: expr.Binary | None = None
    addend: expr.Expression | None = None
    preadd: expr.Add | None = None
    multiplier: expr.Expression | None = None
    mode = "accumulator_plus_product"
    if (
        isinstance(value, expr.Binary)
        and value.operator is expr.BinaryOperator.MULTIPLY
    ):
        product = value
        for possible_preadd, possible_multiplier in (
            (value.left, value.right),
            (value.right, value.left),
        ):
            if isinstance(possible_preadd, expr.Add):
                preadd = possible_preadd
                multiplier = possible_multiplier
                break
    elif isinstance(value, expr.Add):
        for possible_product, possible_addend in (
            (value.left, value.right), (value.right, value.left)
        ):
            if (
                isinstance(possible_product, expr.Binary)
                and possible_product.operator is expr.BinaryOperator.MULTIPLY
            ):
                product = possible_product
                addend = possible_addend
                break
    elif (
        isinstance(value, expr.Binary)
        and value.operator is expr.BinaryOperator.SUBTRACT
    ):
        if (
            isinstance(value.right, expr.Binary)
            and value.right.operator is expr.BinaryOperator.MULTIPLY
        ):
            # DSP48E1 ALUMODE=0011 implements Z - X exactly.
            product = value.right
            addend = value.left
            mode = "accumulator_minus_product"
        elif (
            isinstance(value.left, expr.Binary)
            and value.left.operator is expr.BinaryOperator.MULTIPLY
        ):
            # With X=M, Z=PCIN, ALUMODE=0001 and direct CARRYIN=1,
            # -Z + X + CIN - 1 is exactly X - Z (UG479 table 2-10).
            product = value.left
            addend = value.right
            mode = "product_minus_accumulator"
    if product is None:
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' requires multiply, addend +/- product, "
            "or preadd * value"
        )
    if (
        isinstance(addend, expr.Binary)
        and addend.operator is expr.BinaryOperator.MULTIPLY
    ):
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' leaves a second product in fabric; "
            "use the signed-product cascade cover instead"
        )
    if addend is not None:
        typed_values = (product.left, product.right, product, addend, value)
    elif preadd is not None:
        typed_values = (
            preadd.left, preadd.right, preadd, multiplier, product, value,
        )
    else:
        typed_values = (product.left, product.right, product, value)
    if not all(
        isinstance(item.type, catalog._DSP_NUMERIC_TYPES)
        for item in typed_values
    ):
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' requires exact integer/fixed operands"
        )
    if addend is not None:
        if type(product.type) is not type(addend.type) or type(value.type) is not type(
            product.type
        ):
            raise catalog.TargetArchitectureError(
                f"architecture '{template.identity}' requires explicit mixed-signedness conversion"
            )
        product_fraction = getattr(product.type, "fraction", 0)
        addend_fraction = getattr(addend.type, "fraction", 0)
        if product_fraction != addend_fraction:
            raise catalog.TargetArchitectureError(
                f"architecture '{template.identity}' requires product/addend scale alignment"
            )
    resource = mapping_identity.require_named_resource(resources, template)
    supported_modes = set(dict(resource.capabilities).get("accumulator_modes", "").split("."))
    product_minus_is_bound = mode == "product_minus_accumulator" and any(
        item.backend == "systemverilog"
        and item.emitter == "dsp48e1_explicit"
        and item.primitive == "DSP48E1"
        for item in resource.physical_bindings
    )
    if mode not in supported_modes and not product_minus_is_bound:
        raise catalog.TargetArchitectureError(
            f"resource '{resource.identity}' does not support accumulator mode '{mode}'"
        )
    catalog.validate_inventory(target, ((resource.identity, 1),))
    configuration = catalog.validate_pipeline_configuration(resource, configuration.name)
    a_limit = resource.limit("multiplier_a")
    b_limit = resource.limit("multiplier_b")
    d_value: expr.Expression | None = None
    if preadd is not None:
        assert multiplier is not None
        a_value, d_value, b_value = preadd.left, preadd.right, multiplier
        if (
            catalog._dsp_signed_width(a_value.type) > a_limit
            or catalog._dsp_signed_width(d_value.type) > a_limit
            or catalog._dsp_signed_width(preadd.type) > a_limit
            or catalog._dsp_signed_width(b_value.type) > b_limit
        ):
            raise catalog.TargetArchitectureError(
                f"architecture '{template.identity}' preadd/multiply widths exceed "
                f"{a_limit}-bit preadd and {b_limit}-bit multiplier input"
            )
    else:
        left, right = product.left, product.right
        left_width = catalog._dsp_signed_width(left.type)
        right_width = catalog._dsp_signed_width(right.type)
        if left_width <= a_limit and right_width <= b_limit:
            a_value, b_value = left, right
        elif right_width <= a_limit and left_width <= b_limit:
            a_value, b_value = right, left
        else:
            raise catalog.TargetArchitectureError(
                f"architecture '{template.identity}' product operands "
                f"{left.type.width}x{right.type.width} exceed multiplier ports "
                f"{a_limit}x{b_limit}"
            )
    product_width = catalog._dsp_signed_width(product.type)
    if product_width > resource.limit("product"):
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' physical product width {product_width} "
            f"exceeds {resource.limit('product')}"
        )
    addend_width = catalog._dsp_signed_width(addend.type) if addend is not None else 1
    value_width = catalog._dsp_signed_width(value.type)
    if (
        addend_width > resource.limit("accumulator")
        or value_width > resource.limit("accumulator")
    ):
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' accumulator width exceeds "
            f"{resource.limit('accumulator')}"
        )
    semantic = mapping_identity._expression_identity(value)
    node = target_ir.ResourceInstance(
        "multiply_add0",
        resource.identity,
        resource.operation,
        tuple((
            *configuration.physical_settings,
            ("pipeline_configuration", configuration.name),
            ("accumulator_mode", mode),
            ("preadd_physical_width", (
                catalog._dsp_signed_width(preadd.type)
                if preadd is not None
                else catalog._dsp_signed_width(a_value.type)
            )),
            ("term_ordinal", 0),
        )),
        tuple((
            target_ir.SemanticPortMapping("a", mapping_identity._semantic_mapping_identity(a_value), a_value),
            *(
                (target_ir.SemanticPortMapping(
                    "d", mapping_identity._semantic_mapping_identity(d_value), d_value
                ),)
                if d_value is not None
                else ()
            ),
            target_ir.SemanticPortMapping("b", mapping_identity._semantic_mapping_identity(b_value), b_value),
            *(
                (target_ir.SemanticPortMapping(
                    "pcin", mapping_identity._semantic_mapping_identity(addend), addend
                ),)
                if addend is not None
                else ()
            ),
            target_ir.SemanticPortMapping("p", semantic, value),
            target_ir.SemanticPortMapping("pcout", semantic, value),
        )),
    )
    graph = target_ir.ImplementationGraph(
        semantic_region_identity=semantic,
        architecture_template_identity=template.identity,
        target_identity=target.identity,
        target_hash=target.source_hash,
        resource_definition_hashes=((resource.identity, resource.source_hash),),
        resources=(node,),
        dedicated_edges=(),
        latency=1 + configuration.latency,
        initiation_interval=configuration.initiation_interval,
        realization_backend="direct_systemverilog",
        latency_knowledge=TimingKnowledge.KNOWN.value,
        quantization=boundary,
        source_origin=value.origin,
        legality_evidence=(
            f"{mode}; product {product_width}<={resource.limit('product')}",
            (
                f"preadd {catalog._dsp_signed_width(preadd.type)}<={a_limit}"
                if preadd is not None
                else f"addend {addend_width}<={resource.limit('accumulator')}"
            ),
            boundary_evidence,
        ),
        architecture_template_hash=template.source_hash,
        target_family_identity=family.identity,
        target_dependency_hashes=target.dependency_hashes,
        architecture_dependency_hashes=template.dependency_hashes,
        selection_policy="auto",
        target_part=target.part,
        pipeline_configuration_identity=f"{resource.identity}.{configuration.name}",
        active_pipeline_sites=configuration.sites,
        physical_binding_identities=tuple(
            f"{resource.identity}:{item.backend}:{item.emitter}"
            for item in resource.physical_bindings
        ),
    )
    from zlang.target_timing import build_dsp_cascade_timing_dag

    timing = build_dsp_cascade_timing_dag(
        graph,
        resource,
        configuration,
        structural_latency=1,
        output_width=boundary.type.width,
        exact_latency=exact_latency,
    )
    graph = replace(graph, latency=timing.output_latency, timing_dag=timing)
    return _attach_scheduled_value_graph(module, graph, source_expression)


def _attach_scheduled_value_graph(
    module: Module,
    graph: target_ir.ImplementationGraph,
    source_expression: expr.Expression,
) -> target_ir.ImplementationGraph:
    """Bind a target cover to the same exact-N scheduled operation graph.

    Exact source pipelines and positive-latency ``implement`` candidates both
    own scheduler graphs.  A target cover must join the graph for its exact
    latency; borrowing another candidate's stage assignment would make the
    physical identity and report untruthful.
    """

    from zlang.pipeline_scheduling import erase_pipeline_timing

    plans = tuple(
        candidate.pipeline_plan
        for exploration in module.pipeline_explorations
        if exploration.source_expression == source_expression
        for candidate in exploration.candidates
        if candidate.latency == graph.latency
        and candidate.pipeline_plan is not None
        and candidate.pipeline_plan.scheduled_value_graph is not None
    )
    if not plans:
        plans = tuple(
            assignment.expression.pipeline_plan
            for assignment in (*module.assignments, *module.next_assignments)
            if isinstance(assignment.expression, expr.Pipeline)
            and assignment.expression.pipeline_plan is not None
            and erase_pipeline_timing(assignment.expression) == source_expression
        )
    if not plans:
        return graph
    if len(plans) != 1:
        raise catalog.TargetArchitectureError(
            "physical resource cover matches more than one scheduled value graph"
        )
    scheduled = plans[0].scheduled_value_graph
    if scheduled is None:
        return graph
    if scheduled.clock_domain is None and len(module.clock_domains) == 1:
        # Implementation-intent regions predate explicit state ownership and
        # therefore do not carry a source-level Pipeline node from which to
        # copy the domain.  Their output boundary still resolves the unique
        # physical domain.  Attach it before resource covering so physical,
        # evidence and cache identities cannot be reused after moving the
        # region to another clock.
        scheduled = replace(
            scheduled, clock_domain=module.clock_domains[0].clock
        )
    # A natural resource configuration which exceeds an exact source latency
    # is retained only so the target report can explain its rejection.  It is
    # not a complete/publishable schedule and must not borrow the source
    # contract's shorter graph.
    if scheduled.exact_latency != graph.latency:
        return graph
    by_semantic = {
        semantic: operation
        for operation, semantic in scheduled.operation_semantic_identities
    }
    bindings: list[scheduled_ir.ScheduledValueResourceBinding] = []
    bound_operations: set[str] = set()
    for resource in graph.resources:
        outputs = tuple(
            item
            for item in resource.semantic_mappings
            if item.resource_port == "p" and item.expression is not None
        )
        if len(outputs) != 1:
            raise catalog.TargetArchitectureError(
                f"resource '{resource.identity}' has no unique typed result mapping"
            )
        covered_semantics = _dsp_resource_covered_semantics(resource, outputs[0].expression)
        covered_operations = tuple(
            by_semantic[semantic]
            for semantic in covered_semantics
            if semantic in by_semantic
        )
        if not covered_operations:
            raise catalog.TargetArchitectureError(
                f"resource '{resource.identity}' result does not cover a scheduled operation"
            )
        for operation in covered_operations:
            if operation in bound_operations:
                raise catalog.TargetArchitectureError(
                    f"scheduled operation '{operation}' is covered by more than one resource"
                )
            bound_operations.add(operation)
            bindings.append(scheduled_ir.ScheduledValueResourceBinding(
                operation,
                resource.identity,
                resource.resource_definition_identity,
                graph.pipeline_configuration_identity,
            ))
    timing = graph.timing_dag
    physical = scheduled_ir.ScheduledValueGraph(
        source_expression_identity=scheduled.source_expression_identity,
        selected_value_identity=scheduled.selected_value_identity,
        operation_identities=scheduled.operation_identities,
        operation_semantic_identities=scheduled.operation_semantic_identities,
        dependencies=scheduled.dependencies,
        stage_assignment=scheduled.stage_assignment,
        stage_delays_ps=scheduled.stage_delays_ps,
        exact_latency=graph.latency,
        initiation_interval=graph.initiation_interval,
        # Resource pipeline sites currently publish no non-zero segment timing
        # for this target.  Preserve the scheduler's honest structural label;
        # routed/synthesis evidence remains separate TargetCandidate metadata.
        cost_source=scheduled.cost_source,
        resource_bindings=tuple(bindings),
        cut_identities=tuple(item.identity for item in timing.cuts) if timing else (),
        alignment_delay_identities=tuple(
            item.identity for item in timing.alignment_delays
        ) if timing else (),
        compensation_delay_identities=tuple(
            item.identity for item in timing.compensation_delays
        ) if timing else (),
        rewrite_certificate=scheduled.rewrite_certificate,
        clock_domain=scheduled.clock_domain,
    )
    return replace(graph, scheduled_value_graph=physical)


def _dsp_resource_covered_semantics(
    resource: target_ir.ResourceInstance,
    output: expr.Expression,
) -> tuple[str, ...]:
    """Return exact typed DAG operations spatially implemented by one DSP.

    Resource mapping already owns arithmetic legality.  This helper only joins
    that mapping to the shared scheduled graph: the published P expression,
    its mapped multiply, and (when present) the mapped preadder.  It never
    claims unrelated ancestors from an accumulator cascade.
    """

    mappings = {
        item.resource_port: item.expression
        for item in resource.semantic_mappings
        if item.expression is not None
    }
    a_value = mappings.get("a")
    b_value = mappings.get("b")
    d_value = mappings.get("d")
    if a_value is None or b_value is None:
        return (signed_reductions.expression_semantic_identity(output),)
    a_identity = signed_reductions.expression_semantic_identity(a_value)
    b_identity = signed_reductions.expression_semantic_identity(b_value)
    d_identity = (
        None if d_value is None else signed_reductions.expression_semantic_identity(d_value)
    )
    result = [signed_reductions.expression_semantic_identity(output)]
    for value in walk_expression(output):
        if isinstance(value, expr.Binary) and value.operator is expr.BinaryOperator.MULTIPLY:
            left_identity = signed_reductions.expression_semantic_identity(value.left)
            right_identity = signed_reductions.expression_semantic_identity(value.right)
            direct = {left_identity, right_identity} == {a_identity, b_identity}
            preadd_value = None
            multiplier_value = None
            if isinstance(value.left, expr.Add):
                preadd_value, multiplier_value = value.left, value.right
            elif isinstance(value.right, expr.Add):
                preadd_value, multiplier_value = value.right, value.left
            preadd_match = False
            if preadd_value is not None and d_identity is not None:
                preadd_operands = {
                    signed_reductions.expression_semantic_identity(preadd_value.left),
                    signed_reductions.expression_semantic_identity(preadd_value.right),
                }
                preadd_match = (
                    preadd_operands == {a_identity, d_identity}
                    and signed_reductions.expression_semantic_identity(multiplier_value) == b_identity
                )
            if direct or preadd_match:
                result.append(signed_reductions.expression_semantic_identity(value))
                if preadd_match:
                    result.append(signed_reductions.expression_semantic_identity(preadd_value))
    return tuple(dict.fromkeys(result))
