"""Direct-SystemVerilog emission for selected source-described resource graphs."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

from zlang.backend.manifest import (
    IMPLEMENTATION_MANIFEST_VERSION,
    ImplementationEdgeManifest,
    ImplementationManifest,
    ImplementationResourceManifest,
    publish_artifact,
)
from zlang.backend.companions import collect_rom_companions
from zlang.backend.systemverilog.sequential import clock_event, reset_asserted
from zlang.backend.systemverilog.emitter import (
    SystemVerilogEmissionError,
    _expression,
    _emit_memory,
    _emit_public_leaf_boundary,
    _identifier,
    _physical_port_declarations,
    emit_artifact as emit_generic_artifact,
)
from zlang.ir import expressions as expr
from zlang.ir.module import Module, PortDirection, Register
from zlang.ir.target import ImplementationGraph, ResourceDefinition, ResourceInstance
from zlang.ir.types import FixedType
from zlang.targets import load_target


def emit_target(module: Module, graph: ImplementationGraph, *, simulation_model: bool = False) -> str:
    """Emit one already-selected graph; no semantic matching occurs here."""
    unsupported_domains = tuple(
        domain for domain in module.clock_domains if not domain.is_legacy_default
    )
    if unsupported_domains:
        raise SystemVerilogEmissionError(
            "selected DSP/BRAM resource emission supports only the legacy "
            "rising-edge synchronous active-high reset contract; non-default "
            "physical reset contracts require a target resource binding that "
            "advertises exact reset support",
            code="ZL-BACKEND-TARGET-PHYSICAL-RESET",
        )
    if (
        not graph.is_generic
        and graph.realization_backend != "direct_systemverilog"
    ):
        raise SystemVerilogEmissionError(
            "direct SystemVerilog cannot emit an implementation graph realized "
            f"by '{graph.realization_backend}'"
        )
    if graph.is_generic:
        raise SystemVerilogEmissionError("generic implementation graphs use the ordinary direct-SV emitter")
    if graph.target_identity is None:
        raise SystemVerilogEmissionError("selected resource graph has no target identity")
    target, _, definitions = load_target(graph.target_identity)
    resources = {item.identity: item for item in definitions}
    node_definitions: dict[str, ResourceDefinition] = {}
    emitters: set[str] = set()
    for node in graph.resources:
        definition = resources.get(node.resource_definition_identity)
        if definition is None:
            raise SystemVerilogEmissionError(
                f"backend cannot emit selected resource '{node.resource_definition_identity}' on target '{target.identity}'"
            )
        binding = dict(definition.backend_bindings).get("systemverilog")
        if binding not in {"dsp48e1_explicit", "xilinx_bram_inference"}:
            raise SystemVerilogEmissionError(
                f"backend cannot emit selected resource '{definition.identity}': "
                f"unsupported systemverilog binding '{binding or 'missing'}'",
                code="ZL-BACKEND-BINDING",
                fixes=("select a resource with a supported SystemVerilog binding",),
            )
        node_definitions[node.identity] = definition
        emitters.add(binding)
    if emitters == {"dsp48e1_explicit"}:
        # Signed-product graphs carry one explicit accumulator mode per
        # resource instance. This is a graph/configuration property, not a
        # source-name or RTL-text heuristic. Keep the older symmetric FIR
        # emitter unchanged and use the ordered-cascade emitter for FFT paths.
        if graph.resources and all(
            "accumulator_mode" in dict(item.configuration)
            for item in graph.resources
        ):
            packed = _emit_signed_product_dsp48e1_graph(
                module, graph, node_definitions, simulation_model
            )
        else:
            packed = _emit_dsp48e1_graph(
                module, graph, node_definitions, simulation_model
            )
    elif emitters == {"xilinx_bram_inference"} and len(graph.resources) == 1:
        packed = (
            "`default_nettype none\n"
            + _emit_memory(module, ram_style="block")
            + "`default_nettype wire\n"
        )
    else:
        raise SystemVerilogEmissionError(
            "selected resource graph mixes unsupported physical emitters"
        )
    # Physical-resource selection changes only the private implementation.
    # The selected public top obeys the same mandatory TopPhysicalABI leaf and
    # native-array boundary as the ordinary direct-SystemVerilog emitter.
    return _emit_public_leaf_boundary(module, packed)


def _mapping(node: ResourceInstance, port: str):
    item = next((value for value in node.semantic_mappings if value.resource_port == port), None)
    if item is None or item.expression is None:
        raise SystemVerilogEmissionError(
            f"selected resource '{node.identity}' has no expression mapping for port '{port}'"
        )
    return item.expression


def _configuration(node: ResourceInstance) -> dict[str, int | str]:
    return dict(node.configuration)


def _dsp48e1_parameters(
    config: dict[str, int | str], *, terminal: bool,
) -> tuple[tuple[str, int], ...]:
    """Return source-described DSP register settings for one instance."""
    return (
        ("ACASCREG", 0), ("ADREG", int(config.get("adreg", 0))),
        ("ALUMODEREG", 0), ("AREG", int(config.get("areg", 0))),
        ("BCASCREG", 0), ("BREG", int(config.get("breg", 0))),
        ("CARRYINREG", 0), ("CARRYINSELREG", 0),
        ("CREG", int(config.get("creg", 0))),
        ("DREG", int(config.get("dreg", 0))), ("INMODEREG", 0),
        ("MREG", int(config.get("mreg", 0))), ("OPMODEREG", 0),
        ("PREG", int(config.get("preg", 0)) or (
            int(config.get("terminal_preg", 0)) if terminal else 0
        )),
    )


def _signed_extend(value, width: int) -> str:
    rendered = _expression(value)
    source_width = value.type.width
    if source_width > width:
        raise SystemVerilogEmissionError(
            f"selected expression width {source_width} exceeds physical port width {width}"
        )
    if source_width == width:
        return rendered
    # A sized SystemVerilog cast preserves the signed value while making the
    # physical port width explicit.  It also works for packed-vector slices,
    # for which a second indexing operation is not portable across tools.
    return f"{width}'($signed({rendered}))"


def _emit_signed_product_dsp48e1_graph(module, graph, definitions, simulation_model):
    """Emit an ordered exact signed-product cascade from a selected graph.

    The mapper has already proved typed widths, signedness, accumulator modes,
    inventory and dedicated-link topology. This emitter consumes those graph
    facts only; it does not rediscover arithmetic from source names or RTL
    text. The first physical slice is scalar and covers the FFT real/imag
    two-term reduction. Complex/aggregate pipeline outputs remain excluded.
    """
    if not graph.resources or len(graph.dedicated_edges) != len(graph.resources) - 1:
        raise SystemVerilogEmissionError(
            "signed-product DSP cascade requires one dedicated edge between adjacent resources"
        )
    if module.clock is None or module.reset is None:
        raise SystemVerilogEmissionError(
            "selected signed-product cascade requires one explicit clock and reset"
        )
    physical_names = {
        item.identity: f"zlang_sp_dsp{index}"
        for index, item in enumerate(graph.resources)
    }
    for index, edge in enumerate(graph.dedicated_edges):
        source = graph.resources[index].identity
        destination = graph.resources[index + 1].identity
        actual = (
            edge.source_instance, edge.source_port,
            edge.destination_instance, edge.destination_port, edge.width,
        )
        expected = (source, "pcout", destination, "pcin", 48)
        if actual != expected or edge.kind != "pcascade":
            raise SystemVerilogEmissionError(
                "illegal signed-product dedicated edge: expected adjacent 48-bit PCOUT -> PCIN cascade"
            )

    ports = _physical_port_declarations(module)
    lines: list[str] = [
        "  // Selected ordered signed-product target implementation graph.",
    ]
    for index, node in enumerate(graph.resources):
        config = _configuration(node)
        mode = config.get("accumulator_mode")
        if mode not in {"accumulator_plus_product", "accumulator_minus_product"}:
            raise SystemVerilogEmissionError(
                f"signed-product resource '{node.identity}' has unsupported accumulator mode '{mode}'"
            )
        physical = physical_names[node.identity]
        a_value = _mapping(node, "a")
        b_value = _mapping(node, "b")
        if a_value.type.width > 25 or b_value.type.width > 18:
            raise SystemVerilogEmissionError(
                f"signed-product resource '{node.identity}' mapping exceeds DSP48E1 ports"
            )
        terminal = index == len(graph.resources) - 1
        parameter_text = ",\n".join(
            f"    .{name}({value})"
            for name, value in _dsp48e1_parameters(config, terminal=terminal)
        )
        pcin = (
            "48'sd0" if index == 0
            else f"{physical_names[graph.resources[index - 1].identity]}_pcout"
        )
        # With OPMODE selecting X=M, Y=0 and Z=PCIN, ALUMODE 0011 is
        # Z-(X-Y-CIN), i.e. the source-advertised accumulator-minus-product.
        alumode = "4'b0011" if mode == "accumulator_minus_product" else "4'b0000"
        lines.extend((
            f"  logic signed [24:0] {physical}_a;",
            f"  logic signed [24:0] {physical}_d;",
            f"  logic signed [17:0] {physical}_b;",
            f"  logic signed [47:0] {physical}_p;",
            f"  logic signed [47:0] {physical}_pcout;",
            f"  assign {physical}_a = {_signed_extend(a_value, 25)};",
            f"  assign {physical}_d = 25'sd0;",
            f"  assign {physical}_b = {_signed_extend(b_value, 18)};",
            "  DSP48E1 #(",
            parameter_text + ",",
            '    .A_INPUT("DIRECT"),',
            '    .B_INPUT("DIRECT"),',
            '    .USE_DPORT("TRUE"),',
            '    .USE_MULT("MULTIPLY"),',
            '    .USE_SIMD("ONE48")',
            f"  ) {physical}_primitive (",
            f"    .A({{{{5{{{physical}_a[24]}}}}, {physical}_a}}),",
            f"    .D({physical}_d), .B({physical}_b), .C(48'sd0),",
            f"    .ACIN(30'sd0), .BCIN(18'sd0), .PCIN({pcin}),",
            "    .CARRYCASCIN(1'b0), .MULTSIGNIN(1'b0),",
            f"    .ALUMODE({alumode}), .CARRYIN(1'b0), .CARRYINSEL(3'b000),",
            "    .INMODE(5'b00100), .OPMODE(7'b0010101),",
            f"    .CLK({_identifier(module.clock)}),",
            "    .CEA1(1'b1), .CEA2(1'b1), .CEAD(1'b1), .CEALUMODE(1'b1),",
            "    .CEB1(1'b1), .CEB2(1'b1), .CEC(1'b1), .CECARRYIN(1'b1),",
            "    .CECTRL(1'b1), .CED(1'b1), .CEINMODE(1'b1), .CEM(1'b1), .CEP(1'b1),",
            f"    .RSTA({_identifier(module.reset)}), .RSTB({_identifier(module.reset)}),",
            f"    .RSTC({_identifier(module.reset)}), .RSTD({_identifier(module.reset)}),",
            f"    .RSTM({_identifier(module.reset)}), .RSTP({_identifier(module.reset)}),",
            f"    .RSTCTRL({_identifier(module.reset)}), .RSTINMODE({_identifier(module.reset)}),",
            f"    .RSTALUMODE({_identifier(module.reset)}), .RSTALLCARRYIN({_identifier(module.reset)}),",
            f"    .P({physical}_p), .PCOUT({physical}_pcout)",
            "  );",
        ))

    conversion = graph.quantization
    if not isinstance(conversion, expr.FixedConvert) or not isinstance(
        conversion.expression.type, FixedType
    ):
        raise SystemVerilogEmissionError(
            "selected signed-product cascade has no typed final fixed-point conversion"
        )
    physical_accumulator_type = FixedType(48, conversion.expression.type.fraction)
    physical_conversion = replace(
        conversion,
        expression=expr.InputRef(
            "zlang_signed_product_acc", physical_accumulator_type
        ),
    )
    last_physical = physical_names[graph.resources[-1].identity]
    register_assignment = next(
        (item for item in module.next_assignments if item.expression == conversion), None
    )
    if register_assignment is not None and isinstance(register_assignment.target, Register):
        final_register = _final_conversion_register(module, register_assignment.target)
        output_assignment = next(
            (item for item in module.assignments
             if isinstance(item.expression, expr.RegisterRef)
             and item.expression.name == final_register.name), None,
        )
        if output_assignment is None or not hasattr(output_assignment.target, "name"):
            raise SystemVerilogEmissionError(
                "selected signed-product cascade output register is not exposed by one module output"
            )
        lines.extend((
            "  logic signed [47:0] zlang_signed_product_acc;",
            f"  logic signed [{final_register.type.width - 1}:0] {_identifier(final_register.name)};",
            f"  assign zlang_signed_product_acc = {last_physical}_p;",
            f"  always_ff @({clock_event(module, _identifier)}) begin",
            f"    if ({reset_asserted(module, _identifier)}) {_identifier(final_register.name)} <= {_expression(final_register.initial)};",
            f"    else {_identifier(final_register.name)} <= {_expression(physical_conversion)};",
            "  end",
            f"  assign {_identifier(output_assignment.target.name)} = {_identifier(final_register.name)};",
        ))
    else:
        exploration = next(
            (item for item in module.pipeline_explorations
             if item.source_expression == conversion), None,
        )
        if exploration is None:
            raise SystemVerilogEmissionError(
                "selected signed-product cascade has neither a registered boundary nor a typed pipeline(auto) region"
            )
        compensation = sum(
            item.cycles for item in (
                graph.timing_dag.compensation_delays if graph.timing_dag else ()
            )
        )
        output_registers = 1 + compensation
        names = tuple(
            f"__target_result_q{index}" for index in range(output_registers)
        )
        lines.extend((
            "  logic signed [47:0] zlang_signed_product_acc;",
            *(f"  logic signed [{conversion.type.width - 1}:0] {name};" for name in names),
            f"  assign zlang_signed_product_acc = {last_physical}_p;",
            f"  always_ff @({clock_event(module, _identifier)}) begin",
            f"    if ({reset_asserted(module, _identifier)}) begin",
            *(f"      {name} <= '0;" for name in names),
            "    end else begin",
            f"      {names[0]} <= {_expression(physical_conversion)};",
            *(f"      {names[index]} <= {names[index - 1]};"
              for index in range(1, len(names))),
            "    end",
            "  end",
            f"  assign {_identifier(exploration.output)} = {names[-1]};",
        ))
    top = "\n".join((
        f"module {_identifier(module.name)} (",
        ",\n".join(f"  {item}" for item in ports),
        ");",
        *lines,
        "endmodule",
    ))
    model = _dsp48e1_simulation_model() + "\n" if simulation_model else ""
    return f"`default_nettype none\n{model}{top}\n`default_nettype wire\n"


def _emit_dsp48e1_graph(module, graph, definitions, simulation_model):
    if len(graph.resources) != 4 or len(graph.dedicated_edges) != 3:
        raise SystemVerilogEmissionError("bounded DSP cascade emitter requires four resources and three dedicated edges")
    if module.clock is None or module.reset is None:
        raise SystemVerilogEmissionError("selected DSP cascade requires one explicit clock and reset")
    for index, edge in enumerate(graph.dedicated_edges):
        expected = (f"dsp{index}", "pcout", f"dsp{index + 1}", "pcin", 48)
        actual = (edge.source_instance, edge.source_port, edge.destination_instance,
                  edge.destination_port, edge.width)
        if actual != expected or edge.kind != "pcascade":
            raise SystemVerilogEmissionError(
                f"illegal dedicated edge '{edge.identity}': expected adjacent 48-bit PCOUT -> PCIN cascade"
            )
    ports = _physical_port_declarations(module)
    lines: list[str] = ["  // Selected source-described target implementation graph."]
    for node in graph.resources:
        config = _configuration(node)
        terminal = node.identity == graph.resources[-1].identity
        lines.extend((
            f"  logic signed [24:0] {node.identity}_a;",
            f"  logic signed [24:0] {node.identity}_d;",
            f"  logic signed [17:0] {node.identity}_b;",
            f"  logic signed [47:0] {node.identity}_p;",
            f"  logic signed [47:0] {node.identity}_pcout;",
            f"  assign {node.identity}_a = {_signed_extend(_mapping(node, 'a'), 25)};",
            f"  assign {node.identity}_d = {_signed_extend(_mapping(node, 'd'), 25)};",
            f"  assign {node.identity}_b = {_signed_extend(_mapping(node, 'b'), 18)};",
        ))
        pcin = "48'sd0" if node.identity == "dsp0" else f"dsp{int(node.identity[3:]) - 1}_pcout"
        parameters = _dsp48e1_parameters(config, terminal=terminal)
        parameter_text = ",\n".join(f"    .{name}({value})" for name, value in parameters)
        lines.extend((
            "  DSP48E1 #(",
            parameter_text + ",",
            '    .A_INPUT("DIRECT"),',
            '    .B_INPUT("DIRECT"),',
            '    .USE_DPORT("TRUE"),',
            '    .USE_MULT("MULTIPLY"),',
            '    .USE_SIMD("ONE48")',
            f"  ) {node.identity}_primitive (",
            f"    .A({{{{5{{{node.identity}_a[24]}}}}, {node.identity}_a}}),",
            f"    .D({node.identity}_d), .B({node.identity}_b), .C(48'sd0),",
            f"    .ACIN(30'sd0), .BCIN(18'sd0), .PCIN({pcin}),",
            "    .CARRYCASCIN(1'b0), .MULTSIGNIN(1'b0),",
            "    .ALUMODE(4'b0000), .CARRYIN(1'b0), .CARRYINSEL(3'b000),",
            "    .INMODE(5'b00100), .OPMODE(7'b0010101),",
            f"    .CLK({_identifier(module.clock)}),",
            "    .CEA1(1'b1), .CEA2(1'b1), .CEAD(1'b1), .CEALUMODE(1'b1),",
            "    .CEB1(1'b1), .CEB2(1'b1), .CEC(1'b1), .CECARRYIN(1'b1),",
            "    .CECTRL(1'b1), .CED(1'b1), .CEINMODE(1'b1), .CEM(1'b1), .CEP(1'b1),",
            f"    .RSTA({_identifier(module.reset)}), .RSTB({_identifier(module.reset)}),",
            f"    .RSTC({_identifier(module.reset)}), .RSTD({_identifier(module.reset)}),",
            f"    .RSTM({_identifier(module.reset)}), .RSTP({_identifier(module.reset)}),",
            f"    .RSTCTRL({_identifier(module.reset)}), .RSTINMODE({_identifier(module.reset)}),",
            f"    .RSTALUMODE({_identifier(module.reset)}), .RSTALLCARRYIN({_identifier(module.reset)}),",
            f"    .P({node.identity}_p), .PCOUT({node.identity}_pcout)",
            "  );",
        ))
    conversion = graph.quantization
    if not isinstance(conversion, expr.FixedConvert) or not isinstance(conversion.expression.type, FixedType):
        raise SystemVerilogEmissionError("selected DSP cascade has no typed final fixed-point conversion")
    physical_accumulator_type = FixedType(48, conversion.expression.type.fraction)
    physical_conversion = replace(
        conversion,
        expression=expr.InputRef("dsp_acc", physical_accumulator_type),
    )
    register_assignment = next(
        (item for item in module.next_assignments if item.expression == conversion), None
    )
    if register_assignment is not None and isinstance(register_assignment.target, Register):
        final_register = _final_conversion_register(module, register_assignment.target)
        output_assignment = next(
            (item for item in module.assignments
             if isinstance(item.expression, expr.RegisterRef)
             and item.expression.name == final_register.name), None,
        )
        if output_assignment is None or not hasattr(output_assignment.target, "name"):
            raise SystemVerilogEmissionError("selected DSP cascade output register is not exposed by one module output")
        lines.extend((
            "  logic signed [47:0] dsp_acc;",
            f"  logic signed [{final_register.type.width - 1}:0] {_identifier(final_register.name)};",
            "  assign dsp_acc = dsp3_p;",
            f"  always_ff @({clock_event(module, _identifier)}) begin",
            f"    if ({reset_asserted(module, _identifier)}) {_identifier(final_register.name)} <= {_expression(final_register.initial)};",
            f"    else {_identifier(final_register.name)} <= {_expression(physical_conversion)};",
            "  end",
            f"  assign {_identifier(output_assignment.target.name)} = {_identifier(final_register.name)};",
        ))
    else:
        exploration = next(
            (item for item in module.pipeline_explorations
             if item.source_expression == conversion), None,
        )
        if exploration is None:
            raise SystemVerilogEmissionError(
                "selected DSP cascade has neither a registered boundary nor a typed pipeline(auto) region"
            )
        compensation = sum(
            item.cycles for item in (
                graph.timing_dag.compensation_delays if graph.timing_dag else ()
            )
        )
        output_registers = 1 + compensation
        names = tuple(f"__target_result_q{index}" for index in range(output_registers))
        lines.extend((
            "  logic signed [47:0] dsp_acc;",
            *(f"  logic signed [{conversion.type.width - 1}:0] {name};" for name in names),
            "  assign dsp_acc = dsp3_p;",
            f"  always_ff @({clock_event(module, _identifier)}) begin",
            f"    if ({reset_asserted(module, _identifier)}) begin",
            *(f"      {name} <= '0;" for name in names),
            "    end else begin",
            f"      {names[0]} <= {_expression(physical_conversion)};",
            *(f"      {names[index]} <= {names[index - 1]};"
              for index in range(1, len(names))),
            "    end",
            "  end",
            f"  assign {_identifier(exploration.output)} = {names[-1]};",
        ))
    top = "\n".join((
        f"module {_identifier(module.name)} (",
        ",\n".join(f"  {item}" for item in ports),
        ");",
        *lines,
        "endmodule",
    ))
    model = _dsp48e1_simulation_model() + "\n" if simulation_model else ""
    return f"`default_nettype none\n{model}{top}\n`default_nettype wire\n"


def _final_conversion_register(module: Module, first: Register) -> Register:
    current = first
    visited = {current.name}
    while not any(
        isinstance(item.expression, expr.RegisterRef) and item.expression.name == current.name
        for item in module.assignments
    ):
        followers = tuple(
            item.target for item in module.next_assignments
            if isinstance(item.target, Register)
            and isinstance(item.expression, expr.RegisterRef)
            and item.expression.name == current.name
        )
        if len(followers) != 1 or followers[0].name in visited:
            raise SystemVerilogEmissionError(
                "selected DSP cascade has no deterministic output-delay chain"
            )
        current = followers[0]
        visited.add(current.name)
    return current


def _dsp48e1_simulation_model() -> str:
    return r'''module DSP48E1 #(
  parameter integer ACASCREG=0, ADREG=0, ALUMODEREG=0, AREG=0,
  parameter integer BCASCREG=0, BREG=0, CARRYINREG=0, CARRYINSELREG=0,
  parameter integer CREG=0, DREG=0, INMODEREG=0, MREG=0, OPMODEREG=0, PREG=0,
  parameter A_INPUT="DIRECT", B_INPUT="DIRECT", USE_DPORT="TRUE",
  parameter USE_MULT="MULTIPLY", USE_SIMD="ONE48"
) (
  input wire [29:0] A, input wire [24:0] D, input wire [17:0] B,
  input wire [47:0] C, input wire [29:0] ACIN, input wire [17:0] BCIN,
  input wire [47:0] PCIN, input wire CARRYCASCIN, input wire MULTSIGNIN,
  input wire [3:0] ALUMODE, input wire CARRYIN, input wire [2:0] CARRYINSEL,
  input wire [4:0] INMODE, input wire [6:0] OPMODE, input wire CLK,
  input wire CEA1, CEA2, CEAD, CEALUMODE, CEB1, CEB2, CEC, CECARRYIN,
  input wire CECTRL, CED, CEINMODE, CEM, CEP,
  input wire RSTA, RSTB, RSTC, RSTD, RSTM, RSTP, RSTCTRL, RSTINMODE,
  input wire RSTALUMODE, RSTALLCARRYIN,
  output wire [47:0] P, output wire [47:0] PCOUT
);
  reg [29:0] a_q; reg [24:0] d_q; reg [17:0] b_q;
  reg signed [42:0] product_q;
  reg signed [47:0] p_q;
  logic [29:0] a_value;
  logic [24:0] d_value;
  logic [17:0] b_value;
  logic signed [24:0] preadd;
  logic signed [42:0] product_comb;
  logic signed [42:0] product;
  logic signed [47:0] product_extended;
  logic signed [47:0] result_value;
  always @(posedge CLK) begin
    if (RSTA) a_q <= 0; else if (CEA1) a_q <= A;
    if (RSTD) d_q <= 0; else if (CED) d_q <= D;
    if (RSTB) b_q <= 0; else if (CEB1) b_q <= B;
  end
  assign a_value = AREG == 0 ? A : a_q;
  assign d_value = DREG == 0 ? D : d_q;
  assign b_value = BREG == 0 ? B : b_q;
  assign preadd = $signed(a_value[24:0]) + $signed(d_value);
  assign product_comb = preadd * $signed(b_value);
  always @(posedge CLK) begin
    if (RSTM) product_q <= 0; else if (CEM) product_q <= product_comb;
  end
  assign product = MREG == 0 ? product_comb : product_q;
  assign product_extended = {{5{product[42]}}, product};
  assign result_value = ALUMODE == 4'b0011
    ? $signed(PCIN) - product_extended
    : $signed(PCIN) + product_extended;
  always @(posedge CLK) begin
    if (RSTP) p_q <= 0; else if (CEP) p_q <= result_value;
  end
  assign P = PREG == 0 ? result_value : p_q;
  assign PCOUT = P;
endmodule'''


def emit_target_artifact(
    module: Module,
    graph: ImplementationGraph,
    *,
    simulation_model: bool = False,
    selected_ir_identity: str | None = None,
):
    if (
        not graph.is_generic
        and graph.realization_backend != "direct_systemverilog"
    ):
        raise SystemVerilogEmissionError(
            "direct SystemVerilog cannot publish an implementation graph realized "
            f"by '{graph.realization_backend}'"
        )
    if graph.is_generic:
        base = emit_generic_artifact(
            module,
            selected_ir_identity=selected_ir_identity or graph.identity,
        )
    else:
        text = emit_target(module, graph, simulation_model=simulation_model)
        base = publish_artifact(
            module, text, backend="direct_systemverilog",
            selected_ir_identity=selected_ir_identity or graph.identity,
            companions=collect_rom_companions(module),
        )
    counts = Counter(item.resource_definition_identity for item in graph.resources)
    timing = graph.timing_dag
    selected_cost = dict(
        (name, (value, source)) for name, value, source in graph.selected_cost
    )
    implementation = ImplementationManifest(
        graph.identity, graph.semantic_region_identity,
        graph.architecture_template_identity, graph.target_identity,
        graph.target_hash, graph.resource_definition_hashes,
        tuple(ImplementationResourceManifest(
            item.identity, item.resource_definition_identity, item.operation,
            item.configuration,
            tuple((mapping.resource_port, mapping.semantic_identity)
                  for mapping in item.semantic_mappings),
            next((mapping.expression.origin.render()
                  for mapping in item.semantic_mappings
                  if mapping.expression is not None and mapping.expression.origin is not None), None),
        ) for item in graph.resources),
        tuple(ImplementationEdgeManifest(
            item.identity, item.kind, item.source_instance, item.source_port,
            item.destination_instance, item.destination_port, item.width,
            item.placement_relation, item.latency, item.fabric_fallback,
        ) for item in graph.dedicated_edges),
        graph.latency, graph.initiation_interval, tuple(sorted(counts.items())),
        base.artifact_hash,
        graph.target_family_identity, graph.architecture_template_hash,
        graph.target_dependency_hashes, graph.architecture_dependency_hashes,
        graph.selection_policy,
        graph.target_part,
        graph.pipeline_configuration_identity,
        graph.active_pipeline_sites,
        graph.physical_binding_identities,
        tuple(sorted(counts.items())) if not graph.is_generic else (),
        (),
        tuple(
            (name, str(source)) for name, (_, source) in selected_cost.items()
            if name in {"fmax_est", "lut", "ff", "dsp", "bram"}
        ),
        graph.policy_requirements,
        graph.objective,
        graph.selected_cost,
        timing.identity if timing else None,
        tuple(
            (item.identity, item.kind, item.implementation_node_identity,
             item.latency, item.estimated_delay_ps)
            for item in (timing.nodes if timing else ())
        ),
        tuple(
            (item.identity, item.kind, item.source_node, item.destination_node,
             item.latency, item.estimated_delay_ps, item.dedicated_edge_identity)
            for item in (timing.edges if timing else ())
        ),
        tuple(
            (item.identity, item.node_identity, item.kind, item.cycles,
             item.resource_instance_identity, item.pipeline_site_identity)
            for item in (timing.cuts if timing else ())
        ),
        tuple(
            (item.identity, item.source_node, item.destination_node, item.cycles,
             item.width, item.ff_cost)
            for item in (timing.alignment_delays if timing else ())
        ),
        tuple(
            (item.identity, item.source_node, item.destination_node, item.cycles,
             item.width, item.ff_cost)
            for item in (timing.compensation_delays if timing else ())
        ),
        graph.evidence_identity,
        graph.realization_backend,
        graph.latency_knowledge,
    )
    return replace(
        base,
        manifest_version=max(
            base.manifest_version, IMPLEMENTATION_MANIFEST_VERSION
        ),
        implementation=implementation,
    )


__all__ = ["emit_target", "emit_target_artifact"]
