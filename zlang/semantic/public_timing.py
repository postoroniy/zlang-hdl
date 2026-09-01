"""Exact public module timing derivation over already-typed semantic IR.

This module owns the bounded, backend-independent timing-contract analysis.
The main semantic analyzer supplies fully resolved ports, assignments, locals,
and hierarchy; no parsing, name resolution, or type inference occurs here.
"""

from __future__ import annotations

from zlang.ast import nodes as ast
from zlang.ir import cdc as ir_cdc
from zlang.ir import csr as ir_csr
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir import timing as ir_timing
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.types import StructType, TupleType, VecType
from zlang.source import SourceOrigin

from .errors import SemanticError


def _join_timings(
    description: str,
    timings: tuple[ir_timing.ValueTiming, ...],
) -> ir_timing.ValueTiming:
    """Join exact public value timing without inventing alignment storage."""

    unknown = next(
        (
            timing
            for timing in timings
            if timing.knowledge is ir_timing.TimingKnowledge.UNKNOWN
        ),
        None,
    )
    if unknown is not None:
        return unknown
    known = {
        timing.latency
        for timing in timings
        if timing.knowledge is ir_timing.TimingKnowledge.KNOWN
    }
    if len(known) > 1:
        rendered = ", ".join(str(latency) for latency in sorted(known))
        raise SemanticError(
            f"latency mismatch in {description}: operands have latencies "
            f"{rendered}",
            code="ZL-TIMING-MISMATCH",
            fixes=("align operands explicitly before combining them",),
        )
    if known:
        latency = next(iter(known))
        assert latency is not None
        return ir_timing.ValueTiming.known(latency)
    return ir_timing.ValueTiming.timeless()


def _advance_timing(
    timing: ir_timing.ValueTiming,
    cycles: int,
) -> ir_timing.ValueTiming:
    if timing.knowledge is ir_timing.TimingKnowledge.UNKNOWN:
        return timing
    if cycles == 0:
        return timing
    if timing.knowledge is ir_timing.TimingKnowledge.TIMELESS:
        return ir_timing.ValueTiming.known(cycles)
    assert timing.latency is not None
    return ir_timing.ValueTiming.known(timing.latency + cycles)


def _expression_timing(
    expression: ir_expr.Expression,
    *,
    local_expressions: dict[str, ir_expr.Expression],
    instance_outputs: dict[tuple[str, str], ir_timing.ValueTiming],
    active_locals: tuple[str, ...] = (),
) -> ir_timing.ValueTiming:
    """Derive frozen timeless/known/unknown timing from already-typed IR."""

    recurse = lambda value: _expression_timing(
        value,
        local_expressions=local_expressions,
        instance_outputs=instance_outputs,
        active_locals=active_locals,
    )

    if isinstance(expression, (ir_expr.Constant, ir_expr.ParameterRef)):
        return ir_timing.ValueTiming.timeless()
    if isinstance(expression, ir_expr.InputRef):
        local = local_expressions.get(expression.name)
        if local is None:
            return ir_timing.ValueTiming.known(0)
        if expression.name in active_locals:
            return ir_timing.ValueTiming.unknown(
                f"cyclic local timing dependency through '{expression.name}'"
            )
        return _expression_timing(
            local,
            local_expressions=local_expressions,
            instance_outputs=instance_outputs,
            active_locals=(*active_locals, expression.name),
        )
    if isinstance(expression, ir_expr.InstanceOutputRef):
        return instance_outputs.get(
            (expression.instance, expression.port),
            ir_timing.ValueTiming.unknown(
                f"instance output '{expression.instance}.{expression.port}' "
                "has no exact derived timing"
            ),
        )
    if isinstance(expression, ir_expr.RegisterRef):
        return ir_timing.ValueTiming.unknown(
            f"register '{expression.name}' is state-dependent"
        )
    if isinstance(
        expression,
        (
            ir_expr.ReadyValidRef,
            ir_expr.CreditRef,
            ir_expr.PacketRef,
            ir_expr.VirtualChannelCreditRef,
            ir_expr.RequestResponseRef,
        ),
    ):
        return ir_timing.ValueTiming.unknown(
            "protocol value timing is outside the exact scalar module contract"
        )
    if isinstance(expression, ir_expr.FifoRef):
        return ir_timing.ValueTiming.unknown(
            f"FIFO '{expression.fifo}' is state-dependent"
        )
    if isinstance(expression, ir_expr.MemoryRef):
        return ir_timing.ValueTiming.unknown(
            f"memory '{expression.memory}' is state-dependent"
        )
    if isinstance(expression, ir_expr.RomRef):
        return ir_timing.ValueTiming.unknown(
            f"ROM '{expression.rom}' is state-dependent"
        )
    if isinstance(expression, (ir_expr.Add, ir_expr.Binary)):
        description = "+" if isinstance(expression, ir_expr.Add) else expression.operator.value
        return _join_timings(
            description,
            (recurse(expression.left), recurse(expression.right)),
        )
    if isinstance(expression, (ir_expr.EnumEncode, ir_expr.EnumValid)):
        return recurse(expression.expression)
    if isinstance(expression, ir_expr.EnumDecode):
        return _join_timings(
            "enum decode",
            (recurse(expression.expression), recurse(expression.fallback)),
        )
    if isinstance(
        expression,
        (
            ir_expr.Extend,
            ir_expr.Truncate,
            ir_expr.FixedConvert,
            ir_expr.FieldAccess,
            ir_expr.VectorIndex,
            ir_expr.Slice,
            ir_expr.Bitcast,
            ir_expr.Reshape,
            ir_expr.Pack,
            ir_expr.Unpack,
        ),
    ):
        return recurse(expression.expression)
    if isinstance(expression, (ir_expr.Concat, ir_expr.VectorConcat)):
        return _join_timings(
            "concat", tuple(recurse(value) for value in expression.operands)
        )
    if isinstance(expression, ir_expr.RuntimeIndex):
        return _join_timings(
            "runtime index",
            (recurse(expression.expression), recurse(expression.index)),
        )
    if isinstance(expression, ir_expr.VectorUpdate):
        return _join_timings(
            "vector update",
            (
                recurse(expression.expression),
                recurse(expression.index),
                recurse(expression.value),
            ),
        )
    if isinstance(expression, ir_expr.Delay):
        return _advance_timing(recurse(expression.expression), expression.cycles)
    if isinstance(expression, ir_expr.Pipeline):
        return _advance_timing(recurse(expression.expression), expression.stages)
    if isinstance(expression, ir_expr.Mux):
        return _join_timings(
            "mux",
            (
                recurse(expression.condition),
                recurse(expression.when_true),
                recurse(expression.when_false),
            ),
        )
    if isinstance(expression, ir_expr.Switch):
        return _join_timings(
            "switch",
            (
                recurse(expression.selector),
                *(recurse(case.expression) for case in expression.cases),
                recurse(expression.default),
            ),
        )
    if isinstance(expression, ir_expr.Call):
        return _join_timings(
            f"call to {expression.function}",
            tuple(recurse(argument) for argument in expression.arguments),
        )
    if isinstance(expression, ir_expr.StructConstruct):
        return _join_timings(
            "struct construction",
            tuple(recurse(value) for _, value in expression.fields),
        )
    if isinstance(expression, ir_expr.TupleConstruct):
        return _join_timings(
            "tuple construction",
            tuple(recurse(value) for value in expression.elements),
        )
    if isinstance(expression, ir_expr.TupleProject):
        return recurse(expression.expression)
    if isinstance(expression, (ir_expr.Generate, ir_expr.Map)):
        return _join_timings(
            type(expression).__name__.lower(),
            tuple(recurse(element) for element in expression.elements),
        )
    if isinstance(expression, ir_expr.FunctionalRegion):
        retained = (
            *(value for table in expression.tables for value in table.values),
            *(value for _, value in expression.captures),
        )
        return _join_timings(
            f"functional {expression.kind.value}",
            tuple(recurse(value) for value in retained),
        )
    if isinstance(expression, ir_expr.Dot):
        return _join_timings(
            "dot", tuple(recurse(product) for product in expression.products)
        )
    if isinstance(expression, ir_expr.Reduce):
        return recurse(expression.collection)
    if isinstance(expression, ir_expr.ImplementationChoice):
        alternatives = (
            (expression.selected_alternative,)
            if expression.selected is not None
            else expression.alternatives
        )
        return _join_timings(
            "implementation choice",
            tuple(recurse(alternative.expression) for alternative in alternatives),
        )
    return ir_timing.ValueTiming.unknown(
        f"unsupported typed expression {type(expression).__name__}"
    )


def _derive_instance_output_timings(
    *,
    elaborated_instances: tuple[ir_module.ElaboratedInstance, ...],
    child_irs: dict[str, ir_module.Module],
    bindings: tuple[ir_module.InstancePortBinding, ...],
    local_expressions: dict[str, ir_expr.Expression],
) -> tuple[ir_timing.InstanceOutputTiming, ...]:
    result: list[ir_timing.InstanceOutputTiming] = []
    known_outputs: dict[tuple[str, str], ir_timing.ValueTiming] = {}
    binding_by_port = {
        (binding.instance, binding.port): binding.expression for binding in bindings
    }

    for elaborated in elaborated_instances:
        instance_name = elaborated.instance.name
        child = child_irs[instance_name]
        child_output_timings = {
            item.port: item.timing for item in child.output_timings
        }
        for output in child.outputs:
            if output.protocol is not InterfaceProtocol.WIRE:
                continue
            if child.timing_contract is None:
                timing = ir_timing.ValueTiming.unknown(
                    f"child module '{child.name}' has no timing contract"
                )
            else:
                relative = child_output_timings.get(
                    output.name,
                    ir_timing.ValueTiming.unknown(
                        f"child output '{child.name}.{output.name}' has no "
                        "derived timing"
                    ),
                )
                if relative.knowledge in {
                    ir_timing.TimingKnowledge.TIMELESS,
                    ir_timing.TimingKnowledge.UNKNOWN,
                }:
                    timing = relative
                else:
                    input_timings: list[ir_timing.ValueTiming] = []
                    for child_input in child.inputs:
                        if child_input.protocol is not InterfaceProtocol.WIRE:
                            continue
                        bound = binding_by_port.get((instance_name, child_input.name))
                        if bound is None:
                            input_timings.append(
                                ir_timing.ValueTiming.unknown(
                                    f"instance input '{instance_name}."
                                    f"{child_input.name}' is unbound"
                                )
                            )
                            continue
                        input_timings.append(
                            _expression_timing(
                                bound,
                                local_expressions=local_expressions,
                                instance_outputs=known_outputs,
                            )
                        )
                    base = _join_timings(
                        f"inputs bound to instance '{instance_name}'",
                        tuple(input_timings),
                    )
                    if base.knowledge is ir_timing.TimingKnowledge.UNKNOWN:
                        timing = base
                    else:
                        base_latency = (
                            base.latency
                            if base.knowledge is ir_timing.TimingKnowledge.KNOWN
                            else 0
                        )
                        assert base_latency is not None
                        timing = ir_timing.ValueTiming.known(
                            base_latency + child.timing_contract.latency
                        )
            known_outputs[(instance_name, output.name)] = timing
            result.append(
                ir_timing.InstanceOutputTiming(instance_name, output.name, timing)
            )
    return tuple(result)


def analyze_public_module_timing(
    source_module: ast.Module,
    *,
    ports: tuple[ir_module.Port, ...],
    assignments: tuple[ir_module.Assignment, ...],
    locals_: tuple[ir_module.LocalValue, ...],
    clock_domains: tuple[ir_cdc.ClockDomain, ...],
    child_irs: dict[str, ir_module.Module],
    elaborated_instances: tuple[ir_module.ElaboratedInstance, ...],
    instance_bindings: tuple[ir_module.InstancePortBinding, ...],
    request_responses: tuple[ir_module.RequestResponseInterface, ...],
    aggregate_protocol_endpoints: tuple[ir_module.AggregateProtocolEndpoint, ...],
    csr_blocks: tuple[ir_csr.CsrBlock, ...],
    source_unit: str | None,
    source_digest: str | None,
) -> tuple[
    ir_timing.ModuleTimingContract | None,
    tuple[ir_timing.OutputTiming, ...],
    tuple[ir_timing.InstanceOutputTiming, ...],
]:
    """Validate and derive an explicit public module timing contract."""

    declaration = source_module.timing
    if declaration is None:
        return None, (), ()

    local_expressions = {item.name: item.expression for item in locals_}
    instance_timings = _derive_instance_output_timings(
        elaborated_instances=elaborated_instances,
        child_irs=child_irs,
        bindings=instance_bindings,
        local_expressions=local_expressions,
    )
    instance_timing_map = {
        (item.instance, item.port): item.timing for item in instance_timings
    }
    scalar_outputs = tuple(
        port
        for port in ports
        if port.direction is ir_module.PortDirection.OUTPUT
        and port.protocol is InterfaceProtocol.WIRE
        and not isinstance(port.type, (StructType, TupleType, VecType))
    )
    assignment_by_output = {
        assignment.target.name: assignment.expression
        for assignment in assignments
        if isinstance(assignment.target, ir_module.Port)
        and assignment.target.direction is ir_module.PortDirection.OUTPUT
        and assignment.target.protocol is InterfaceProtocol.WIRE
        and assignment.signal is None
        and assignment.channel is None
    }
    output_timings = tuple(
        ir_timing.OutputTiming(
            output.name,
            _expression_timing(
                assignment_by_output[output.name],
                local_expressions=local_expressions,
                instance_outputs=instance_timing_map,
            )
            if output.name in assignment_by_output
            else ir_timing.ValueTiming.unknown(
                f"output '{output.name}' is driven by state or rules"
            ),
        )
        for output in scalar_outputs
    )

    if declaration.initiation_interval != 1:
        raise SemanticError("module timing contracts currently require ii 1")
    if len(clock_domains) > 1:
        raise SemanticError(
            "module timing contracts currently require at most one clock/reset domain"
        )
    if declaration.latency > 0 and len(clock_domains) != 1:
        raise SemanticError(
            "a positive module timing latency requires exactly one clock/reset domain"
        )
    if (
        request_responses
        or aggregate_protocol_endpoints
        or any(port.protocol is not InterfaceProtocol.WIRE for port in ports)
    ):
        raise SemanticError(
            "module timing contracts currently support only scalar wire ports, "
            "not protocols"
        )
    aggregate_output = next(
        (
            port
            for port in ports
            if port.direction is ir_module.PortDirection.OUTPUT
            and isinstance(port.type, (StructType, TupleType, VecType))
        ),
        None,
    )
    if aggregate_output is not None:
        raise SemanticError(
            f"module timing contract output '{aggregate_output.name}' must be "
            "a scalar wire value"
        )
    if csr_blocks:
        raise SemanticError(
            "module timing contracts do not cover stateful CSR behavior"
        )
    if not scalar_outputs:
        raise SemanticError(
            "module timing contract requires at least one scalar wire output"
        )

    domain = clock_domains[0] if clock_domains else None
    source_origin = (
        SourceOrigin(
            declaration.origin,
            f"module timing contract {source_module.name}",
            source_unit,
            source_digest,
        )
        if declaration.origin is not None
        else None
    )
    contract = ir_timing.ModuleTimingContract(
        declaration.latency,
        declaration.initiation_interval,
        domain.clock if domain is not None else None,
        domain.reset if domain is not None else None,
        source_origin,
    )
    for output in output_timings:
        if output.timing.knowledge is ir_timing.TimingKnowledge.UNKNOWN:
            raise SemanticError(
                f"output '{output.port}' has unknown timing: "
                f"{output.timing.reason}"
            )
        if (
            output.timing.knowledge is ir_timing.TimingKnowledge.KNOWN
            and output.timing.latency != contract.latency
        ):
            raise SemanticError(
                f"output '{output.port}' has exact latency "
                f"{output.timing.latency}, but module timing contract declares "
                f"latency {contract.latency}",
                code="ZL-TIMING-CONTRACT",
                fixes=("make the declared and derived exact latencies equal",),
            )
    return contract, output_timings, instance_timings


__all__ = ["analyze_public_module_timing"]
