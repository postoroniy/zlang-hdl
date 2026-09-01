"""Bounded equality saturation for pure combinational canonical expressions.

This deliberately small Milestone 18 experiment keeps one explicit mathematical
equivalence class as a deterministic set of typed expression terms.  It does not
select a preferred implementation or admit state, protocol, or architecture
nodes; those boundaries belong to later milestones.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import ast
import json
from itertools import product
from typing import Any

from zlang.ir.expressions import BinaryOperator, Expression
from zlang.ir.module import (
    EquivalenceGuardKind,
    EquivalenceGuardPredicate,
    EquivalenceRule,
)
from zlang.ir.types import BitType, BitsType, HardwareType, SIntType, UIntType
from zlang.source import SourceOrigin
from zlang.opt.ir import (
    CanonicalExpression,
    CanonicalModule,
    EquivalenceMode,
    ExpressionOp,
    NodeCategory,
    NodeId,
    Observation,
    Purity,
    equivalence_definition,
    pure_metadata,
)
from zlang.ir.callables import CallableExpansionError, expand_callable_calls
from zlang.opt.egraph import (
    EGraphAdapterError,
    canonical_nodes_to_egraph,
    egraph_to_canonical,
)
from zlang.opt.lowering import lower_expression_graph, restore, restore_expression


class SaturationError(ValueError):
    """A root or requested equality-saturation mode is not eligible."""


class RewriteRule(str, Enum):
    # Retained as historical M18 report names; M26 never emits these rules.
    ADD_ZERO = "add_zero"
    SUBTRACT_ZERO = "subtract_zero"
    MULTIPLY_ZERO = "multiply_zero"
    MULTIPLY_ONE = "multiply_one"
    BIT_OR_ZERO = "bit_or_zero"
    BIT_XOR_ZERO = "bit_xor_zero"
    SHIFT_ZERO = "shift_zero"
    MUX_IDENTITY = "mux_identity"
    MUX_CONSTANT = "mux_constant"
    RESIZE_IDENTITY = "resize_identity"
    MULTIPLY_POWER_OF_TWO = "multiply_power_of_two"


@dataclass(frozen=True)
class Term:
    """A tree-shaped member of one typed equality class."""

    category: NodeCategory
    op: ExpressionOp
    type: HardwareType
    operands: tuple[Term, ...] = ()
    attributes: tuple[tuple[str, object], ...] = ()
    origins: tuple[SourceOrigin, ...] = field(default=(), compare=False, hash=False)

    def attribute(self, name: str) -> object:
        for key, value in self.attributes:
            if key == name:
                return value
        raise KeyError(name)


@dataclass(frozen=True)
class EquivalenceClass:
    id: int
    mode: EquivalenceMode
    type: HardwareType
    terms: tuple[Term, ...]

    def __post_init__(self) -> None:
        if any(term.type != self.type for term in self.terms):
            raise ValueError("every equality-class term must have the same exact type")


@dataclass(frozen=True)
class RewriteRegistration:
    """Deterministic provenance and execution status for one engine rule."""

    identity: str
    rule: RewriteRule
    direction: str
    provenance: tuple[str, ...]
    guards: tuple[str, ...]
    enabled: bool
    fired: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class SaturationResult:
    root: NodeId
    equivalence_class: EquivalenceClass
    observations: tuple[Observation, ...]
    rules: tuple[RewriteRule, ...]
    iterations: int
    max_iterations: int
    max_terms: int
    saturated: bool
    truncated: bool
    registrations: tuple[RewriteRegistration, ...] = ()
    rejection_reasons: tuple[str, ...] = ()
    eclass_count: int = 1

    @property
    def original(self) -> Term:
        return self.equivalence_class.terms[0]

    @property
    def alternatives(self) -> tuple[Term, ...]:
        return self.equivalence_class.terms[1:]


def _legacy_saturate(
    module: CanonicalModule,
    root: NodeId,
    *,
    mode: EquivalenceMode = EquivalenceMode.MATHEMATICAL,
    max_iterations: int = 8,
    max_terms: int = 256,
) -> SaturationResult:
    """Saturate one pure value root under a bounded set of typed equalities."""

    if mode is not EquivalenceMode.MATHEMATICAL:
        raise SaturationError(
            "Milestone 18 saturation supports only mathematical equivalence, "
            f"got {mode.value}"
        )
    if max_iterations < 1:
        raise SaturationError("max_iterations must be at least 1")
    if max_terms < 1:
        raise SaturationError("max_terms must be at least 1")
    _require_pure_value_root(module, root)

    original = _term_from_root(module.expressions, root)
    known: dict[str, Term] = {render_term(original): original}
    applied: set[RewriteRule] = set()
    iterations = 0
    saturated = False
    truncated = False

    for iteration in range(1, max_iterations + 1):
        iterations = iteration
        additions: list[tuple[RewriteRule, str, Term]] = []
        for term in sorted(known.values(), key=render_term):
            for rule, candidate in _rewrite_anywhere(term):
                if candidate.type != original.type:
                    continue
                rendered = render_term(candidate)
                if rendered not in known:
                    additions.append((rule, rendered, candidate))
        if not additions:
            saturated = True
            break
        for rule, rendered, candidate in sorted(
            additions,
            key=lambda item: (item[0].value, item[1]),
        ):
            if rendered in known:
                continue
            if len(known) >= max_terms:
                truncated = True
                break
            known[rendered] = candidate
            applied.add(rule)
        if truncated:
            break

    alternatives = tuple(
        term
        for term in sorted(known.values(), key=render_term)
        if term != original
    )
    equality_class = EquivalenceClass(
        0,
        mode,
        original.type,
        (original, *alternatives),
    )
    definition = equivalence_definition(mode)
    return SaturationResult(
        root,
        equality_class,
        definition.observations,
        tuple(sorted(applied, key=lambda rule: rule.value)),
        iterations,
        max_iterations,
        max_terms,
        saturated,
        truncated,
    )


def term_to_expression(term: Term) -> Expression:
    """Materialize one equality-class member as typed semantic expression IR."""

    nodes: list[CanonicalExpression] = []
    interned: dict[Term, NodeId] = {}

    def lower(item: Term) -> NodeId:
        if item in interned:
            return interned[item]
        operands = tuple(lower(operand) for operand in item.operands)
        node_id = len(nodes)
        nodes.append(
            CanonicalExpression(
                id=node_id,
                category=item.category,
                op=item.op,
                type=item.type,
                operands=operands,
                attributes=item.attributes,
                metadata=pure_metadata(item.type),
            )
        )
        interned[item] = node_id
        return node_id

    root = lower(term)
    return restore_expression(tuple(nodes), root)


def render_saturation(result: SaturationResult) -> str:
    """Render a stable, inspectable equality-saturation report."""

    observations = ",".join(item.value for item in result.observations)
    rules = ",".join(rule.value for rule in result.rules) or "none"
    terms = result.equivalence_class.terms
    lines = [
        f"saturation root=%{result.root}",
        f"equivalence {result.equivalence_class.mode.value} "
        f"observations={observations}",
        f"bounds max_iterations={result.max_iterations} max_terms={result.max_terms}",
        f"result saturated={str(result.saturated).lower()} "
        f"truncated={str(result.truncated).lower()} "
        f"iterations={result.iterations} eclasses={result.eclass_count} "
        f"candidates={len(terms)}",
        f"rules {rules}",
    ]
    lines.extend(
        "rewrite "
        f"{registration.identity} enabled={str(registration.enabled).lower()} "
        f"fired={str(registration.fired).lower()} "
        f"direction={registration.direction} "
        f"provenance={','.join(registration.provenance)} "
        f"guards={','.join(registration.guards) or 'none'}"
        + (f" reason={registration.reason}" if registration.reason else "")
        for registration in result.registrations
    )
    lines.append(
        "rejections "
        + ("; ".join(result.rejection_reasons) if result.rejection_reasons else "none")
    )
    lines.extend([
        f"eclass {result.equivalence_class.id} type={result.equivalence_class.type}",
        f"  original {render_term(terms[0])}",
    ])
    lines.extend(
        f"  alternative {index} {render_term(term)}"
        for index, term in enumerate(terms[1:], 1)
    )
    return "\n".join(lines) + "\n"


def render_term(term: Term) -> str:
    attributes = " ".join(
        f"{name}={_render_value(value)}" for name, value in term.attributes
    )
    operands = " ".join(render_term(operand) for operand in term.operands)
    contents = " ".join(item for item in (attributes, operands) if item)
    head = f"{term.op.value}:{term.type}"
    return f"({head}{(' ' + contents) if contents else ''})"


def _require_pure_value_root(module: CanonicalModule, root: NodeId) -> None:
    if root < 0 or root >= len(module.expressions):
        raise SaturationError(f"canonical expression root %{root} does not exist")
    visited: set[NodeId] = set()

    def visit(node_id: NodeId) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        node = module.expressions[node_id]
        metadata = node.metadata
        if node.category is not NodeCategory.VALUE or metadata.purity is not Purity.PURE:
            effects = ",".join(effect.value for effect in metadata.effects) or "none"
            raise SaturationError(
                f"root %{root} is not a pure mathematical value: dependency "
                f"%{node.id} is {node.category.value}.{node.op.value} "
                f"purity={metadata.purity.value} effects=[{effects}]"
            )
        operand_types = tuple(module.expressions[item].type for item in node.operands)
        if node.op is ExpressionOp.BINARY:
            operator = node.attribute("operator")
            if operator in {BinaryOperator.SHIFT_LEFT, BinaryOperator.SHIFT_RIGHT}:
                compatible = bool(operand_types) and operand_types[0] == node.type
            elif operator in {
                BinaryOperator.BIT_AND,
                BinaryOperator.BIT_OR,
                BinaryOperator.BIT_XOR,
            }:
                compatible = all(item == node.type for item in operand_types)
            else:
                compatible = True
            if not compatible:
                raise SaturationError(
                    f"root %{root} has incompatible operand types for {operator.value}"
                )
        elif node.op is ExpressionOp.MUX:
            compatible = (
                len(operand_types) == 3
                and isinstance(operand_types[0], BitType)
                and operand_types[1] == node.type
                and operand_types[2] == node.type
            )
            if not compatible:
                raise SaturationError(f"root %{root} has incompatible mux operand types")
        for operand in node.operands:
            visit(operand)

    visit(root)


def _term_from_root(
    nodes: tuple[CanonicalExpression, ...],
    root: NodeId,
) -> Term:
    cache: dict[NodeId, Term] = {}

    def build(node_id: NodeId) -> Term:
        if node_id in cache:
            return cache[node_id]
        node = nodes[node_id]
        term = Term(
            node.category,
            node.op,
            node.type,
            tuple(build(operand) for operand in node.operands),
            node.attributes,
            node.origins,
        )
        cache[node_id] = term
        return term

    return build(root)


def _rewrite_anywhere(term: Term) -> tuple[tuple[RewriteRule, Term], ...]:
    rewritten = list(_rewrite_root(term))
    for index, operand in enumerate(term.operands):
        for rule, replacement in _rewrite_anywhere(operand):
            operands = list(term.operands)
            operands[index] = replacement
            rewritten.append((rule, replace(term, operands=tuple(operands))))
    unique: dict[tuple[RewriteRule, str], Term] = {}
    for rule, candidate in rewritten:
        if candidate.type == term.type:
            unique[(rule, render_term(candidate))] = candidate
    return tuple(
        (rule, unique[(rule, rendered)])
        for rule, rendered in sorted(
            unique,
            key=lambda item: (item[0].value, item[1]),
        )
    )


def _rewrite_root(term: Term) -> tuple[tuple[RewriteRule, Term], ...]:
    if len(term.operands) != 2:
        return ()
    left, right = term.operands
    candidates: list[tuple[RewriteRule, Term]] = []

    if term.op is ExpressionOp.ADD:
        if _is_zero(right) and (replacement := _resize_exact(left, term.type)):
            candidates.append((RewriteRule.ADD_ZERO, replacement))
        if _is_zero(left) and (replacement := _resize_exact(right, term.type)):
            candidates.append((RewriteRule.ADD_ZERO, replacement))

    if term.op is not ExpressionOp.BINARY:
        return tuple(candidates)
    operator = term.attribute("operator")
    if operator is BinaryOperator.SUBTRACT and _is_zero(right):
        if replacement := _resize_exact(left, term.type):
            candidates.append((RewriteRule.SUBTRACT_ZERO, replacement))
    elif operator is BinaryOperator.MULTIPLY:
        for constant, value in ((left, right), (right, left)):
            if _is_zero(constant):
                candidates.append((RewriteRule.MULTIPLY_ZERO, _constant(0, term.type)))
            if _is_one(constant) and (
                replacement := _resize_exact(value, term.type)
            ):
                candidates.append((RewriteRule.MULTIPLY_ONE, replacement))
            shift = _power_of_two_shift(constant)
            if shift is not None and shift > 0:
                extended = _resize_exact(value, term.type)
                if extended is not None:
                    amount_type = UIntType(max(1, shift.bit_length()))
                    candidates.append(
                        (
                            RewriteRule.MULTIPLY_POWER_OF_TWO,
                            Term(
                                NodeCategory.VALUE,
                                ExpressionOp.BINARY,
                                term.type,
                                (extended, _constant(shift, amount_type)),
                                (
                                    ("operator", BinaryOperator.SHIFT_LEFT),
                                    ("operand_type", term.type),
                                ),
                            ),
                        )
                    )
    elif operator in {BinaryOperator.BIT_OR, BinaryOperator.BIT_XOR}:
        rule = (
            RewriteRule.BIT_OR_ZERO
            if operator is BinaryOperator.BIT_OR
            else RewriteRule.BIT_XOR_ZERO
        )
        if _is_zero(right) and (replacement := _resize_exact(left, term.type)):
            candidates.append((rule, replacement))
        if _is_zero(left) and (replacement := _resize_exact(right, term.type)):
            candidates.append((rule, replacement))
    elif operator in {BinaryOperator.SHIFT_LEFT, BinaryOperator.SHIFT_RIGHT}:
        if _is_zero(right) and left.type == term.type:
            candidates.append((RewriteRule.SHIFT_ZERO, left))
    return tuple(candidates)


def _resize_exact(term: Term, target: HardwareType) -> Term | None:
    if term.type == target:
        return term
    if type(term.type) is not type(target):
        return None
    if not isinstance(term.type, (UIntType, SIntType, BitsType)):
        return None
    if term.type.width > target.width:
        return None
    return Term(
        NodeCategory.VALUE,
        ExpressionOp.EXTEND,
        target,
        (term,),
    )


def _constant(value: int, type_: HardwareType) -> Term:
    return Term(
        NodeCategory.VALUE,
        ExpressionOp.CONSTANT,
        type_,
        attributes=(("value", value),),
    )


def _constant_value(term: Term) -> int | None:
    if term.op is not ExpressionOp.CONSTANT:
        return None
    value = term.attribute("value")
    return value if isinstance(value, int) else None


def _is_zero(term: Term) -> bool:
    return _constant_value(term) == 0


def _is_one(term: Term) -> bool:
    return _constant_value(term) == 1


def _power_of_two_shift(term: Term) -> int | None:
    value = _constant_value(term)
    if value is None or value < 1 or value & (value - 1):
        return None
    return value.bit_length() - 1


def _render_value(value: object) -> str:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return "[" + ",".join(_render_value(item) for item in value) + "]"
    return str(value)


# M26 uses egglog for congruence closure and saturation.  The expression schema
# keeps the exact ZLang type on every node and on every child wrapper; rewrite
# results therefore cannot cross a type boundary.
from egglog import EGraph, Expr as EggExpr, StringLike, String, function, rewrite, var
from egglog.deconstruct import get_callable_args, get_callable_fn


class _EggNode(EggExpr):
    def __init__(self, tag: StringLike, payload: StringLike): ...


@function
def _egg_typed(type_name: StringLike, expression: _EggNode) -> _EggNode: ...


@function
def _egg_node(
    op: StringLike,
    type_name: StringLike,
    payload: StringLike,
    first: _EggNode,
    second: _EggNode,
    third: _EggNode,
) -> _EggNode: ...


_EGG_NONE = _EggNode("none", "")


@dataclass(frozen=True)
class _CompiledRewrite:
    identity: str
    rule: RewriteRule
    direction: str
    provenance: tuple[str, ...]
    guards: tuple[str, ...]
    engine_rule: Any = field(compare=False, hash=False, repr=False)
    reverse_engine_rule: Any | None = field(
        default=None,
        compare=False,
        hash=False,
        repr=False,
    )

    @property
    def engine_rules(self) -> tuple[Any, ...]:
        """Return the directed egglog rules implementing this registration.

        Built-in M26 simplifications are intentionally one-way.  A source
        ``<=>`` declaration is one logical registration backed by two directed
        engine rules, so reporting and provenance stay attached to the source
        equality rather than pretending that it was two source declarations.
        """

        if self.reverse_engine_rule is None:
            return (self.engine_rule,)
        return (self.engine_rule, self.reverse_engine_rule)


@dataclass(frozen=True)
class _PatternValue:
    type: HardwareType
    constant: int | None = None


def saturate(
    module: CanonicalModule,
    root: NodeId,
    *,
    mode: EquivalenceMode = EquivalenceMode.MATHEMATICAL,
    max_iterations: int = 8,
    max_terms: int = 256,
) -> SaturationResult:
    """Saturate one pure scalar root with the pinned egglog engine.

    M26 deliberately excludes arithmetic identities, reassociation, strength
    reduction, timing, architecture, and protocol rewrites.
    """

    if mode is not EquivalenceMode.MATHEMATICAL:
        raise SaturationError(
            "M26 e-graph saturation supports only mathematical equivalence, "
            f"got {mode.value}"
        )
    if max_iterations < 1:
        raise SaturationError("max_iterations must be at least 1")
    if max_terms < 1:
        raise SaturationError("max_terms must be at least 1")
    _require_pure_value_root(module, root)
    try:
        semantic_module = restore(module)
        semantic_root = restore_expression(module.expressions, root)
        expanded_root = expand_callable_calls(
            semantic_root,
            (*semantic_module.functions, *semantic_module.callable_definitions),
        )
        expanded_nodes, expanded_root_id = lower_expression_graph(
            semantic_module,
            expanded_root,
            scope="m26-call-expansion",
        )
        adapter_program = canonical_nodes_to_egraph(
            expanded_nodes,
            expanded_root_id,
        )
        adapter_nodes, adapter_root = egraph_to_canonical(adapter_program)
    except (CallableExpansionError, EGraphAdapterError) as error:
        raise SaturationError(str(error)) from error
    original = _term_from_root(adapter_nodes, adapter_root)
    egg_root = _term_to_egg(original)
    compiled, disabled = _compile_egg_rewrites(module.equivalences, original)
    graph = EGraph(*(
        engine_rule
        for item in compiled
        for engine_rule in item.engine_rules
    ))
    graph.let("root", egg_root)
    report = graph.run(max_iterations)
    eclass_count = _engine_typed_eclass_count(graph)
    extracted = graph.extract_multiple(egg_root, max_terms + 1)
    terms = [_egg_to_term(item) for item in extracted]
    if any(term.type != original.type for term in terms):
        raise SaturationError("egglog produced an incompatible type in one e-class")
    # ``extract_multiple`` enumerates bounded cheapest representatives; it is
    # not an archival dump of every inserted expression.  In particular, after
    # nested identities such as ``(x | 0) ^ 0`` merge, egglog can return ``x``,
    # ``x | 0`` and ``x ^ 0`` without returning the more expensive original.
    # The source root is independently adapter-validated above, so retain it
    # explicitly and validate every engine-produced candidate before exposing
    # the relation to unified exploration.
    for term in terms:
        term_to_expression(term)
    unique = {render_term(term): term for term in terms}
    original_key = render_term(original)
    unique.setdefault(original_key, original)
    ordered = [original] + [
        term
        for key, term in sorted(unique.items())
        if key != original_key
    ]
    truncated = len(ordered) > max_terms
    if len(ordered) > max_terms:
        ordered = ordered[:max_terms]
    alternatives = tuple(term for term in ordered if term != original)
    iterations = len(report.iterations)
    saturated = (iterations < max_iterations or not report.updated) and not truncated
    truncated = truncated or (len(ordered) >= max_terms and not saturated)
    registrations = tuple(
        sorted(
            (
                *(
                    RewriteRegistration(
                        item.identity,
                        item.rule,
                        item.direction,
                        item.provenance,
                        item.guards,
                        True,
                        any(
                            report.num_matches_per_rule.get(engine_rule.decl, 0) > 0
                            for engine_rule in item.engine_rules
                        ),
                    )
                    for item in compiled
                ),
                *disabled,
            ),
            key=lambda item: item.identity,
        )
    )
    rules = tuple(
        sorted(
            {item.rule for item in registrations if item.enabled and item.fired},
            key=lambda item: item.value,
        )
    )
    return SaturationResult(
        root,
        EquivalenceClass(0, mode, original.type, (original, *alternatives)),
        equivalence_definition(mode).observations,
        rules,
        iterations,
        max_iterations,
        max_terms,
        saturated,
        truncated,
        registrations,
        tuple(
            item.reason
            for item in registrations
            if not item.enabled
            and item.reason is not None
            and any(source.startswith("source:") for source in item.provenance)
        ),
        eclass_count,
    )


def _engine_typed_eclass_count(graph: EGraph) -> int:
    """Count exact-typed expression e-classes in the saturated egglog graph.

    ``egglog==13.2.0`` does not expose a public scalar e-class counter.  Its
    deterministic serialized graph does expose the engine e-class assigned to
    every node.  ZLang reports the number of distinct classes containing an
    ``_egg_typed`` node: these are the semantic typed-expression classes.  Raw
    constructor nodes and the implementation-only ``String`` classes are
    deliberately excluded.
    """

    serialized = json.loads(
        graph._serialize(include_temporary_functions=True).to_json()
    )
    return len({
        node["eclass"]
        for node in serialized.get("nodes", {}).values()
        if node.get("op") == "_egg_typed"
    })


def _compile_egg_rewrites(
    equivalences: tuple[EquivalenceRule, ...],
    original: Term,
) -> tuple[tuple[_CompiledRewrite, ...], tuple[RewriteRegistration, ...]]:
    """Build the exact bounded rule set used for one typed root.

    Built-ins remain the M26 baseline.  A source declaration replaces the
    corresponding built-in family for this module, so its typed guard is
    observable rather than being bypassed by an unconditional duplicate.
    Source rules are instantiated only for exact types and constants already
    present in the eligible root graph.
    """

    source_families = {_rule_family(rule) for rule in equivalences}
    compiled: list[_CompiledRewrite] = []
    disabled: list[RewriteRegistration] = []

    for identity, logical, family, engine_rule in _builtin_egg_rewrites():
        if family in source_families:
            disabled.append(
                RewriteRegistration(
                    identity,
                    logical,
                    "equality",
                    (f"builtin:{identity}",),
                    (),
                    False,
                    reason="replaced by source equiv declaration",
                )
            )
            continue
        compiled.append(
            _CompiledRewrite(
                identity,
                logical,
                "equality",
                (f"builtin:{identity}",),
                (),
                engine_rule,
            )
        )

    for rule in sorted(equivalences, key=lambda item: item.name):
        source_rules = _source_egg_rewrites(rule, original)
        if not source_rules:
            logical = _logical_rule(rule)
            guards = tuple(item.render() for item in rule.guards)
            disabled.append(
                RewriteRegistration(
                    f"source.{rule.name}",
                    logical,
                    "equality",
                    (f"source:{rule.name}",),
                    guards,
                    False,
                    reason="guard has no satisfying typed binding in this root",
                )
            )
            continue
        compiled.extend(source_rules)

    # Equal engine declarations are registered once.  Their distinct builtin
    # or source origins remain visible as sorted provenance on the one rule.
    deduplicated: dict[str, _CompiledRewrite] = {}
    for item in compiled:
        key = repr(item.engine_rule.decl)
        previous = deduplicated.get(key)
        if previous is None:
            deduplicated[key] = item
            continue
        deduplicated[key] = replace(
            previous,
            provenance=tuple(sorted(set(previous.provenance + item.provenance))),
            guards=tuple(sorted(set(previous.guards + item.guards))),
            identity=min(previous.identity, item.identity),
        )
    return (
        tuple(sorted(deduplicated.values(), key=lambda item: item.identity)),
        tuple(sorted(disabled, key=lambda item: item.identity)),
    )


def _builtin_egg_rewrites():
    type_name = var("type_name", String)
    shift_type = var("shift_type", String)
    x = var("x", _EggNode)
    y = var("y", _EggNode)
    condition = var("condition", _EggNode)
    resize_inner = var("resize_inner", _EggNode)
    exact_resize_operand = _egg_typed(type_name, resize_inner)
    zero = _egg_constant(type_name, 0)
    shift_zero = _egg_constant(shift_type, 0)
    return (
        ("bit_or_zero", RewriteRule.BIT_OR_ZERO, ("or_zero", "|"),
         rewrite(_egg_binary(type_name, "|", x, zero)).to(x)),
        ("bit_xor_zero", RewriteRule.BIT_XOR_ZERO, ("xor_zero", "^"),
         rewrite(_egg_binary(type_name, "^", x, zero)).to(x)),
        ("shift_left_zero", RewriteRule.SHIFT_ZERO, ("shift_zero", "<<"),
         rewrite(_egg_binary(type_name, "<<", x, shift_zero)).to(x)),
        ("shift_right_zero", RewriteRule.SHIFT_ZERO, ("shift_zero", ">>"),
         rewrite(_egg_binary(type_name, ">>", x, shift_zero)).to(x)),
        ("mux_identity", RewriteRule.MUX_IDENTITY, ("mux_identity", None),
         rewrite(_egg_mux(type_name, condition, x, x)).to(x)),
        ("mux_constant_false", RewriteRule.MUX_CONSTANT, ("mux_constant", "0"),
         rewrite(_egg_mux(type_name, _egg_constant("bit", 0), x, y)).to(y)),
        ("mux_constant_true", RewriteRule.MUX_CONSTANT, ("mux_constant", "1"),
         rewrite(_egg_mux(type_name, _egg_constant("bit", 1), x, y)).to(x)),
        ("extend_identity", RewriteRule.RESIZE_IDENTITY, ("resize_identity", "extend"),
         rewrite(_egg_resize(type_name, "extend", exact_resize_operand)).to(exact_resize_operand)),
        ("truncate_identity", RewriteRule.RESIZE_IDENTITY, ("resize_identity", "truncate"),
         rewrite(_egg_resize(type_name, "truncate", exact_resize_operand)).to(exact_resize_operand)),
    )


def _source_egg_rewrites(
    rule: EquivalenceRule,
    original: Term,
) -> tuple[_CompiledRewrite, ...]:
    logical = _logical_rule(rule)
    bindings = dict(rule.bindings)
    value_name = bindings["value"]
    condition_name = bindings.get("condition")
    terms = _all_terms(original)
    types = tuple(sorted({item.type for item in terms}, key=str))
    results: list[_CompiledRewrite] = []
    serial = 0

    for value_type in types:
        value_options = _pattern_options(value_name, value_type, rule.guards, terms)
        if condition_name is None:
            condition_options: tuple[_PatternValue | None, ...] = (None,)
        else:
            condition_options = tuple(
                _pattern_options(condition_name, BitType(), rule.guards, terms)
            )
        for value_binding, condition_binding in product(
            value_options,
            condition_options,
        ):
            typed_bindings = {value_name: value_binding}
            if condition_name is not None and condition_binding is not None:
                typed_bindings[condition_name] = condition_binding
            if not all(
                _guard_holds(predicate, typed_bindings)
                for predicate in rule.guards
            ):
                continue
            serial += 1
            value = _egg_pattern_value(
                value_binding,
                f"source_value_{serial}",
            )
            condition = (
                None
                if condition_binding is None
                else _egg_pattern_value(
                    condition_binding,
                    f"source_condition_{serial}",
                )
            )
            guard_text = tuple(item.render() for item in rule.guards)
            for variant, (left, right) in enumerate(
                _source_rule_pairs(
                    rule,
                    value_type,
                    value,
                    condition,
                    terms,
                ),
                1,
            ):
                identity = (
                    f"source.{rule.name}.{rule.kind}."
                    f"{str(value_type).replace('<', '_').replace('>', '')}."
                    f"{serial}.{variant}"
                )
                results.append(
                    _CompiledRewrite(
                        identity,
                        logical,
                        "equality",
                        (f"source:{rule.name}",),
                        guard_text,
                        rewrite(left).to(right),
                        (
                            rewrite(right).to(left)
                            if _source_reverse_is_constructible(rule)
                            else None
                        ),
                    )
                )
    return tuple(results)


def _source_reverse_is_constructible(rule: EquivalenceRule) -> bool:
    """Whether the reverse engine direction has no unbound pattern values.

    A mux identity removes its condition variable.  Starting from the value
    alone cannot synthesize an arbitrary condition, and egglog correctly
    rejects such a reverse rewrite as unbound.  Matching the mux still unions
    both expressions into one symmetric e-class; the other frozen source
    families lose only typed constants and are constructible in both engine
    directions.
    """

    return rule.kind != "mux_identity"


def _source_rule_pairs(
    rule: EquivalenceRule,
    value_type: HardwareType,
    value: _EggNode,
    condition: _EggNode | None,
    terms: tuple[Term, ...],
) -> tuple[tuple[_EggNode, _EggNode], ...]:
    type_name = _type_name(value_type)
    if rule.kind == "or_zero":
        return ((_egg_binary(type_name, "|", value, _egg_constant(type_name, 0)), value),)
    if rule.kind == "xor_zero":
        return ((_egg_binary(type_name, "^", value, _egg_constant(type_name, 0)), value),)
    if rule.kind == "shift_zero":
        shift_types = tuple(
            sorted(
                {
                    item.type
                    for item in terms
                    if _constant_value(item) == 0
                },
                key=str,
            )
        )
        return tuple(
            (
                _egg_binary(
                    type_name,
                    rule.operator or "<<",
                    value,
                    _egg_constant(_type_name(shift_type), 0),
                ),
                value,
            )
            for shift_type in (shift_types or (value_type,))
        )
    if rule.kind == "mux_identity" and condition is not None:
        return ((_egg_mux(type_name, condition, value, value), value),)
    raise SaturationError(
        f"unsupported source equivalence rule kind '{rule.kind}'"
    )


def _pattern_options(
    variable: str,
    type_: HardwareType,
    guards: tuple[EquivalenceGuardPredicate, ...],
    terms: tuple[Term, ...],
) -> tuple[_PatternValue, ...]:
    requires_constant = any(
        predicate.kind in {
            EquivalenceGuardKind.CONSTANT,
            EquivalenceGuardKind.POWER_OF_TWO,
        }
        and variable in predicate.arguments
        for predicate in guards
    )
    if not requires_constant:
        return (_PatternValue(type_),)
    values = {
        value
        for item in terms
        if item.type == type_
        if (value := _constant_value(item)) is not None
    }
    return tuple(_PatternValue(type_, value) for value in sorted(values))


def _egg_pattern_value(binding: _PatternValue, variable: str) -> _EggNode:
    if binding.constant is not None:
        return _egg_constant(_type_name(binding.type), binding.constant)
    return _egg_typed(_type_name(binding.type), var(variable, _EggNode))


def _guard_holds(
    predicate: EquivalenceGuardPredicate,
    bindings: dict[str, _PatternValue],
) -> bool:
    values = tuple(bindings[name] for name in predicate.arguments)
    first = values[0]
    if predicate.kind is EquivalenceGuardKind.UNSIGNED:
        return isinstance(first.type, UIntType)
    if predicate.kind is EquivalenceGuardKind.SIGNED:
        return isinstance(first.type, SIntType)
    if predicate.kind is EquivalenceGuardKind.BITS:
        return isinstance(first.type, BitsType)
    if predicate.kind is EquivalenceGuardKind.BIT:
        return isinstance(first.type, BitType)
    if predicate.kind is EquivalenceGuardKind.WIDTH:
        return first.type.width == predicate.value
    if predicate.kind is EquivalenceGuardKind.SAME_TYPE:
        return values[0].type == values[1].type
    if predicate.kind is EquivalenceGuardKind.CONSTANT:
        return first.constant is not None
    if predicate.kind is EquivalenceGuardKind.POWER_OF_TWO:
        value = first.constant
        return value is not None and value > 0 and value & (value - 1) == 0
    raise SaturationError(f"unsupported equiv guard kind '{predicate.kind.value}'")


def _all_terms(root: Term) -> tuple[Term, ...]:
    known: dict[str, Term] = {}

    def visit(term: Term) -> None:
        known.setdefault(render_term(term), term)
        for operand in term.operands:
            visit(operand)

    visit(root)
    return tuple(known[key] for key in sorted(known))


def _rule_family(rule: EquivalenceRule) -> tuple[str, str | None]:
    return rule.kind, rule.operator


def _logical_rule(rule: EquivalenceRule) -> RewriteRule:
    return {
        "or_zero": RewriteRule.BIT_OR_ZERO,
        "xor_zero": RewriteRule.BIT_XOR_ZERO,
        "shift_zero": RewriteRule.SHIFT_ZERO,
        "mux_identity": RewriteRule.MUX_IDENTITY,
    }[rule.kind]


def _egg_rewrites(equivalences=(), original: Term | None = None):
    """Compatibility helper returning the effective engine rules."""

    if original is None:
        return [item[3] for item in _builtin_egg_rewrites()]
    compiled, _ = _compile_egg_rewrites(tuple(equivalences), original)
    return [
        engine_rule
        for item in compiled
        for engine_rule in item.engine_rules
    ]


def _egg_binary(type_name: StringLike, operator: StringLike, left: _EggNode, right: _EggNode) -> _EggNode:
    return _egg_typed(type_name, _egg_node("binary", type_name, operator, left, right, _EGG_NONE))


def _egg_mux(type_name: StringLike, condition: _EggNode, when_true: _EggNode, when_false: _EggNode) -> _EggNode:
    return _egg_typed(type_name, _egg_node("mux", type_name, "", condition, when_true, when_false))


def _egg_resize(type_name: StringLike, operation: StringLike, operand: _EggNode) -> _EggNode:
    return _egg_typed(type_name, _egg_node("resize", type_name, operation, operand, _EGG_NONE, _EGG_NONE))


def _egg_constant(type_name: StringLike, value: int) -> _EggNode:
    return _egg_typed(type_name, _egg_node("constant", type_name, str(value), _EGG_NONE, _EGG_NONE, _EGG_NONE))


def _term_to_egg(term: Term) -> _EggNode:
    type_name = _type_name(term.type)
    operands = tuple(_term_to_egg(item) for item in term.operands)
    if term.op is ExpressionOp.CONSTANT:
        return _egg_constant(type_name, int(term.attribute("value")))
    if term.op is ExpressionOp.ADD:
        return _egg_binary(type_name, "add", operands[0], operands[1])
    if term.op is ExpressionOp.BINARY:
        return _egg_binary(type_name, str(term.attribute("operator").value), operands[0], operands[1])
    if term.op is ExpressionOp.MUX:
        return _egg_mux(type_name, operands[0], operands[1], operands[2])
    if term.op in {ExpressionOp.EXTEND, ExpressionOp.TRUNCATE}:
        return _egg_resize(type_name, term.op.value, operands[0])
    # Opaque pure scalar leaves preserve the existing semantic term while
    # preventing unapproved rewrites from looking through it.
    payload = json.dumps({"op": term.op.value, "attributes": repr(term.attributes)}, sort_keys=True)
    return _egg_typed(type_name, _EggNode("opaque", payload))


def _egg_to_term(expression: _EggNode) -> Term:
    typed_args = get_callable_args(expression, _egg_typed)
    if typed_args is None:
        raise SaturationError("egglog extraction produced an untyped expression")
    type_name, inner = typed_args
    type_ = _parse_type_name(_string_value(type_name))
    args = get_callable_args(inner)
    fn = get_callable_fn(inner)
    fn_name = getattr(fn, "__name__", str(fn))
    if fn_name in {"_EggNode", "Z"}:
        tag, payload = (_string_value(item) for item in args)
        tag = str(tag)
        if tag == "constant":
            return Term(NodeCategory.VALUE, ExpressionOp.CONSTANT, type_, attributes=(("value", int(payload)),))
        if tag == "opaque":
            encoded = json.loads(payload)
            attributes = tuple(ast.literal_eval(encoded["attributes"]))
            return Term(
                NodeCategory.VALUE,
                ExpressionOp(encoded["op"]),
                type_,
                attributes=attributes,
            )
        return Term(NodeCategory.VALUE, ExpressionOp.INPUT, type_, attributes=(("name", payload),))
    if fn_name in {"_egg_node", "node"}:
        op, _, payload, first, second, third = args
        op_name = _string_value(op)
        if op_name == "constant":
            return Term(NodeCategory.VALUE, ExpressionOp.CONSTANT, type_, attributes=(("value", int(_string_value(payload))),))
        if op_name == "binary":
            left_term = _egg_to_term(first)
            right_term = _egg_to_term(second)
            if _string_value(payload) == "add":
                return Term(NodeCategory.VALUE, ExpressionOp.ADD, type_,
                            (left_term, right_term))
            operator = BinaryOperator(_string_value(payload))
            operand_type = (
                left_term.type
                if operator
                in {
                    BinaryOperator.EQUAL,
                    BinaryOperator.NOT_EQUAL,
                    BinaryOperator.LESS,
                    BinaryOperator.LESS_EQUAL,
                    BinaryOperator.GREATER,
                    BinaryOperator.GREATER_EQUAL,
                }
                else type_
            )
            return Term(NodeCategory.VALUE, ExpressionOp.BINARY, type_,
                        (left_term, right_term),
                        (("operator", operator), ("operand_type", operand_type)))
        if op_name == "mux":
            return Term(NodeCategory.VALUE, ExpressionOp.MUX, type_,
                        (_egg_to_term(first), _egg_to_term(second), _egg_to_term(third)))
        if op_name == "resize":
            operation = ExpressionOp.EXTEND if _string_value(payload) == "extend" else ExpressionOp.TRUNCATE
            return Term(NodeCategory.VALUE, operation, type_, (_egg_to_term(first),))
    raise SaturationError(f"egglog extraction produced an unsupported expression fn={fn!r} name={fn_name!r} args={args!r}")


def _type_name(type_: HardwareType) -> str:
    return str(type_)


def _parse_type_name(name: str) -> HardwareType:
    if name == "bit": return BitType()
    if name.startswith("u"): return UIntType(int(name[1:]))
    if name.startswith("s"): return SIntType(int(name[1:]))
    if name.startswith("bits<"): return BitsType(int(name[5:-1]))
    raise SaturationError(f"unsupported e-graph type '{name}'")


def _string_value(value: object) -> str:
    return str(getattr(value, "value", value))
