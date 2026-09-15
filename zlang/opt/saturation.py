"""Bounded egglog execution and extraction for exact typed value rewrites.

Adapter validation, stable result models, declarative rule metadata, semantic
guards, and subsystem capabilities live in their dedicated modules.  This
engine does not schedule cycles, bind resources, or admit state/protocol nodes.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import ast
from hashlib import sha256
import json
from itertools import product
from typing import Any

from egglog import EGraph, Expr as EggExpr, StringLike, String, function, rewrite, var
from egglog.deconstruct import get_callable_args, get_callable_fn

from zlang.ir.expressions import (
    BinaryOperator,
    FixedConversionKind,
    FixedOverflow,
    FixedRounding,
)
from zlang.ir.module import (
    EquivalenceRule,
)
from zlang.ir.types import (
    BitType,
    FixedType,
    HardwareType,
    SIntType,
    UFixedType,
    UIntType,
)
from zlang.ir.type_codec import (
    TypeCodecError,
    scalar_type_from_name,
    scalar_type_name as _type_name,
)
from zlang.opt.ir import (
    CanonicalExpression,
    CanonicalModule,
    EquivalenceMode,
    ExpressionOp,
    NodeCategory,
    NodeId,
    equivalence_definition,
)
from zlang.ir.callables import CallableExpansionError, expand_callable_calls
from zlang.opt.egraph import (
    EGraphAdapterError,
    canonical_nodes_to_egraph,
    egraph_to_canonical,
    validate_scalar_pure_nodes,
)
from zlang.opt.lowering import lower_expression_graph, restore, restore_expression
from zlang.opt.rewrite_spec import (
    RewriteRegistration,
    RewriteRule,
    TypedRewriteSpec,
    builtin_rewrite_spec,
)
from zlang.opt.rewrite_model import (
    EquivalenceClass,
    SaturationResult,
    Term,
    render_saturation as render_saturation,
    render_term,
    term_constant_value,
    term_to_expression,
)
from zlang.opt.rewrite_guards import (
    PatternValue,
    RewriteGuardError,
    guard_holds,
    pattern_options,
)


class SaturationError(ValueError):
    """A root or requested equality-saturation mode is not eligible."""


def _require_pure_value_root(module: CanonicalModule, root: NodeId) -> None:
    try:
        validate_scalar_pure_nodes(
            module.expressions,
            root,
            allow_retained_calls=True,
        )
    except EGraphAdapterError as error:
        raise SaturationError(str(error)) from error


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


def _power_of_two_shift(term: Term) -> int | None:
    value = term_constant_value(term)
    if value is None or value < 1 or value & (value - 1):
        return None
    return value.bit_length() - 1


# M26 uses egglog for congruence closure and saturation.  The expression schema
# keeps the exact ZLang type on every node and on every child wrapper; rewrite
# results therefore cannot cross a type boundary.


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


@function
def _egg_operand_list(first: _EggNode, rest: _EggNode) -> _EggNode: ...


_EGG_NONE = _EggNode("none", "")
_EGG_OPERAND_NIL = _EggNode("operand_nil", "")


@dataclass(frozen=True)
class _CompiledRewrite:
    spec: TypedRewriteSpec
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

    @property
    def identity(self) -> str:
        return self.spec.identity

    @property
    def rule(self) -> RewriteRule:
        return self.spec.rule

    @property
    def direction(self) -> str:
        return self.spec.direction

    @property
    def provenance(self) -> tuple[str, ...]:
        return self.spec.provenance

    @property
    def guards(self) -> tuple[str, ...]:
        return self.spec.guards


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
                    RewriteRegistration.from_spec(
                        item.spec,
                        enabled=True,
                        fired=any(
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

    for spec, engine_rule in _builtin_egg_rewrites():
        if spec.family in source_families:
            disabled.append(
                RewriteRegistration.from_spec(
                    spec,
                    enabled=False,
                    reason="replaced by source equiv declaration",
                )
            )
            continue
        compiled.append(_CompiledRewrite(spec, engine_rule))

    for spec, engine_rule in _typed_arithmetic_egg_rewrites(
        original
    ):
        compiled.append(_CompiledRewrite(spec, engine_rule))

    for rule in sorted(equivalences, key=lambda item: item.name):
        source_rules = _source_egg_rewrites(rule, original)
        if not source_rules:
            logical = _logical_rule(rule)
            guards = tuple(item.render() for item in rule.guards)
            disabled.append(
                RewriteRegistration.from_spec(
                    TypedRewriteSpec(
                        f"source.{rule.name}",
                        logical,
                        _rule_family(rule),
                        "equality",
                        (f"source:{rule.name}",),
                        guards,
                    ),
                    enabled=False,
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
            spec=replace(
                previous.spec,
                provenance=tuple(
                    sorted(set(previous.provenance + item.provenance))
                ),
                guards=tuple(sorted(set(previous.guards + item.guards))),
                identity=min(previous.identity, item.identity),
            ),
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
        (builtin_rewrite_spec("bit_or_zero"),
         rewrite(_egg_binary(type_name, "|", x, zero)).to(x)),
        (builtin_rewrite_spec("bit_xor_zero"),
         rewrite(_egg_binary(type_name, "^", x, zero)).to(x)),
        (builtin_rewrite_spec("shift_left_zero"),
         rewrite(_egg_binary(type_name, "<<", x, shift_zero)).to(x)),
        (builtin_rewrite_spec("shift_right_zero"),
         rewrite(_egg_binary(type_name, ">>", x, shift_zero)).to(x)),
        (builtin_rewrite_spec("mux_identity"),
         rewrite(_egg_mux(type_name, condition, x, x)).to(x)),
        (builtin_rewrite_spec("mux_constant_false"),
         rewrite(_egg_mux(type_name, _egg_constant("bit", 0), x, y)).to(y)),
        (builtin_rewrite_spec("mux_constant_true"),
         rewrite(_egg_mux(type_name, _egg_constant("bit", 1), x, y)).to(x)),
        (builtin_rewrite_spec("extend_identity"),
         rewrite(_egg_resize(type_name, "extend", exact_resize_operand)).to(exact_resize_operand)),
        (builtin_rewrite_spec("truncate_identity"),
         rewrite(_egg_resize(type_name, "truncate", exact_resize_operand)).to(exact_resize_operand)),
    )


def _typed_arithmetic_egg_rewrites(original: Term):
    """Compile exact-signature arithmetic rules observed in this typed DAG.

    Concrete type names are part of every rule.  This deliberately avoids an
    untyped universal arithmetic identity and makes mixed-width/signature
    changes impossible inside an e-class.
    """

    x_inner = var("arithmetic_x_inner", _EggNode)
    y_inner = var("arithmetic_y_inner", _EggNode)
    rules: list[tuple[str, RewriteRule, tuple[str, str], object]] = []
    signatures: set[tuple[str, HardwareType, HardwareType, HardwareType, HardwareType | None]] = set()
    for term in _all_terms(original):
        if term.op is ExpressionOp.ADD and len(term.operands) == 2:
            left, right = term.operands
            signatures.add(("add", term.type, left.type, right.type, None))
        elif term.op is ExpressionOp.BINARY and len(term.operands) == 2:
            operator = term.attribute("operator")
            if operator in {BinaryOperator.SUBTRACT, BinaryOperator.MULTIPLY}:
                left, right = term.operands
                signatures.add((
                    operator.value,
                    term.type,
                    left.type,
                    right.type,
                    term.attribute("operand_type"),
                ))
    for operator, result_type, left_type, right_type, operand_type in sorted(
        signatures,
        key=lambda item: tuple(str(part) for part in item),
    ):
        result_name = _type_name(result_type)
        left_name = _type_name(left_type)
        right_name = _type_name(right_type)
        operand_name = _type_name(operand_type or result_type)
        x = _egg_typed(left_name, x_inner)
        y = _egg_typed(right_name, y_inner)
        signature = sha256(
            repr((operator, result_name, left_name, right_name, operand_name)).encode()
        ).hexdigest()[:16]
        if operator == "add":
            # Add is commutative only when swapping operands preserves the
            # exact typed signature admitted by semantic analysis.
            if left_type == right_type:
                rules.append((
                    f"add_commute.{signature}", RewriteRule.ADD_COMMUTE,
                    ("add_commute", "add"),
                    rewrite(_egg_binary(result_name, "add", x, y)).to(
                        _egg_binary(result_name, "add", y, x)
                    ),
                ))
            if left_type == result_type:
                rules.append((
                    f"add_zero_right.{signature}", RewriteRule.ADD_ZERO,
                    ("add_zero", "add"),
                    rewrite(
                        _egg_binary(
                            result_name, "add", x,
                            _egg_constant(right_name, 0),
                        )
                    ).to(x),
                ))
            if right_type == result_type:
                rules.append((
                    f"add_zero_left.{signature}", RewriteRule.ADD_ZERO,
                    ("add_zero", "add"),
                    rewrite(
                        _egg_binary(
                            result_name, "add",
                            _egg_constant(left_name, 0), y,
                        )
                    ).to(y),
                ))
            continue
        if operator == BinaryOperator.SUBTRACT.value and left_type == result_type:
            rules.append((
                f"subtract_zero.{signature}", RewriteRule.SUBTRACT_ZERO,
                ("subtract_zero", operator),
                rewrite(
                    _egg_binary(
                        result_name, operator, x,
                        _egg_constant(right_name, 0), operand_name,
                    )
                ).to(x),
            ))
            continue
        if operator != BinaryOperator.MULTIPLY.value:
            continue
        if left_type == right_type:
            rules.append((
                f"multiply_commute.{signature}", RewriteRule.MULTIPLY_COMMUTE,
                ("multiply_commute", operator),
                rewrite(
                    _egg_binary(result_name, operator, x, y, operand_name)
                ).to(
                    _egg_binary(result_name, operator, y, x, operand_name)
                ),
            ))
        # Multiplication by zero is exact for all supported integer/fixed
        # signatures and the replacement is explicitly result-typed.
        rules.extend((
            (
                f"multiply_zero_right.{signature}", RewriteRule.MULTIPLY_ZERO,
                ("multiply_zero", operator),
                rewrite(
                    _egg_binary(
                        result_name, operator, x,
                        _egg_constant(right_name, 0), operand_name,
                    )
                ).to(_egg_constant(result_name, 0)),
            ),
            (
                f"multiply_zero_left.{signature}", RewriteRule.MULTIPLY_ZERO,
                ("multiply_zero", operator),
                rewrite(
                    _egg_binary(
                        result_name, operator,
                        _egg_constant(left_name, 0), y, operand_name,
                    )
                ).to(_egg_constant(result_name, 0)),
            ),
        ))
        if not isinstance(result_type, (FixedType, UFixedType)):
            if left_type == result_type:
                rules.append((
                    f"multiply_one_right.{signature}", RewriteRule.MULTIPLY_ONE,
                    ("multiply_one", operator),
                    rewrite(
                        _egg_binary(
                            result_name, operator, x,
                            _egg_constant(right_name, 1), operand_name,
                        )
                    ).to(x),
                ))
            if right_type == result_type:
                rules.append((
                    f"multiply_one_left.{signature}", RewriteRule.MULTIPLY_ONE,
                    ("multiply_one", operator),
                    rewrite(
                        _egg_binary(
                            result_name, operator,
                            _egg_constant(left_name, 1), y, operand_name,
                        )
                    ).to(y),
                ))

        # Multiplication grows to a wider exact integer result in ZLang, while
        # shifts deliberately preserve their left-hand width.  A strength
        # reduction is therefore sound only when the non-constant operand is
        # first extended to the multiplication result type.  Instantiate the
        # rewrite for constants actually present in this bounded typed DAG;
        # this avoids an untyped "is power of two" predicate in egglog.
        if isinstance(result_type, (UIntType, SIntType)):
            for term in _all_terms(original):
                if (
                    term.op is not ExpressionOp.BINARY
                    or term.attribute("operator") is not BinaryOperator.MULTIPLY
                    or term.type != result_type
                    or len(term.operands) != 2
                ):
                    continue
                term_left, term_right = term.operands
                for side, constant, value in (
                    ("right", term_right, term_left),
                    ("left", term_left, term_right),
                ):
                    shift = _power_of_two_shift(constant)
                    if shift is None or shift < 1:
                        continue
                    if (
                        type(value.type) is not type(result_type)
                        or value.type.width > result_type.width
                    ):
                        continue
                    value_name = _type_name(value.type)
                    constant_name = _type_name(constant.type)
                    value_var = _egg_typed(value_name, x_inner)
                    widened = (
                        value_var
                        if value.type == result_type
                        else _egg_resize(result_name, "extend", value_var)
                    )
                    shift_type = UIntType(max(1, shift.bit_length()))
                    shifted = _egg_binary(
                        result_name,
                        BinaryOperator.SHIFT_LEFT.value,
                        widened,
                        _egg_constant(_type_name(shift_type), shift),
                        result_name,
                    )
                    constant_node = _egg_constant(
                        constant_name,
                        int(constant.attribute("value")),
                    )
                    multiply = (
                        _egg_binary(
                            result_name,
                            operator,
                            value_var,
                            constant_node,
                            operand_name,
                        )
                        if side == "right"
                        else _egg_binary(
                            result_name,
                            operator,
                            constant_node,
                            value_var,
                            operand_name,
                        )
                    )
                    power_signature = sha256(
                        repr((signature, side, value.type, constant.type, shift)).encode()
                    ).hexdigest()[:16]
                    rules.append((
                        f"multiply_power_of_two.{power_signature}",
                        RewriteRule.MULTIPLY_POWER_OF_TWO,
                        ("multiply_power_of_two", operator),
                        rewrite(multiply).to(shifted),
                    ))
    return tuple(
        (TypedRewriteSpec.builtin(identity, logical, family), engine_rule)
        for identity, logical, family, engine_rule in rules
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
        value_options = pattern_options(value_name, value_type, rule.guards, terms)
        if condition_name is None:
            condition_options: tuple[PatternValue | None, ...] = (None,)
        else:
            condition_options = tuple(
                pattern_options(condition_name, BitType(), rule.guards, terms)
            )
        for value_binding, condition_binding in product(
            value_options,
            condition_options,
        ):
            typed_bindings = {value_name: value_binding}
            if condition_name is not None and condition_binding is not None:
                typed_bindings[condition_name] = condition_binding
            try:
                guards_hold = all(
                    guard_holds(predicate, typed_bindings)
                    for predicate in rule.guards
                )
            except RewriteGuardError as error:
                raise SaturationError(str(error)) from error
            if not guards_hold:
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
                        TypedRewriteSpec(
                            identity,
                            logical,
                            _rule_family(rule),
                            "equality",
                            (f"source:{rule.name}",),
                            guard_text,
                        ),
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
                    if term_constant_value(item) == 0
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


def _egg_pattern_value(binding: PatternValue, variable: str) -> _EggNode:
    if binding.constant is not None:
        return _egg_constant(_type_name(binding.type), binding.constant)
    return _egg_typed(_type_name(binding.type), var(variable, _EggNode))


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


def _egg_binary(
    type_name: StringLike,
    operator: StringLike,
    left: _EggNode,
    right: _EggNode,
    operand_type: StringLike | None = None,
) -> _EggNode:
    return _egg_typed(
        type_name,
        _egg_node(
            "binary", type_name, operator, left, right,
            _EggNode(
                "operand_type",
                type_name if operand_type is None else operand_type,
            ),
        ),
    )


def _egg_mux(type_name: StringLike, condition: _EggNode, when_true: _EggNode, when_false: _EggNode) -> _EggNode:
    return _egg_typed(type_name, _egg_node("mux", type_name, "", condition, when_true, when_false))


def _egg_resize(type_name: StringLike, operation: StringLike, operand: _EggNode) -> _EggNode:
    return _egg_typed(type_name, _egg_node("resize", type_name, operation, operand, _EGG_NONE, _EGG_NONE))


def _egg_fixed_convert(
    type_name: StringLike,
    payload: StringLike,
    operand: _EggNode,
) -> _EggNode:
    return _egg_typed(
        type_name,
        _egg_node(
            "fixed_convert", type_name, payload, operand, _EGG_NONE, _EGG_NONE
        ),
    )


def _egg_operands(values: tuple[_EggNode, ...]) -> _EggNode:
    result = _EGG_OPERAND_NIL
    for value in reversed(values):
        result = _egg_operand_list(value, result)
    return result


def _egg_wiring(
    type_name: StringLike,
    operation: StringLike,
    payload: StringLike,
    operands: tuple[_EggNode, ...],
) -> _EggNode:
    return _egg_typed(
        type_name,
        _egg_node(
            "wiring",
            type_name,
            json.dumps({"operation": operation, "payload": payload}),
            _egg_operands(operands),
            _EGG_NONE,
            _EGG_NONE,
        ),
    )


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
        return _egg_binary(
            type_name,
            str(term.attribute("operator").value),
            operands[0],
            operands[1],
            _type_name(term.attribute("operand_type")),
        )
    if term.op is ExpressionOp.MUX:
        return _egg_mux(type_name, operands[0], operands[1], operands[2])
    if term.op in {ExpressionOp.EXTEND, ExpressionOp.TRUNCATE}:
        return _egg_resize(type_name, term.op.value, operands[0])
    if term.op is ExpressionOp.FIXED_CONVERT:
        payload = json.dumps(
            {
                "rounding": term.attribute("rounding").value,
                "overflow": term.attribute("overflow").value,
                "conversion_kind": term.attribute("conversion_kind").value,
                "rational_denominator": dict(term.attributes).get(
                    "rational_denominator"
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return _egg_fixed_convert(type_name, payload, operands[0])
    if term.op is ExpressionOp.SLICE:
        return _egg_wiring(
            type_name,
            term.op.value,
            json.dumps({
                "msb": int(term.attribute("msb")),
                "lsb": int(term.attribute("lsb")),
            }, sort_keys=True),
            operands,
        )
    if term.op is ExpressionOp.CONCAT:
        return _egg_wiring(type_name, term.op.value, "", operands)
    if term.op is ExpressionOp.BITCAST:
        return _egg_wiring(type_name, term.op.value, "", operands)
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
            operand_args = get_callable_args(third, _EggNode)
            if operand_args is None or _string_value(operand_args[0]) != "operand_type":
                raise SaturationError(
                    "egglog binary node lost its exact operand-type signature"
                )
            operand_type = _parse_type_name(_string_value(operand_args[1]))
            return Term(NodeCategory.VALUE, ExpressionOp.BINARY, type_,
                        (left_term, right_term),
                        (("operator", operator), ("operand_type", operand_type)))
        if op_name == "mux":
            return Term(NodeCategory.VALUE, ExpressionOp.MUX, type_,
                        (_egg_to_term(first), _egg_to_term(second), _egg_to_term(third)))
        if op_name == "resize":
            operation = ExpressionOp.EXTEND if _string_value(payload) == "extend" else ExpressionOp.TRUNCATE
            return Term(NodeCategory.VALUE, operation, type_, (_egg_to_term(first),))
        if op_name == "fixed_convert":
            conversion = json.loads(_string_value(payload))
            return Term(
                NodeCategory.VALUE,
                ExpressionOp.FIXED_CONVERT,
                type_,
                (_egg_to_term(first),),
                (
                    ("rounding", FixedRounding(conversion["rounding"])),
                    ("overflow", FixedOverflow(conversion["overflow"])),
                    (
                        "conversion_kind",
                        FixedConversionKind(conversion["conversion_kind"]),
                    ),
                    (
                        "rational_denominator",
                        conversion.get("rational_denominator"),
                    ),
                ),
            )
        if op_name == "wiring":
            descriptor = json.loads(_string_value(payload))
            operation = ExpressionOp(descriptor["operation"])
            operands = _egg_operand_terms(first)
            if operation is ExpressionOp.SLICE:
                values = json.loads(descriptor["payload"])
                attributes = (("msb", values["msb"]), ("lsb", values["lsb"]))
            elif operation is ExpressionOp.CONCAT:
                attributes = (("operand_widths", tuple(item.type.width for item in operands)),)
            elif operation is ExpressionOp.BITCAST:
                if len(operands) != 1:
                    raise SaturationError("egglog bitcast lost its unique operand")
                attributes = (("source_type", operands[0].type),)
            else:  # pragma: no cover - closed by _term_to_egg.
                raise SaturationError(
                    f"egglog produced unsupported wiring operation {operation.value}"
                )
            return Term(
                NodeCategory.VALUE,
                operation,
                type_,
                operands,
                attributes,
            )
    raise SaturationError(f"egglog extraction produced an unsupported expression fn={fn!r} name={fn_name!r} args={args!r}")


def _egg_operand_terms(value: _EggNode) -> tuple[Term, ...]:
    result: list[Term] = []
    current = value
    while True:
        nil = get_callable_args(current, _EggNode)
        if nil is not None and _string_value(nil[0]) == "operand_nil":
            return tuple(result)
        pair = get_callable_args(current, _egg_operand_list)
        if pair is None:
            raise SaturationError("egglog wiring node has a malformed operand list")
        first, current = pair
        result.append(_egg_to_term(first))


def _parse_type_name(name: str) -> HardwareType:
    try:
        return scalar_type_from_name(name)
    except TypeCodecError as error:
        raise SaturationError(str(error)) from error


def _string_value(value: object) -> str:
    return str(getattr(value, "value", value))
