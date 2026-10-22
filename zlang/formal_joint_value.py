# SPDX-License-Identifier: Apache-2.0
"""Compiler-owned lossless joint-value adapter for scalar equivalence miters.

Both legs use the same exact leaf correspondence. The adapter does not prove
arithmetic or change production ABI; it builds private proof-only boundaries.
"""

from __future__ import annotations

from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version

from zlang.ir import expressions as e
from zlang.ir.module import Assignment, Port, PortDirection
from zlang.ir.joint_values import joint_value_boundary
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.target_mapping_identity import _semantic_mapping_identity


RTL_ARITHMETIC_PROOF_SCHEMA = "zlang-rtl-arithmetic-normalization-v2"


def validate_physical_joint_value(module, implementation):
    """Do not replace an actual DUT output with a caller's claimed expression."""
    from zlang.candidate_expression import candidate_input_refs
    if (
        len(module.assignments) != 1 or len(module.outputs) != 1
        or module.assignments[0].target != module.outputs[0]
        or module.outputs[0].type != implementation.type
        or expression_semantic_identity(module.assignments[0].expression)
        != expression_semantic_identity(implementation)
    ):
        raise ValueError("joint physical equivalence requires the exact actual output assignment")
    ports = {port.name: port.type for port in module.inputs}
    if any(ports.get(name) != type_ for name, type_ in candidate_input_refs(implementation).items()):
        raise ValueError("joint physical equivalence input binding/type mismatch")


def rtl_arithmetic_policy(implementation, solver):
    """Version optional word-level preparation without importing a solver."""
    resources = getattr(implementation, "resources", ())
    if (
        solver != "z3"
        or not resources
        or not all(
            dict(item.configuration).get("parallel_bilinear") == 1 for item in resources
        )
    ):
        return None
    try:
        binding_version = version("z3-solver")
    except PackageNotFoundError:
        return None  # Retain the existing raw SMT route, never a fake proof.
    return {
        "schema": RTL_ARITHMETIC_PROOF_SCHEMA,
        "z3_python": binding_version,
        "semantics": "QF_BV",
        "max_cells": 4096,
        "max_width": 128,
        "max_roots": 32,
        "max_nodes": 4096,
        "max_lemmas": 64,
        "max_leaves": 16,
        "max_degree": 2,
        "max_cone_depth": 128,
        "max_rewrite_depth": 256,
    }


def flatten_joint_equivalence(reference, implementation, module=None, graph=None):
    boundary = joint_value_boundary(reference)
    if boundary is None:
        raise ValueError("joint equivalence requires a bounded signed struct value")

    def pack(value):
        if isinstance(value, e.Pipeline):
            return replace(
                value,
                expression=pack(value.expression),
                type=boundary.packed.type,
                pipeline_plan=None,
            )
        if not isinstance(value, e.StructConstruct) or value.type != reference.type:
            raise ValueError(
                "joint equivalence implementation changed the aggregate type"
            )
        scalar = boundary.scalarize(value)
        return replace(
            boundary.packed,
            operands=tuple(
                replace(component, expression=item)
                for component, (_, item) in zip(
                    boundary.packed.operands, scalar.fields, strict=True
                )
            ),
        )

    if module is not None:
        validate_physical_joint_value(module, implementation)
    implementation = pack(implementation)
    if module is not None:
        if len(module.assignments) != 1 or len(module.outputs) != 1:
            raise ValueError("joint physical equivalence requires one joint output")
        output = replace(module.outputs[0], type=boundary.packed.type)
        inputs = tuple(
            Port(PortDirection.INPUT, name, value.type)
            for name, value in boundary.bindings
        )
        module = replace(
            module,
            ports=(*inputs, output),
            assignments=(Assignment(output, implementation),),
            functions=(),
            callable_definitions=(),
            pipeline_explorations=(),
        )
        nodes = tuple(
            replace(
                node,
                semantic_mappings=tuple(
                    replace(
                        mapping,
                        expression=boundary.scalarize(mapping.expression),
                        semantic_identity=_semantic_mapping_identity(
                            boundary.scalarize(mapping.expression)
                        ),
                    )
                    if mapping.expression is not None
                    else mapping
                    for mapping in node.semantic_mappings
                ),
            )
            for node in graph.resources
        )
        selected = (
            implementation.expression
            if isinstance(implementation, e.Pipeline)
            else implementation
        )
        # The production schedule is keyed to production field semantics.
        # This private proof boundary has scalarized input identities instead.
        graph = replace(
            graph, resources=nodes, quantization=selected, scheduled_value_graph=None
        )
    return boundary.packed, implementation, module, graph
