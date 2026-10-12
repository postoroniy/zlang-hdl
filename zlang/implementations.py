"""Inspection and explicit selection of typed implementation alternatives."""

from __future__ import annotations

from dataclasses import replace

from zlang.ir import expressions as expr
from zlang.ir.module import Module


class ImplementationSelectionError(ValueError):
    """A named output or implementation alternative cannot be selected."""


def select_implementation(
    module: Module,
    output: str,
    kind: expr.ImplementationKind | str,
) -> Module:
    """Select one already validated alternative without applying a cost model."""

    try:
        selected = (
            kind
            if isinstance(kind, expr.ImplementationKind)
            else expr.ImplementationKind(kind)
        )
    except ValueError as error:
        raise ImplementationSelectionError(
            f"unknown implementation kind '{kind}'"
        ) from error
    matches = tuple(
        (index, assignment)
        for index, assignment in enumerate(module.assignments)
        if assignment.target.name == output
        and assignment.signal is None
        and assignment.channel is None
        and isinstance(assignment.expression, expr.ImplementationChoice)
    )
    if len(matches) != 1:
        raise ImplementationSelectionError(
            f"output '{output}' does not have one implementation choice"
        )
    index, assignment = matches[0]
    choice = assignment.expression
    assert isinstance(choice, expr.ImplementationChoice)
    if selected not in {alternative.kind for alternative in choice.alternatives}:
        raise ImplementationSelectionError(
            f"output '{output}' has no '{selected.value}' alternative"
        )
    assignments = list(module.assignments)
    assignments[index] = replace(
        assignment,
        expression=replace(
            choice,
            selected=selected,
            alternatives=tuple(
                replace(alternative, estimate=None, measurement=None)
                for alternative in choice.alternatives
            ),
            cost_policy=None,
        ),
    )
    return replace(module, assignments=tuple(assignments))


def render_implementation_report(module: Module) -> str:
    """Render applicability and observable semantics for every source choice."""

    choices = tuple(
        (assignment.target.name, assignment.expression)
        for assignment in module.assignments
        if assignment.signal is None
        and assignment.channel is None
        and isinstance(assignment.expression, expr.ImplementationChoice)
    )
    if not choices:
        return ""
    lines = [f"module {module.name}"]
    for output, choice in choices:
        equivalences = ",".join(
            item.value for item in choice.proven_equivalences
        )
        selected = (
            choice.selected_alternative
            if choice.selected is not None
            else choice.alternatives[0]
        )
        selected_name = (
            choice.selected.value
            if choice.selected is not None
            else "unresolved"
        )
        lines.extend(
            (
                f"choice output={output} selected={selected_name}",
                f"  proven_equivalence={equivalences}",
                f"  observable result={choice.type} "
                f"latency={selected.semantics.latency} "
                f"initiation_interval={selected.semantics.initiation_interval} "
                "protocol_events=none",
            )
        )
        for alternative in choice.alternatives:
            applicability = alternative.applicability
            semantics = alternative.semantics
            conditions = ",".join(applicability.conditions)
            estimate = alternative.estimate
            estimate_text = (
                " estimated_lut=" + str(estimate.lut)
                + " estimated_ff=" + str(estimate.ff)
                + " estimated_dsp=" + str(estimate.dsp)
                + " estimated_bram=" + str(estimate.bram)
                if estimate is not None
                else ""
            )
            measurement = alternative.measurement
            measurement_text = (
                " measured_yosys_lut_cells=" + str(measurement.lut_cells)
                + " measured_yosys_flip_flops=" + str(measurement.flip_flops)
                + " measured_yosys_total_cells=" + str(measurement.total_cells)
                + " measured_yosys_logic_depth=" + str(measurement.logic_depth)
                if measurement is not None
                else ""
            )
            lines.append(
                f"  alternative kind={alternative.kind.value} "
                f"operation={applicability.operation} "
                f"multiply={applicability.multiplier_left_type}*"
                f"{applicability.multiplier_right_type} "
                f"addend={applicability.addend_type} "
                f"result={applicability.result_type} "
                f"resource_hint={applicability.resource_hint.value} "
                f"latency={semantics.latency} "
                f"initiation_interval={semantics.initiation_interval} "
                f"conditions=[{conditions}]"
                f"{estimate_text}"
                f"{measurement_text}"
            )
        if choice.cost_policy is None:
            lines.append(
                f"  selection source_explicit={selected_name} "
                "cost_source=none measured=false"
            )
        else:
            constraints = ",".join(
                f"{constraint.metric.value}<={constraint.maximum}"
                for constraint in choice.cost_policy.constraints
            )
            lines.append(
                f"  selection source_explicit=false selected={selected_name} "
                f"goal=minimize_{choice.cost_policy.goal.value} "
                f"constraints=[{constraints}] "
                f"feedback={choice.cost_policy.feedback.value if choice.cost_policy.feedback else 'none'} "
                f"cost_source={'measured_yosys' if any(item.measurement for item in choice.alternatives) else 'estimate'} "
                f"measured={str(any(item.measurement for item in choice.alternatives)).lower()}"
            )
    return "\n".join(lines) + "\n"
