"""Bindable SVA projection of the shared structured verification predicates.

This compatibility artifact is intentionally not a second expression
lowering. Executable meaning is generated once by M35's ``FormalPredicate``
IR and rendered here through explicit semantic signal bindings.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from zlang.ir.formal import (
    PropertyKind,
    SignalBinding,
    generate_properties,
    render_bound_predicate,
    signal_bindings,
)
from zlang.ir.module import Module
from zlang.ir.top_abi import build_top_physical_abi
from zlang.ir.types import FixedType, SIntType
from zlang.backend.systemverilog.syntax import sized_decimal
from zlang.formal_domain import (
    FormalDomainRenderingError,
    formal_domain_applicability_reason,
    render_formal_domain,
)


class ContractEmissionError(ValueError):
    """A source verification goal cannot be represented by this SVA view."""


@dataclass(frozen=True)
class _Clause:
    name: str
    statement: str
    clock: str
    reset: str | None
    predicate: object
    signals: tuple[str, ...]


_IDENTIFIER = re.compile(r"[^A-Za-z0-9_$]+")


def _emit_constant(value: int, type_: object) -> str:
    """Compatibility spelling helper shared with the structured renderer.

    This is intentionally lexical only; source contracts no longer have an
    independent expression walker.
    """

    width = getattr(type_, "width", None)
    if not isinstance(width, int):
        raise ContractEmissionError("contract constant requires a fixed-width type")
    return sized_decimal(width, value, signed=isinstance(type_, (SIntType, FixedType)))


def _source_generated(value: str | None) -> bool:
    return bool(value) and (
        value.startswith("contract:")
        or value.startswith("verification-assert:")
        or value.startswith("verification-ensure:")
        or value.startswith("verification-cover:")
        or value.startswith("verification-requirement:")
    )


def _label(generated_from: str | None, property_id: str) -> str:
    source = (generated_from or property_id).split(":")[-1]
    token = _IDENTIFIER.sub("_", source).strip("_") or "goal"
    return "goal_" + token if token[0].isdigit() else token


def emit_contracts(module: Module) -> str:
    """Emit source-declared assumptions/assertions/covers as bindable SVA."""

    design = generate_properties(module)
    clauses: list[_Clause] = []
    for item in design.properties:
        if not _source_generated(item.generated_from):
            continue
        if item.predicate is None or item.non_executable_reason is not None:
            raise ContractEmissionError(
                f"verification goal '{item.id}' has no structured executable predicate"
            )
        clauses.append(_Clause(
            _label(item.generated_from, item.id),
            "assume" if item.kind is PropertyKind.ASSUMPTION else "assert",
            item.clock,
            item.reset_condition,
            item.predicate,
            item.relevant_signals,
        ))
    for item in design.covers:
        if not _source_generated(item.generated_from):
            continue
        if item.predicate is None or item.non_executable_reason is not None:
            raise ContractEmissionError(
                f"verification goal '{item.id}' has no structured executable predicate"
            )
        clauses.append(_Clause(
            _label(item.generated_from, item.id), "cover", item.clock,
            item.reset_condition, item.predicate, item.relevant_signals,
        ))
    if not clauses:
        return ""

    domains_by_clock = {item.clock: item for item in module.clock_domains}

    binding = {
        item.semantic_signal_id: item for item in signal_bindings(module)
    }
    required_ids: list[str] = []
    for clause in clauses:
        required_ids.extend(clause.signals)
    required = tuple(dict.fromkeys(required_ids))
    missing = tuple(item for item in required if item not in binding)
    if missing:
        raise ContractEmissionError(
            f"verification goal has no semantic binding for '{missing[0]}'"
        )

    # This compatibility artifact binds the selected public top directly.  A
    # packed semantic root is not a physical top port when the shared
    # TopPhysicalABI splits a struct/tuple into leaves or retains a vector as
    # an unpacked array.  Backend formal artifacts expose an explicit packed
    # observation port for those predicates; this backend-neutral sidecar must
    # fail closed instead of guessing a private core name or emitting an
    # invalid ``bind`` connection.
    public_leaves = build_top_physical_abi(module).leaves
    leaves_by_root: dict[str, list[object]] = {}
    for leaf in public_leaves:
        root = leaf.packed_root_semantic_id
        if root is not None:
            leaves_by_root.setdefault(root, []).append(leaf)
    for semantic_id in required:
        leaves = leaves_by_root.get(semantic_id)
        if leaves is None:
            continue
        binding_item = binding[semantic_id]
        exact_public = (
            len(leaves) == 1
            and leaves[0].leaf_semantic_id == semantic_id
            and leaves[0].external_name == binding_item.rtl_name
            and not leaves[0].array_dimensions
            and leaves[0].width == binding_item.width
        )
        if not exact_public:
            return (
                "// ZLang non-executable verification contract report\n"
                f"// observation={semantic_id}\n"
                "// reason=no exact packed public-top binding; use a connected "
                "verification bundle\n"
            )

    physical: list[SignalBinding] = []
    by_name: dict[str, SignalBinding] = {}
    for clause in clauses:
        for name in (clause.clock, clause.reset):
            if name is None or name in by_name:
                continue
            item = SignalBinding(
                f"verification-domain:{name}", module.name, name, 1,
                "input", clause.clock,
            )
            by_name[name] = item
            physical.append(item)
    for semantic_id in required:
        item = binding[semantic_id]
        previous = by_name.get(item.rtl_name)
        if previous is None:
            by_name[item.rtl_name] = item
            physical.append(item)
            continue
        if (
            previous.width, previous.direction, previous.clock_domain,
            previous.rtl_module,
        ) != (
            item.width, item.direction, item.clock_domain, item.rtl_module,
        ):
            raise ContractEmissionError(
                f"verification bindings reuse '{item.rtl_name}' incompatibly"
            )

    checker_name = f"{module.name}__zlang_contracts"
    declarations = ",\n".join(
        "    input logic"
        + ("" if item.width == 1 else f" [{item.width - 1}:0]")
        + f" {item.rtl_name}"
        for item in physical
    )
    rendered: list[str] = []
    used_labels: set[str] = set()
    used_names = set(by_name)
    domain_renderings: dict[tuple[str, str], object] = {}
    support_lines: list[str] = []
    for ordinal, clause in enumerate(clauses):
        label = clause.name
        if label in used_labels:
            label = f"{label}_{ordinal}"
        used_labels.add(label)
        domain = domains_by_clock.get(clause.clock)
        if domain is None:
            raise ContractEmissionError(
                f"verification goal clock '{clause.clock}' has no physical domain"
            )
        if clause.reset is not None and clause.reset != domain.reset:
            raise ContractEmissionError(
                f"verification goal reset '{clause.reset}' does not match "
                f"clock domain '{clause.clock}'"
            )
        try:
            unsupported_reason = formal_domain_applicability_reason(
                domain,
                domain_count=len(module.clock_domains),
            )
        except FormalDomainRenderingError as error:
            raise ContractEmissionError(str(error)) from error
        if unsupported_reason is not None:
            raise ContractEmissionError(unsupported_reason)
        key = (domain.clock, domain.reset)
        domain_rendering = domain_renderings.get(key)
        if domain_rendering is None:
            try:
                domain_rendering = render_formal_domain(
                    domain,
                    clock_name=domain.clock,
                    reset_name=domain.reset,
                    used_names=used_names,
                )
            except FormalDomainRenderingError as error:
                raise ContractEmissionError(str(error)) from error
            domain_renderings[key] = domain_rendering
            support_lines.extend(domain_rendering.support_lines)
        clause_bindings = binding
        reset_binding = binding.get("reset")
        if reset_binding is not None:
            clause_bindings = dict(binding)
            clause_bindings["reset"] = SignalBinding(
                reset_binding.semantic_signal_id,
                reset_binding.rtl_module,
                domain_rendering.reset_active,
                reset_binding.width,
                reset_binding.direction,
                reset_binding.clock_domain,
                reset_binding.source_origin,
            )
        try:
            expression = render_bound_predicate(
                clause.predicate, clause_bindings
            )
        except ValueError as error:
            raise ContractEmissionError(str(error)) from error
        disable = ""
        if clause.reset is not None:
            reset_expression = (
                f"{clause.reset} == 1'b1"
                if domain.is_legacy_default
                else domain_rendering.reset_active
            )
            disable = f" disable iff ({reset_expression})"
        rendered.append(
            f"  {label}: {clause.statement} property "
            f"(@({domain_rendering.sample_event}){disable} ({expression}));"
        )
    connections = ",\n".join(
        f"    .{item.rtl_name}({item.rtl_name})" for item in physical
    )
    return (
        "`default_nettype none\n"
        f"module {checker_name} (\n{declarations}\n);\n"
        + "".join(f"  {item}\n" for item in support_lines)
        + "\n".join(rendered)
        + "\nendmodule\n\n"
        f"bind {module.name} {checker_name} zlang_contracts (\n"
        f"{connections}\n);\n"
        "`default_nettype wire\n"
    )


__all__ = ["ContractEmissionError", "emit_contracts"]
