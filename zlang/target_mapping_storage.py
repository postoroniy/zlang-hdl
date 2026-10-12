"""Deterministic target architecture selection and mapping."""

from __future__ import annotations

from hashlib import sha256

from zlang.ir import target as target_ir
from zlang.ir.timing import TimingKnowledge
from zlang.async_fifo import build_async_fifo_physical_plan

from zlang import target_catalog as catalog
from zlang.target_mapping_identity import _semantic_payload, require_named_resource


def _map_async_fifo_memory(module, target, family, resources, template, policy):
    """Bind only the compiler-owned 1W1R FIFO storage, never a public async_mem.

    Full tracks the *consumed* Gray pointer, including a prefetched but stalled
    beat.  The writer therefore cannot reach the address of a live output
    beat.  A stale synchronized read pointer only reduces available capacity.
    This is a digital structural certificate, not an MTBF or silicon proof.
    """

    crossings = tuple(
        item for item in module.connections
        if item.crossing is not None and item.crossing.kind.value == "async_fifo"
    )
    if len(crossings) != 1 or len(module.connections) != 1 or module.memories:
        raise catalog.TargetArchitectureError(
            "native FIFO-memory binding requires one isolated explicit async_fifo "
            "crossing and no separate semantic memory"
        )
    try:
        plan = build_async_fifo_physical_plan(module, crossings[0])
    except ValueError as error:
        raise catalog.TargetArchitectureError(str(error)) from error
    if template.resource_count != 1 or template.initiation_interval != 1:
        raise catalog.TargetArchitectureError(
            "native FIFO-memory binding requires one spatial RAM and II=1"
        )
    resource = require_named_resource(resources, template)
    catalog.validate_inventory(target, ((resource.identity, 1),))
    capabilities = dict(resource.capabilities)
    if (
        family.name != "Xilinx7Series"
        or resource.name not in {"RAMB18E1", "RAMB36E1"}
        or capabilities.get("synchronous_read") != "true"
        or capabilities.get("output_register") != "optional"
        or not any(
            item.backend == "systemverilog"
            and item.emitter == "xilinx_bram_inference"
            and item.primitive == resource.name
            for item in resource.physical_bindings
        )
    ):
        raise catalog.TargetArchitectureError(
            f"resource '{resource.identity}' is not the bounded AMD 7-Series "
            "RAMB18E1/RAMB36E1 FIFO-memory route"
        )
    catalog.validate_memory_configuration(
        resource, width=plan.memory.element_type.width,
        depth=plan.depth, port_mode="simple_dual",
    )
    configuration = catalog.validate_pipeline_configuration(
        resource, template.pipeline_configuration or "core_registered",
    )
    if configuration.name != "core_registered" or configuration.latency != 0:
        raise catalog.TargetArchitectureError(
            "the first native FIFO-memory route requires DO_REG=0 and one "
            "destination-clock read cycle"
        )
    latency = plan.memory.read_latency + configuration.latency
    if template.latency != latency:
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' latency {template.latency} "
            f"does not match FIFO memory latency {latency}"
        )
    node = target_ir.ResourceInstance(
        "fifo_memory0", resource.identity, resource.operation,
        (
            *configuration.physical_settings,
            ("pipeline_configuration", configuration.name),
            ("dorega", 0),
            ("depth", plan.depth),
            ("width", plan.memory.element_type.width),
            ("fifo_prefetch_policy", plan.prefetch_policy),
            ("fifo_memory", 1),
        ),
        tuple(
            mapping
            for port in plan.memory.ports
            for mapping in (
                target_ir.SemanticPortMapping(
                    f"{port.name}.address",
                    f"memory:{plan.memory.semantic_id}:port:{port.name}:address",
                    port.address,
                ),
                target_ir.SemanticPortMapping(
                    f"{port.name}.enable",
                    f"memory:{plan.memory.semantic_id}:port:{port.name}:enable",
                    port.read_enable or port.write_enable,
                ),
                target_ir.SemanticPortMapping(
                    f"{port.name}.data",
                    f"memory:{plan.memory.semantic_id}:port:{port.name}:data",
                    port.write_data,
                ),
            )
        ),
    )
    return target_ir.ImplementationGraph(
        semantic_region_identity=plan.identity,
        architecture_template_identity=template.identity,
        target_identity=target.identity, target_hash=target.source_hash,
        resource_definition_hashes=((resource.identity, resource.source_hash),),
        resources=(node,), dedicated_edges=(), latency=latency,
        initiation_interval=1,
        realization_backend="direct_systemverilog",
        latency_knowledge=TimingKnowledge.KNOWN.value,
        legality_evidence=(
            "typed 1W1R async_mem; write/read clocks independent; read_latency=1",
            "DO_REG=0; one destination-domain registered read boundary",
            "consumed-pointer full guard prevents enabled same-address "
            "cross-clock collision, including prefetched/stalled beat",
            "Gray synchronizers are two-stage; digital structural model only",
        ),
        architecture_template_hash=template.source_hash,
        target_family_identity=family.identity,
        target_dependency_hashes=target.dependency_hashes,
        architecture_dependency_hashes=template.dependency_hashes,
        selection_policy=catalog.ArchitectureSelectionMode(policy).value,
        target_part=target.part,
        pipeline_configuration_identity=f"{resource.identity}.{configuration.name}",
        active_pipeline_sites=configuration.sites,
        physical_binding_identities=tuple(
            f"{resource.identity}:{item.backend}:{item.emitter}"
            for item in resource.physical_bindings
        ),
    )


def _map_synchronous_memory(module, target, family, resources, template, policy):
    if len(module.memories) != 1 or template.resource_count != 1:
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' requires exactly one semantic memory and one resource"
        )
    resource = require_named_resource(resources, template)
    catalog.validate_inventory(target, ((resource.identity, 1),))
    memory = module.memories[0]
    named_true_dual = (
        memory.ported
        and not memory.async_memory
        and len(memory.ports) == 2
        and all(port.kind.value == "read_write" for port in memory.ports)
    )
    if memory.ported and not named_true_dual:
        raise catalog.TargetArchitectureError(
            "selected synchronous-memory architecture supports legacy simple-dual "
            "or exact same-clock two-read/write port shape only"
        )
    if memory.scheduled:
        raise catalog.TargetArchitectureError(
            "target memory mapping does not support rule-owned scheduled memory"
        )
    if memory.read_latency != 1:
        raise catalog.TargetArchitectureError(
            "target memory mapping supports only one-cycle synchronous reads"
        )
    if named_true_dual:
        if memory.contents_reset.value != "preserve":
            raise catalog.TargetArchitectureError(
                "native true-dual block-memory mapping requires "
                "reset-preserved contents; clearing every cell would prevent "
                "exact block-memory inference"
            )
    elif (
        memory.contents_reset.value != "clear"
        or memory.read_data_reset.value != "clear"
    ):
        raise catalog.TargetArchitectureError(
            "target memory mapping does not advertise reset-preserved contents "
            "or read data"
        )
    if memory.write_mask_width is not None or any(
        port.write_mask is not None for port in memory.ports
    ):
        raise catalog.TargetArchitectureError(
            "target memory mapping does not support byte write masks"
        )
    port_mode = "true_dual" if named_true_dual else "simple_dual"
    catalog.validate_memory_configuration(
        resource, width=memory.element_type.width, depth=memory.depth,
        port_mode=port_mode,
    )
    if named_true_dual:
        capabilities = dict(resource.capabilities)
        if memory.initial_value is not None and not capabilities.get(
            "initialization", ""
        ):
            raise catalog.TargetArchitectureError(
                f"memory resource '{resource.identity}' does not advertise "
                "initial-content support"
            )
        collision_modes = set(
            capabilities.get(
                "same_clock_collision",
                capabilities.get("read_during_write", ""),
            ).split(".")
        )
        if memory.collision.value not in collision_modes:
            raise catalog.TargetArchitectureError(
                f"memory resource '{resource.identity}' does not support "
                f"same-clock collision mode '{memory.collision.value}'"
            )
    configuration = catalog.validate_pipeline_configuration(
        resource, template.pipeline_configuration or "core_registered",
    )
    total_latency = memory.read_latency + configuration.latency
    if template.latency != total_latency:
        raise catalog.TargetArchitectureError(
            f"architecture '{template.identity}' latency {template.latency} does not match "
            f"memory/configuration latency {total_latency}"
        )
    semantic_mappings = (
        tuple(
            mapping
            for port in memory.ports
            for mapping in (
                target_ir.SemanticPortMapping(
                    f"{port.name}.address",
                    f"memory:{memory.name}:port:{port.name}:address",
                    port.address,
                ),
                target_ir.SemanticPortMapping(
                    f"{port.name}.read_enable",
                    f"memory:{memory.name}:port:{port.name}:read_enable",
                    port.read_enable,
                ),
                target_ir.SemanticPortMapping(
                    f"{port.name}.write_enable",
                    f"memory:{memory.name}:port:{port.name}:write_enable",
                    port.write_enable,
                ),
                target_ir.SemanticPortMapping(
                    f"{port.name}.write_data",
                    f"memory:{memory.name}:port:{port.name}:write_data",
                    port.write_data,
                ),
                target_ir.SemanticPortMapping(
                    f"{port.name}.read_data",
                    f"memory:{memory.name}:port:{port.name}:read_data",
                ),
            )
        )
        if named_true_dual else (
            target_ir.SemanticPortMapping("read_address", f"memory:{memory.name}:read_address", memory.read_address),
            target_ir.SemanticPortMapping("write_enable", f"memory:{memory.name}:write_enable", memory.write_enable),
            target_ir.SemanticPortMapping("write_address", f"memory:{memory.name}:write_address", memory.write_address),
            target_ir.SemanticPortMapping("write_data", f"memory:{memory.name}:write_data", memory.write_data),
            target_ir.SemanticPortMapping("read_data", f"memory:{memory.name}:read_data"),
        )
    )
    node = target_ir.ResourceInstance(
        "memory0", resource.identity, resource.operation,
        tuple((*configuration.physical_settings,
               ("pipeline_configuration", configuration.name),
               ("depth", memory.depth), ("width", memory.element_type.width))),
        semantic_mappings,
    )
    return target_ir.ImplementationGraph(
        semantic_region_identity=sha256(_semantic_payload(memory).encode()).hexdigest(),
        architecture_template_identity=template.identity,
        target_identity=target.identity, target_hash=target.source_hash,
        resource_definition_hashes=((resource.identity, resource.source_hash),),
        resources=(node,), dedicated_edges=(), latency=total_latency,
        initiation_interval=template.initiation_interval,
        realization_backend="direct_systemverilog",
        latency_knowledge=TimingKnowledge.KNOWN.value,
        legality_evidence=(
            f"{port_mode} synchronous memory {memory.depth}x{memory.element_type.width}",
            f"pipeline configuration {configuration.name}",
        ),
        architecture_template_hash=template.source_hash,
        target_family_identity=family.identity,
        target_dependency_hashes=target.dependency_hashes,
        architecture_dependency_hashes=template.dependency_hashes,
        selection_policy=catalog.ArchitectureSelectionMode(policy).value,
        target_part=target.part,
        pipeline_configuration_identity=f"{resource.identity}.{configuration.name}",
        active_pipeline_sites=configuration.sites,
        physical_binding_identities=tuple(
            f"{resource.identity}:{item.backend}:{item.emitter}"
            for item in resource.physical_bindings
        ),
    )
