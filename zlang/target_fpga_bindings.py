"""Qualified DSP-plus-fabric binding facts and graph accounting.

One node reserves a whole hard macro/slice; its single multiplier is emitted
explicitly. Other arithmetic and alignment remain visible fabric operations.
This is not an allocator or a second candidate provider.
"""

from __future__ import annotations

from dataclasses import replace

from zlang.common import stable_digest
from zlang.costs import CandidateCost
from zlang.ir import expressions as expr
from zlang.ir.target import ImplementationDelay, TimingEdge, TimingNode
from zlang.target_catalog import TargetArchitectureError


DSP_BINDINGS = {
    "gowin_dsp_fabric": "MULT18X18",
    "ecp5_dsp_fabric": "MULT18X18D",
    "ice40_dsp_fabric": "SB_MAC16",
    "nexus_dsp_fabric": "MULT18X18",
    "gowin_v_dsp_fabric": "MULT12X12",
}
BRAM_BINDINGS = frozenset({
    "gowin_bram_inference", "ecp5_bram_inference", "ice40_bram_inference",
    "nexus_bram_inference", "gowin_v_bram_inference",
})


def fpga_multiplier_layout(binding, configuration):
    """Exact primitive ABI and operand width of one selected scalar cell."""
    if binding not in DSP_BINDINGS:
        raise TargetArchitectureError("unknown FPGA DSP binding")
    width = {"ice40_dsp_fabric": 16, "gowin_v_dsp_fabric": 12}.get(binding, 18)
    mode = configuration.get("primitive_width", width)
    if mode != width and not (binding == "gowin_dsp_fabric" and mode == 9):
        raise TargetArchitectureError("unsupported FPGA multiplier width mode")
    family = {"gowin_dsp_fabric": "gowin", "ecp5_dsp_fabric": "ecp5",
              "ice40_dsp_fabric": "ice40", "nexus_dsp_fabric": "nexus",
              "gowin_v_dsp_fabric": "gowin_v"}[binding]
    if mode == 9:
        return "gowin9", 9, "MULT9X9"
    return family, width, DSP_BINDINGS[binding]



def fpga_dsp_binding(resource, operation: str | None = None) -> str | None:
    matches = tuple(
        b
        for b in resource.physical_bindings
        if b.backend == "systemverilog" and b.emitter in DSP_BINDINGS
    )
    if not matches:
        return None
    caps = dict(resource.capabilities)
    if (
        len(matches) != 1
        or matches[0].primitive != DSP_BINDINGS[matches[0].emitter]
        or caps.get("arithmetic_realization") != "dsp_plus_fabric"
        or caps.get("inventory_unit") != "whole_macro"
    ):
        raise TargetArchitectureError("invalid qualified FPGA DSP binding contract")
    width = fpga_multiplier_layout(matches[0].emitter, {})[1]
    for name, maximum in (
        ("multiplier_a", width),
        ("multiplier_b", width),
        ("preadder", width),
        ("product", 2 * width),
        ("accumulator", 54),
    ):
        if not 1 <= resource.limit(name) <= maximum:
            raise TargetArchitectureError(
                f"FPGA DSP {name} exceeds the qualified model"
            )
    if operation is not None and operation not in caps.get(
        "physical_architectures", ""
    ).split("."):
        raise TargetArchitectureError(f"FPGA DSP binding does not qualify {operation}")
    return matches[0].emitter


def fpga_bram_binding(resource) -> str | None:
    return next(
        (
            b.emitter
            for b in resource.physical_bindings
            if b.backend == "systemverilog" and b.emitter in BRAM_BINDINGS
        ),
        None,
    )


def finalize_fpga_graph(graph, resource):
    binding = fpga_dsp_binding(resource)
    if binding is None:
        return graph
    if graph.dedicated_edges:
        raise TargetArchitectureError("DSP + fabric cannot publish dedicated edges")
    value = graph.quantization
    if value is None:
        raise TargetArchitectureError("FPGA DSP graph has no typed result")
    arithmetic = value.expression if isinstance(value, expr.FixedConvert) else value
    configs = [dict(n.configuration) for n in graph.resources]
    native_cycles = set()
    for cfg in configs:
        name = cfg["pipeline_configuration"]
        definition = resource.pipeline_configuration(name)
        mreg, oreg = cfg.get("mreg", 0), cfg.get("product_oreg", 0)
        if (
            type(mreg) is not int
            or type(oreg) is not int
            or mreg not in (0, 1)
            or oreg not in (0, 1)
            or definition.latency != mreg + oreg
            or set(dict(definition.physical_settings)) - {"mreg", "product_oreg", "primitive_width"}
        ):
            raise TargetArchitectureError("unqualified FPGA DSP register configuration")
        family, _, _ = fpga_multiplier_layout(binding, cfg)
        if family not in {"gowin", "ecp5"} and mreg:
            raise TargetArchitectureError("unsupported FPGA multiplier pipeline register")
        native_cycles.add(mreg + oreg)
    if len(native_cycles) != 1:
        raise TargetArchitectureError(
            "FPGA products must have coherent pipeline stages"
        )
    cycles = native_cycles.pop()
    if graph.latency < cycles:
        raise TargetArchitectureError("FPGA DSP exceeds the complete latency contract")
    parallel = all(c.get("parallel_bilinear") == 1 for c in configs)
    fir = len(graph.resources) == 4 and all(
        any(
            m.resource_port == "d" and m.expression is not None
            for m in n.semantic_mappings
        )
        for n in graph.resources
    )
    nodes, delays = [], []
    for index, (node, cfg) in enumerate(zip(graph.resources, configs, strict=True)):
        operands = {
            m.resource_port: m.expression
            for m in node.semantic_mappings
            if m.expression is not None
        }
        _, physical_width, primitive = fpga_multiplier_layout(binding, cfg)
        if any(operands[p].type.width > physical_width for p in ("a", "b")):
            raise TargetArchitectureError("selected FPGA multiplier mode operand width exceeded")
        if binding == "gowin_v_dsp_fabric" and any(
            type(operands[p].type).__name__ in {"UIntType", "UFixedType"}
            and operands[p].type.width >= physical_width for p in ("a", "b")
        ):
            raise TargetArchitectureError("signed-only FPGA multiplier requires unsigned zero-extension")
        preadd = cfg.get("preadd_physical_width", 0) if "d" in operands else 0
        if "d" in operands and not preadd:
            preadd = max(operands["a"].type.width, operands["d"].type.width) + 1
        addend = operands.get("c", operands.get("pcin"))
        add_bits = (
            arithmetic.type.width
            if (addend is not None or index > 0) and not parallel
            else 0
        )
        if fir:
            add_bits = (
                arithmetic.type.width - 1
                if index == 1
                else 2 * arithmetic.type.width - 1
                if index == 3
                else 0
            )
        facts = (
            ("realization", "dsp_plus_fabric"),
            ("reserved_macro_units", 1),
            ("native_multiplier_cells", 1),
            (
                "native_asign",
                int(type(operands["a"].type).__name__ in {"SIntType", "FixedType"}),
            ),
            (
                "native_bsign",
                int(type(operands["b"].type).__name__ in {"SIntType", "FixedType"}),
            ),
            ("native_product_pipeline", cycles),
            ("fabric_preadder_bits", preadd),
            ("fabric_add_bits", add_bits),
            ("fabric_reduction_topology", "balanced_pairwise" if fir else "ordered"),
            ("fabric_addend_delay", cycles if addend is not None else 0),
            ("fabric_addend_width", addend.type.width if addend is not None else 0),
            ("fabric_output_delay", graph.latency - cycles if index == 0 else 0),
            ("fabric_output_width", value.type.width if index == 0 else 0),
        )
        if binding not in {"gowin_dsp_fabric", "ecp5_dsp_fabric"} or physical_width != 18:
            facts = (*facts, ("native_operand_width", physical_width), ("native_primitive", primitive))
        nodes.append(replace(node, configuration=(*node.configuration, *facts)))
        if addend is not None and cycles and graph.timing_dag is not None:
            source = next(
                n.identity for n in graph.timing_dag.nodes if n.kind == "input_boundary"
            )
            destination = next(
                n.identity
                for n in graph.timing_dag.nodes
                if n.implementation_node_identity == node.identity
            )
            delays.append(
                ImplementationDelay(
                    stable_digest(
                        (
                            "fpga-addend-alignment",
                            node.identity,
                            cycles,
                            addend.type.width,
                        )
                    ),
                    "alignment",
                    source,
                    destination,
                    cycles,
                    addend.type.width,
                    cycles * addend.type.width,
                    semantic_identity=graph.semantic_region_identity,
                    source_origin=addend.origin,
                )
            )
    timing = graph.timing_dag
    if timing is not None:
        ordered = tuple(
            next(
                n.identity
                for n in timing.nodes
                if n.implementation_node_identity == node.identity
            )
            for node in graph.resources
        )
        input_id = next(n.identity for n in timing.nodes if n.kind == "input_boundary")
        quant = next(n for n in timing.nodes if n.kind == "fixed_quantization")
        output_id = next(
            n.identity for n in timing.nodes if n.kind == "output_boundary"
        )
        extra, edges = [], []

        def edge(a, b):
            edges.append(
                TimingEdge(stable_digest(("fpga-fabric", a, b)), "fabric", a, b)
            )

        def join(label, sources):
            identity = stable_digest(
                (graph.semantic_region_identity, "fabric-add", label)
            )
            extra.append(
                TimingNode(
                    identity,
                    "fabric_add",
                    f"fabric_add_{label}",
                    graph.semantic_region_identity,
                    source_origin=graph.source_origin,
                )
            )
            for source in sources:
                edge(source, identity)
            return identity

        for n in ordered:
            edge(input_id, n)
        if fir:
            root = join("sum", (join("pair0", ordered[:2]), join("pair1", ordered[2:])))
        else:
            root = ordered[0]
            for index, n in enumerate(ordered[1:], 1):
                root = join(str(index), (root, n))
            if any(
                c.get("fabric_addend_width", 0)
                for c in map(lambda n: dict(n.configuration), nodes)
            ):
                root = join("addend", (root, input_id))
                delays = [replace(d, destination_node=root) for d in delays]
        edge(root, quant.identity)
        edge(quant.identity, output_id)
        timing = replace(
            timing,
            nodes=(*timing.nodes, *extra),
            edges=tuple(edges),
            alignment_delays=(*timing.alignment_delays, *delays),
        )
    return replace(
        graph,
        resources=tuple(nodes),
        timing_dag=timing,
        legality_evidence=(
            *graph.legality_evidence,
            f"one whole macro reserved per hard {fpga_multiplier_layout(binding, configs[0])[1]}x{fpga_multiplier_layout(binding, configs[0])[1]} multiplier",
            "pre-add/reduction/addend arithmetic is explicit fabric; no dedicated cascade",
        ),
    )


def fpga_fabric_cost(graph):
    configs = [dict(n.configuration) for n in graph.resources]
    if not configs or not all(
        c.get("realization") == "dsp_plus_fabric" for c in configs
    ):
        return None
    return CandidateCost.estimate(
        lut=sum(c["fabric_preadder_bits"] + c["fabric_add_bits"] for c in configs),
        ff=sum(
            c["fabric_addend_width"] * c["fabric_addend_delay"]
            + c["fabric_output_width"] * c["fabric_output_delay"]
            for c in configs
        ),
        dsp=sum(c["native_multiplier_cells"] for c in configs),
        bram=0,
        latency=graph.latency,
        ii=graph.initiation_interval,
        fmax_est=None,
        structural_cost=len(graph.resources),
    )
