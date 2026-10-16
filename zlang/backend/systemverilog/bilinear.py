# SPDX-License-Identifier: Apache-2.0
"""Emit an already-bound parallel product graph and its joint output stage."""

from __future__ import annotations

from zlang.ir import expressions as e
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.ir.expression_graph import ExpressionDagIndex
from zlang.ir.joint_values import map_joint_value
from zlang.backend.systemverilog import (
    boundary,
    expression,
    materialized,
    module_rendering,
    rendering,
)
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.identifiers import allocate_private_rtl_identifier


def emit_parallel_products(module, graph, simulation_model):
    from zlang.backend.systemverilog.target import (
        _dsp48e1_parameters,
        _dsp48e1_simulation_model,
        _mapping,
        _signed_extend,
    )

    if (
        graph.latency not in (0, 1)
        or graph.initiation_interval != 1
        or graph.dedicated_edges
    ):
        raise SystemVerilogEmissionError(
            "invalid parallel-product timing/binding contract"
        )
    value = graph.quantization
    if value is None:
        raise SystemVerilogEmissionError("parallel-product graph has no joint value")
    assignments = tuple(
        item
        for item in module.assignments
        if item.expression == value
        or (
            isinstance(item.expression, e.Pipeline)
            and item.expression.expression == value
        )
    )
    if len(assignments) != 1 or len(module.assignments) != 1:
        raise SystemVerilogEmissionError(
            "parallel-product emitter requires one complete joint output"
        )
    if graph.latency and (module.clock is None or module.reset is None):
        raise SystemVerilogEmissionError(
            "registered parallel products require clock and reset"
        )
    expected = {
        expression_semantic_identity(item)
        for item in ExpressionDagIndex((value,))
        if isinstance(item, e.Binary) and item.operator is e.BinaryOperator.MULTIPLY
    }
    mapped = tuple(
        expression_semantic_identity(_mapping(node, "p")) for node in graph.resources
    )
    if set(mapped) != expected or len(mapped) != len(expected):
        raise SystemVerilogEmissionError(
            "parallel products have incomplete or duplicate physical bindings"
        )
    owner = materialized.module_materialization_owner(module)
    used = set(owner.names.allocated_names)

    def private_name(preferred, identity):
        return allocate_private_rtl_identifier(
            preferred, semantic_identity=identity, used=used
        )

    replacements, lines = {}, []
    for node in graph.resources:
        a, b, product = (_mapping(node, port) for port in ("a", "b", "p"))
        if not (
            isinstance(product, e.Binary)
            and (a, b)
            in (
                (product.left, product.right),
                (product.right, product.left),
            )
        ):
            raise SystemVerilogEmissionError(
                "physical multiplier operands differ from the selected value"
            )
        name = private_name(node.identity, f"{node.identity}:value")
        physical = private_name(f"{node.identity}_p", f"{node.identity}:physical")
        instance = private_name(
            f"{node.identity}_primitive", f"{node.identity}:instance"
        )
        replacements[expression_semantic_identity(product)] = e.InputRef(
            name, product.type
        )
        parameters = ", ".join(
            f".{key}({value})" for key, value in _dsp48e1_parameters({}, terminal=False)
        )
        if simulation_model:
            parameters += (
                f", .ZLANG_A_WIDTH({a.type.width}), .ZLANG_D_WIDTH(25)"
                f", .ZLANG_B_WIDTH({b.type.width}), .ZLANG_PRE_WIDTH({a.type.width})"
                f", .ZLANG_ACC_WIDTH({product.type.width})"
            )
        clock = rendering._identifier(module.clock) if module.clock else "1'b0"
        reset = rendering._identifier(module.reset) if module.reset else "1'b0"
        lines.extend(
            (
                f"  logic signed [{product.type.width - 1}:0] {name};",
                f"  logic [47:0] {physical};",
                f"  assign {name} = {physical}[{product.type.width - 1}:0];",
                f'  DSP48E1 #({parameters}, .A_INPUT("DIRECT"), .B_INPUT("DIRECT"),',
                '    .USE_DPORT("FALSE"), .USE_MULT("MULTIPLY"), .USE_SIMD("ONE48"))',
                f"  {instance} (",
                f"    .A(30'({_signed_extend(owner.physicalize(a), 25)})),",
                f"    .B({_signed_extend(owner.physicalize(b), 18)}), .D(25'sd0), .C(48'sd0),",
                "    .ACIN(30'sd0), .BCIN(18'sd0), .PCIN(48'sd0),",
                "    .CARRYCASCIN(1'b0), .MULTSIGNIN(1'b0), .ALUMODE(4'b0000),",
                "    .CARRYIN(1'b0), .CARRYINSEL(3'b000), .INMODE(5'b00000), .OPMODE(7'b0000101),",
                f"    .CLK({clock}),",
                "    .CEA1(1'b1), .CEA2(1'b1), .CEAD(1'b1), .CEALUMODE(1'b1),",
                "    .CEB1(1'b1), .CEB2(1'b1), .CEC(1'b1), .CECARRYIN(1'b1),",
                "    .CECTRL(1'b1), .CED(1'b1), .CEINMODE(1'b1), .CEM(1'b1), .CEP(1'b1),",
                f"    .RSTA({reset}), .RSTB({reset}), .RSTC({reset}), .RSTD({reset}),",
                f"    .RSTM({reset}), .RSTP({reset}), .RSTCTRL({reset}), .RSTINMODE({reset}),",
                f"    .RSTALUMODE({reset}), .RSTALLCARRYIN({reset}), .P({physical}),",
                "    .PCOUT()",
                "  );",
            )
        )

    def substitute(item):
        if isinstance(item, e.Binary) and item.operator is e.BinaryOperator.MULTIPLY:
            return replacements.get(expression_semantic_identity(item))
        return None

    result = owner.physicalize(map_joint_value(value, substitute))
    rhs = expression._expression(result)
    output = rendering._identifier(assignments[0].target.name)
    if graph.latency:
        stored = private_name("joint_output", "parallel_bilinear:joint_output")
        lines.extend(
            (
                f"  logic [{value.type.width - 1}:0] {stored};",
                f"  always_ff @(posedge {clock}) begin",
                f"    if ({reset}) {stored} <= '0;",
                f"    else {stored} <= {rhs};",
                "  end",
                f"  assign {output} = {stored};",
            )
        )
    else:
        lines.append(f"  assign {output} = {rhs};")
    rtl = module_rendering._module(
        module, boundary._physical_port_declarations(module), lines
    )
    model = _dsp48e1_simulation_model() + "\n" if simulation_model else ""
    return "`default_nettype none\n" + model + rtl + "`default_nettype wire\n"
