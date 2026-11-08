"""Direct-SV rendering of qualified 18x18 DSP plus explicit fabric graphs."""

from __future__ import annotations

from dataclasses import replace

from zlang.backend.identifiers import allocate_private_rtl_identifier
from zlang.backend.systemverilog import (
    boundary,
    expression,
    materialized,
    module_rendering,
    rendering,
)
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.systemverilog.fpga_primitives import (
    multiplier_instance,
    multiplier_models,
)
from zlang.ir import expressions as e
from zlang.ir import types as t
from zlang.ir.expression_graph import ExpressionDagIndex
from zlang.ir.joint_values import map_joint_value, arithmetic as typed_arithmetic
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.target_fpga_bindings import fpga_dsp_binding, fpga_multiplier_layout


def _signed(type_):
    return isinstance(type_, (t.SIntType, t.FixedType))


def _output(module, value):
    from zlang.backend.systemverilog.target import (
        _implementation_output,
        _final_conversion_register,
    )

    output = _implementation_output(module, value)
    if output is not None:
        return output
    for item in module.next_assignments:
        if item.expression == value:
            last = _final_conversion_register(module, item.target)
            matches = [
                a.target.name
                for a in module.assignments
                if isinstance(a.expression, e.RegisterRef)
                and a.expression.name == last.name
            ]
            if len(matches) == 1:
                return matches[0]
    matches = [a.target.name for a in module.assignments if a.expression == value]
    if len(matches) != 1:
        raise SystemVerilogEmissionError(
            "FPGA DSP requires one complete output boundary"
        )
    return matches[0]


def _fir_value(products, arithmetic, name, lines):
    """Four typed preadd-products and their explicit pairwise fabric tree."""
    if len(products) != 4:
        raise SystemVerilogEmissionError("FPGA FIR requires four ordered products")
    values = []
    for index, (node, operands, cfg, product, signed) in enumerate(products):
        if not signed or cfg.get("fabric_reduction_topology") != "balanced_pairwise":
            raise SystemVerilogEmissionError("unqualified FIR fabric topology")
        a, d, b = (operands[k] for k in ("a", "d", "b"))
        if any(
            cfg.get(key) != getattr(value, "index", None)
            for key, value in (
                ("sample_left_index", a),
                ("sample_right_index", d),
                ("coefficient_index", b),
            )
        ):
            raise SystemVerilogEmissionError(
                "FIR sample/coefficient order differs from typed bindings"
            )
        width = max(a.type.width, d.type.width) + 1 + b.type.width
        type_ = t.FixedType(width, a.type.fraction + b.type.fraction)
        signal = name(f"zlang_fpga_fir_product{index}", f"{node.identity}:fir-product")
        lines.extend(
            (
                f"  wire [{width - 1}:0] {signal};",
                f"  assign {signal} = {product}[{width - 1}:0];",
            )
        )
        values.append(e.InputRef(signal, type_))
    value = typed_arithmetic(
        "+",
        typed_arithmetic("+", values[0], values[1]),
        typed_arithmetic("+", values[2], values[3]),
    )
    if (
        value.type.fraction != arithmetic.type.fraction
        or value.type.width > arithmetic.type.width
    ):
        raise SystemVerilogEmissionError(
            "FIR reduction cannot preserve the exact carrier"
        )
    signal = name("zlang_fpga_fir_sum", "fir:balanced-sum")
    width = arithmetic.type.width
    lines.extend(
        (
            f"  wire [{width - 1}:0] {signal};",
            f"  assign {signal} = {width}'($signed({expression._expression(value)}));",
        )
    )
    return e.InputRef(signal, arithmetic.type)


def _fir_round_sat(value, replacement, name, lines):
    """Exact signed nearest-even conversion using floor quotient/remainder.

    A two's-complement floor quotient rounds up iff guard && (sticky || odd).
    This avoids an absolute-value/negate path; the extra quotient bit preserves
    the maximum positive rounded value before the sole final saturation.
    """
    shift = replacement.type.fraction - value.type.fraction
    if (
        shift < 1
        or value.rounding is not e.FixedRounding.NEAREST_EVEN
        or value.overflow is not e.FixedOverflow.SATURATE
    ):
        return expression._expression(replace(value, expression=replacement))
    width, out_width = replacement.type.width, value.type.width
    quotient_width = width - shift + 1
    raw = expression._expression(replacement)
    guard = f"{raw}[{shift - 1}]"
    sticky = f"(|{raw}[{shift - 2}:0]) || " if shift > 1 else ""
    increment = f"({guard} && ({sticky}{raw}[{shift}]))"
    rounded = name("zlang_fpga_fir_rounded", "fir:rounded-quotient")
    lines.extend(
        (
            f"  wire signed [{quotient_width - 1}:0] {rounded};",
            f"  assign {rounded} = {quotient_width}'($signed({raw}[{width - 1}:{shift}])) + {quotient_width}'($unsigned({increment}));",
        )
    )
    if quotient_width <= out_width:
        return f"{out_width}'($signed({rounded}))"
    maximum, minimum = (1 << (out_width - 1)) - 1, 1 << (out_width - 1)
    result = name("zlang_fpga_fir_converted", "fir:converted")
    lines.extend(
        (
            f'  (* zlang_fir_quantizer = "{width}:{replacement.type.fraction}:{out_width}:{value.type.fraction}", zlang_fir_quantizer_input = "{raw}" *) wire [{out_width - 1}:0] {result};',
            f"  assign {result} = (($signed({rounded}) > {quotient_width}'sd{maximum}) ? {out_width}'h{maximum:x} : "
            f"(($signed({rounded}) < -{quotient_width}'sd{minimum}) ? {out_width}'h{minimum:x} : {rounded}[{out_width - 1}:0]));",
        )
    )
    return result


def emit_fpga_dsp(module, graph, definitions, simulation_model):
    if graph.dedicated_edges or graph.initiation_interval != 1:
        raise SystemVerilogEmissionError(
            "DSP + fabric must not claim a dedicated cascade or sharing"
        )
    bindings = {fpga_dsp_binding(r) for r in definitions.values()}
    if len(bindings) != 1 or None in bindings:
        raise SystemVerilogEmissionError("mixed/unqualified FPGA DSP bindings")
    binding = bindings.pop()
    family, physical_width, _ = fpga_multiplier_layout(binding, dict(graph.resources[0].configuration))
    if any(fpga_multiplier_layout(binding, dict(n.configuration))[:2] != (family, physical_width)
           for n in graph.resources):
        raise SystemVerilogEmissionError("mixed FPGA multiplier modes")
    value = graph.quantization
    if value is None:
        raise SystemVerilogEmissionError("FPGA DSP has no typed value")
    arithmetic = value.expression if isinstance(value, e.FixedConvert) else value
    configs = [dict(node.configuration) for node in graph.resources]
    cycles = {c.get("native_product_pipeline") for c in configs}
    if len(cycles) != 1 or None in cycles:
        raise SystemVerilogEmissionError("missing/incoherent FPGA pipeline binding")
    native_cycles = cycles.pop()
    if native_cycles or graph.latency:
        if module.clock is None or module.reset is None:
            raise SystemVerilogEmissionError(
                "registered FPGA DSP requires an explicit clock/reset"
            )
    clock = rendering._identifier(module.clock) if module.clock else "1'b0"
    reset = rendering._identifier(module.reset) if module.reset else "1'b0"
    owner = materialized.module_materialization_owner(module)
    used = set(owner.names.allocated_names)
    lines, replacements, products = [], {}, []

    def name(preferred, identity):
        return allocate_private_rtl_identifier(
            preferred, semantic_identity=identity, used=used
        )

    def render(v):
        physical = owner.physicalize(v)
        if any(
            isinstance(n, e.InputRef) and n.name not in {v.name for v in module.inputs}
            for n in ExpressionDagIndex((physical,))
        ):
            raise SystemVerilogEmissionError(
                "FPGA binding requires resolved pure operand values"
            )
        return expression._expression(physical)

    def extend(v, width):
        cast = "$signed" if _signed(v.type) else "$unsigned"
        return f"{width}'({cast}({render(v)}))"

    def delay(rhs, width, count, identity):
        if not count:
            return rhs
        names = [
            name(f"zlang_fpga_{identity}_q{i}", f"{identity}:delay:{i}")
            for i in range(count)
        ]
        lines.extend(f"  logic [{width - 1}:0] {n};" for n in names)
        lines.append(f"  always_ff @(posedge {clock}) begin")
        lines.append(f"    if ({reset}) begin")
        lines.extend(f"      {n} <= '0;" for n in names)
        lines.append("    end else begin")
        lines.append(f"      {names[0]} <= {rhs};")
        lines.extend(f"      {b} <= {a};" for a, b in zip(names, names[1:]))
        lines.extend(("    end", "  end"))
        return names[-1]

    for index, (node, config) in enumerate(zip(graph.resources, configs, strict=True)):
        if config.get("realization") != "dsp_plus_fabric":
            raise SystemVerilogEmissionError(
                "FPGA graph lacks explicit realization facts"
            )
        _, expected_width, expected_primitive = fpga_multiplier_layout(binding, config)
        if (config.get("native_primitive", expected_primitive) != expected_primitive
                or config.get("native_operand_width", expected_width) != expected_width):
            raise SystemVerilogEmissionError("FPGA primitive report differs from selected mode")
        operands = {
            m.resource_port: m.expression
            for m in node.semantic_mappings
            if m.expression is not None
        }
        a, b = operands.get("a"), operands.get("b")
        if a is None or b is None or type(a.type) is not type(b.type):
            raise SystemVerilogEmissionError(
                "FPGA multiplier requires uniform numeric signedness"
            )
        signed = _signed(a.type)
        if (config.get("native_asign"), config.get("native_bsign")) != (
            int(signed),
            int(signed),
        ):
            raise SystemVerilogEmissionError(
                "FPGA signedness binding differs from typed operands"
            )
        aa = name(f"zlang_fpga_a{index}", f"{node.identity}:a")
        bb = name(f"zlang_fpga_b{index}", f"{node.identity}:b")
        product = name(f"zlang_fpga_product{index}", f"{node.identity}:product")
        instance = name(f"zlang_fpga_multiplier{index}", f"{node.identity}:primitive")
        d = operands.get("d")
        a_rhs = extend(a, physical_width)
        if d is not None:
            required = max(a.type.width, d.type.width) + 1
            if (
                required > physical_width
                or type(d.type) is not type(a.type)
                or getattr(a.type, "fraction", 0) != getattr(d.type, "fraction", 0)
                or config.get("fabric_preadder_bits") != required
            ):
                raise SystemVerilogEmissionError(
                    "FPGA preadder cannot preserve the exact input format"
                )
            cast = "$signed" if signed else "$unsigned"
            a_rhs = f"{physical_width}'({cast}({required}'({extend(a, required)} + {extend(d, required)})))"
        if a.type.width > physical_width or b.type.width > physical_width:
            raise SystemVerilogEmissionError("FPGA multiplier port exceeds 18 bits")
        lines.extend(
            (
                f"  logic [{physical_width - 1}:0] {aa}, {bb};",
                f"  wire [{2 * physical_width - 1}:0] {product};",
                f"  assign {aa} = {a_rhs};",
                f"  assign {bb} = {extend(b, physical_width)};",
            )
        )
        lines.append(
            multiplier_instance(
                family,
                pipe_reg=config.get("mreg", 0),
                output_reg=config.get("product_oreg", 0),
                a=aa,
                b=bb,
                p=product,
                clock=clock,
                reset=reset,
                name=instance,
                signed=signed,
            )
        )
        products.append((node, operands, config, product, signed))
        if config.get("parallel_bilinear") == 1:
            p = operands.get("p")
            if (
                not isinstance(p, e.Binary)
                or p.operator is not e.BinaryOperator.MULTIPLY
                or (a, b) not in ((p.left, p.right), (p.right, p.left))
            ):
                raise SystemVerilogEmissionError(
                    "parallel FPGA product has an invalid typed binding"
                )
            physical_value = name(
                f"zlang_fpga_value{index}", f"{node.identity}:typed-value"
            )
            lines.extend(
                (
                    f"  wire [{p.type.width - 1}:0] {physical_value};",
                    f"  assign {physical_value} = {product}[{p.type.width - 1}:0];",
                )
            )
            key = expression_semantic_identity(p)
            if key in replacements:
                raise SystemVerilogEmissionError("duplicate FPGA product binding")
            replacements[key] = e.InputRef(physical_value, p.type)
    parallel = all(c.get("parallel_bilinear") == 1 for c in configs)
    if parallel:
        expected = {
            expression_semantic_identity(n)
            for n in ExpressionDagIndex((value,))
            if isinstance(n, e.Binary) and n.operator is e.BinaryOperator.MULTIPLY
        }
        if expected != set(replacements) or native_cycles:
            raise SystemVerilogEmissionError(
                "parallel FPGA products have incomplete bindings or timing"
            )

        def substitute(node):
            if (
                isinstance(node, e.Binary)
                and node.operator is e.BinaryOperator.MULTIPLY
            ):
                return replacements.get(expression_semantic_identity(node))
            return None

        rhs = expression._expression(
            owner.physicalize(map_joint_value(value, substitute))
        )
    elif len(products) > 1 and all(
        "d" in operands for _, operands, _, _, _ in products
    ):
        replacement = _fir_value(products, arithmetic, name, lines)
        rhs = _fir_round_sat(value, replacement, name, lines)
    else:
        width = arithmetic.type.width
        if (
            not isinstance(
                arithmetic.type, (t.SIntType, t.UIntType, t.FixedType, t.UFixedType)
            )
            or width > 54
        ):
            raise SystemVerilogEmissionError(
                "FPGA accumulation exceeds the qualified exact carrier"
            )
        previous = f"{width}'d0"
        for index, (node, operands, config, product, signed) in enumerate(products):
            product_width = operands["a"].type.width + operands["b"].type.width
            if "d" in operands:
                product_width = (
                    max(operands["a"].type.width, operands["d"].type.width)
                    + 1
                    + operands["b"].type.width
                )
            term = f"{width}'({'$signed' if signed else '$unsigned'}({product}[{product_width - 1}:0]))"
            c = operands.get("c", operands.get("pcin"))
            left = previous
            if c is not None:
                if config.get("fabric_addend_delay") != native_cycles:
                    raise SystemVerilogEmissionError(
                        "FPGA addend pipeline alignment differs from product"
                    )
                delayed = delay(
                    render(c), c.type.width, native_cycles, f"addend{index}"
                )
                left = f"{width}'({'$signed' if _signed(c.type) else '$unsigned'}({delayed}))"
            mode = config.get("accumulator_mode", "accumulator_plus_product")
            if mode == "accumulator_plus_product":
                rhs = f"{left} + {term}"
            elif mode == "accumulator_minus_product":
                rhs = f"{left} - {term}"
            elif mode == "product_minus_accumulator":
                rhs = f"{term} - {left}"
            else:
                raise SystemVerilogEmissionError("unqualified FPGA accumulator mode")
            accumulator = name(f"zlang_fpga_acc{index}", f"{node.identity}:accumulator")
            lines.extend(
                (
                    f"  wire [{width - 1}:0] {accumulator};",
                    f"  assign {accumulator} = {rhs};",
                )
            )
            previous = accumulator
        replacement = e.InputRef(previous, arithmetic.type)
        result = (
            replace(value, expression=replacement)
            if isinstance(value, e.FixedConvert)
            else replacement
        )
        rhs = expression._expression(result)
    if (
        native_cycles
        and len(products) > 1
        and all("d" in operands for _, operands, _, _, _ in products)
    ):
        raise SystemVerilogEmissionError(
            "native registered FIR composition is not qualified"
        )
    remaining = graph.latency - native_cycles
    if remaining < 0:
        raise SystemVerilogEmissionError("FPGA pipeline exceeds the output contract")
    stored = delay(rhs, value.type.width, remaining, "output")
    output = rendering._identifier(_output(module, value))
    if (
        len(module.outputs) != 1
        or module.instances
        or module.memories
        or module.fifos
        or module.rules
    ):
        raise SystemVerilogEmissionError(
            "FPGA arithmetic binding requires an isolated complete value region"
        )
    if len(module.assignments) != 1:
        raise SystemVerilogEmissionError(
            "FPGA arithmetic binding requires one complete output assignment"
        )
    if module.registers:
        first = next(
            (n for n in module.next_assignments if n.expression == value), None
        )
        chain = set()
        current = first.target.name if first is not None else None
        while current is not None and current not in chain:
            chain.add(current)
            followers = [
                n.target.name
                for n in module.next_assignments
                if isinstance(n.expression, e.RegisterRef)
                and n.expression.name == current
            ]
            if len(followers) > 1:
                raise SystemVerilogEmissionError(
                    "FPGA output delay has multiple state owners"
                )
            current = followers[0] if followers else None
        if chain != {r.name for r in module.registers} or len(
            module.next_assignments
        ) != len(chain):
            raise SystemVerilogEmissionError(
                "FPGA binding cannot discard unrelated architectural state"
            )
    lines.append(f"  assign {output} = {stored};")
    rtl = module_rendering._module(
        module, boundary._physical_port_declarations(module), lines
    )
    model = multiplier_models(family) + "\n" if simulation_model else ""
    return "`default_nettype none\n" + model + rtl + "`default_nettype wire\n"
