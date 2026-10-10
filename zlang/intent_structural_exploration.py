# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Bounded exact-value structures for one ``implement`` expression.

This owner decides only *what* bit-exact value DAGs are worth presenting to
implementation providers.  It does not know targets, resources, timing,
protocol admission, formal execution, or RTL emission.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass, field, replace
from enum import Enum
from hashlib import sha256
from importlib.metadata import version as distribution_version
from threading import Event, RLock, get_ident
from typing import Callable

from zlang.candidate_expression import (
    candidate_expression_children,
    candidate_input_refs,
)
from zlang.common.serialization import stable_digest
from zlang.ir import expressions as ir_expr
from zlang.ir.expression_graph import ExpressionDagIndex
from zlang.ir.module import Assignment, EquivalenceRule, Module, Port, PortDirection
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.opt.module_lowering import lower
from zlang.opt.rewrite_model import (
    CheckedValueCertificate,
    Term,
    render_term,
    term_to_expression,
)
from zlang.opt.saturation import (
    INTENT_STRUCTURAL_REWRITE_POLICY,
    SATURATION_ENGINE_SCHEMA,
    replay_checked_alternative,
    saturate,
)
from zlang.opt.value_certificate import CHECKER_VERSION


_CACHE_SCHEMA = "zlang-intent-structural-cache-v1"
_EGGLOG_VERSION = distribution_version("egglog")


@dataclass(frozen=True)
class StructuralSignature:
    """Target-neutral physical-shape family for one exact value DAG."""

    identity: str
    operation_counts: tuple[tuple[str, int], ...]
    unique_nodes: int
    logic_depth: int
    fanout_profile: tuple[int, ...]


@dataclass(frozen=True)
class StructuralAlternative:
    """One certified exact structure retained for downstream providers."""

    expression: ir_expr.Expression = field(compare=False, repr=False)
    selected_value_identity: str
    signature: StructuralSignature
    extraction_rank: tuple[int, int, str]
    certificate: CheckedValueCertificate | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    @property
    def is_source(self) -> bool:
        return self.certificate is None


@dataclass(frozen=True)
class StructuralExplorationStats:
    """Deterministic work/result accounting for one local e-graph."""

    saturation_iterations: int
    eclasses: int
    enodes: int
    raw_extractions: int
    structurally_unique_extractions: int
    retained_structures: int
    saturated: bool
    truncated: bool
    rejection_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntentStructuralExploration:
    alternatives: tuple[StructuralAlternative, ...]
    stats: StructuralExplorationStats


@dataclass(frozen=True)
class IntentStructuralExplorationKey:
    """Origin-free identity of one bounded structural search."""

    root_identity: str
    equivalence_identity: str
    limits: tuple[int, int, int, int, int]
    engine_identity: str = field(
        default=stable_digest(
            {
                "schema": _CACHE_SCHEMA,
                "saturation": SATURATION_ENGINE_SCHEMA,
                "rewrite_policy": INTENT_STRUCTURAL_REWRITE_POLICY,
                "checker": CHECKER_VERSION,
                "egglog": _EGGLOG_VERSION,
            }
        ),
        init=False,
    )


@dataclass(frozen=True)
class StructuralAlternativeRecipe:
    """One origin-free checked term retained in the structural cache."""

    term: Term
    selected_value_identity: str
    signature: StructuralSignature
    extraction_rank: tuple[int, int, str]
    certificate: CheckedValueCertificate


@dataclass(frozen=True)
class StructuralExplorationRecipe:
    """Reusable result of one exact saturation, independent of source sites."""

    source_term: Term
    alternatives: tuple[StructuralAlternativeRecipe, ...]
    stats: StructuralExplorationStats

    @property
    def weight(self) -> int:
        return max(1, self.stats.enodes)


@dataclass(frozen=True)
class IntentStructuralCacheInfo:
    hits: int
    misses: int
    waits: int
    evictions: int
    entries: int
    total_enodes: int


@dataclass
class _InFlightExploration:
    owner: int
    completed: Event = field(default_factory=Event)
    value: StructuralExplorationRecipe | None = None
    error: BaseException | None = None


class IntentStructuralExplorationCache:
    """Bounded owner of checked, origin-free structural exploration recipes."""

    def __init__(self, *, max_entries: int = 64, max_total_enodes: int = 262_144):
        if max_entries < 1 or max_total_enodes < 1:
            raise ValueError("intent structural cache bounds must be positive")
        self._max_entries = max_entries
        self._max_total_enodes = max_total_enodes
        self._entries: OrderedDict[
            IntentStructuralExplorationKey, StructuralExplorationRecipe
        ] = OrderedDict()
        self._inflight: dict[
            IntentStructuralExplorationKey, _InFlightExploration
        ] = {}
        self._total_enodes = 0
        self._hits = 0
        self._misses = 0
        self._waits = 0
        self._evictions = 0
        self._lock = RLock()

    def get_or_compute(
        self,
        key: IntentStructuralExplorationKey,
        compute: Callable[[], StructuralExplorationRecipe],
    ) -> StructuralExplorationRecipe:
        """Return one recipe, single-flighting concurrent work for ``key``."""

        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._hits += 1
                self._entries.move_to_end(key)
                return cached
            pending = self._inflight.get(key)
            if pending is None:
                pending = _InFlightExploration(get_ident())
                self._inflight[key] = pending
                self._misses += 1
                leader = True
            else:
                if pending.owner == get_ident():
                    raise RuntimeError("recursive intent structural cache computation")
                self._waits += 1
                leader = False

        if not leader:
            pending.completed.wait()
            if pending.error is not None:
                raise pending.error
            if pending.value is None:
                raise RuntimeError("intent structural cache computation produced no value")
            return pending.value

        try:
            value = compute()
        except BaseException as error:
            with self._lock:
                pending.error = error
                self._inflight.pop(key, None)
                pending.completed.set()
            raise

        with self._lock:
            pending.value = value
            if value.weight <= self._max_total_enodes:
                self._entries[key] = value
                self._entries.move_to_end(key)
                self._total_enodes += value.weight
                while (
                    len(self._entries) > self._max_entries
                    or self._total_enodes > self._max_total_enodes
                ):
                    _, evicted = self._entries.popitem(last=False)
                    self._total_enodes -= evicted.weight
                    self._evictions += 1
            self._inflight.pop(key, None)
            pending.completed.set()
        return value

    def info(self) -> IntentStructuralCacheInfo:
        """Return profiler-only counters; they never enter compiler identity."""

        with self._lock:
            return IntentStructuralCacheInfo(
                self._hits,
                self._misses,
                self._waits,
                self._evictions,
                len(self._entries),
                self._total_enodes,
            )

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._total_enodes = 0


def explore_intent_structures(
    root: ir_expr.Expression,
    equivalences: tuple[EquivalenceRule, ...],
    *,
    max_saturation_iterations: int,
    max_eclasses: int,
    max_enodes: int,
    max_raw_extractions: int,
    max_structural_alternatives: int,
    cache: IntentStructuralExplorationCache | None = None,
) -> IntentStructuralExploration:
    """Return source plus bounded, structurally diverse exact alternatives."""

    limits = (
        max_saturation_iterations,
        max_eclasses,
        max_enodes,
        max_raw_extractions,
        max_structural_alternatives,
    )
    if any(limit < 1 for limit in limits):
        raise ValueError("intent structural exploration limits must be positive")

    key = IntentStructuralExplorationKey(
        expression_semantic_identity(root),
        _equivalence_identity(equivalences),
        limits,
    )
    recipe = (
        _compute_recipe(
            root,
            equivalences,
            max_saturation_iterations=max_saturation_iterations,
            max_eclasses=max_eclasses,
            max_enodes=max_enodes,
            max_raw_extractions=max_raw_extractions,
            max_structural_alternatives=max_structural_alternatives,
        )
        if cache is None
        else cache.get_or_compute(
            key,
            lambda: _compute_recipe(
                root,
                equivalences,
                max_saturation_iterations=max_saturation_iterations,
                max_eclasses=max_eclasses,
                max_enodes=max_enodes,
                max_raw_extractions=max_raw_extractions,
                max_structural_alternatives=max_structural_alternatives,
            ),
        )
    )
    return _materialize_recipe(root, equivalences, recipe)


def _compute_recipe(
    root: ir_expr.Expression,
    equivalences: tuple[EquivalenceRule, ...],
    *,
    max_saturation_iterations: int,
    max_eclasses: int,
    max_enodes: int,
    max_raw_extractions: int,
    max_structural_alternatives: int,
) -> StructuralExplorationRecipe:
    inputs = candidate_input_refs(root)
    output_name = "__zlang_explore_result"
    while output_name in inputs:
        output_name += "_"
    ports = tuple(
        Port(PortDirection.INPUT, name, type_)
        for name, type_ in sorted(inputs.items())
    )
    output = Port(PortDirection.OUTPUT, output_name, root.type)
    module = Module(
        "__ZLangExplore",
        (*ports, output),
        (Assignment(output, root),),
        equivalences=equivalences,
    )
    canonical = lower(module)
    saturation = saturate(
        canonical,
        canonical.assignments[0].expression,
        max_iterations=max_saturation_iterations,
        max_terms=max_raw_extractions,
        max_eclasses=max_eclasses,
        max_enodes=max_enodes,
        include_commutative_aliases=False,
    )

    source = _alternative(root, None)
    source_term = _origin_free_term(saturation.original)
    certificates = {
        certificate.selected_identity: certificate
        for certificate in saturation.certificates
    }
    extracted: list[tuple[StructuralAlternative, Term]] = []
    for term in saturation.alternatives:
        origin_free = _origin_free_term(term)
        expression = term_to_expression(origin_free)
        if root.origin is not None:
            expression = replace(expression, origin=root.origin)
        term_identity = sha256(render_term(term).encode()).hexdigest()
        certificate = certificates.get(term_identity)
        if certificate is None:
            # Saturation exposes only independently checked alternatives.  A
            # missing certificate is an invalid engine/product association.
            raise ValueError(
                "exact structural alternative has no checked-value certificate"
            )
        extracted.append((_alternative(expression, certificate), origin_free))

    # The source is mandatory even when an engine spelling shares its physical
    # signature.  For every other family retain the cheapest target-neutral
    # shape, with semantic identity as the final deterministic tie-breaker.
    by_identity: dict[str, StructuralAlternative] = {}
    by_term_identity = {
        alternative.selected_value_identity: term
        for alternative, term in extracted
    }
    for alternative, _ in extracted:
        by_identity.setdefault(alternative.selected_value_identity, alternative)
    by_signature: dict[str, StructuralAlternative] = {}
    for alternative in by_identity.values():
        if alternative.signature.identity == source.signature.identity:
            continue
        previous = by_signature.get(alternative.signature.identity)
        if previous is None or alternative.extraction_rank < previous.extraction_rank:
            by_signature[alternative.signature.identity] = alternative
    diverse = sorted(
        by_signature.values(),
        key=lambda item: item.extraction_rank,
    )
    retained = tuple(diverse[: max_structural_alternatives - 1])
    structural_truncated = len(diverse) + 1 > max_structural_alternatives
    stats = StructuralExplorationStats(
        saturation.iterations,
        saturation.eclass_count,
        saturation.enode_count,
        len(saturation.alternatives) + 1,
        len(diverse) + 1,
        len(retained) + 1,
        saturation.saturated,
        saturation.truncated or structural_truncated,
        saturation.rejection_reasons,
    )
    return StructuralExplorationRecipe(
        source_term,
        tuple(
            StructuralAlternativeRecipe(
                by_term_identity[item.selected_value_identity],
                item.selected_value_identity,
                item.signature,
                item.extraction_rank,
                item.certificate,
            )
            for item in retained
            if item.certificate is not None
        ),
        stats,
    )


def _materialize_recipe(
    root: ir_expr.Expression,
    equivalences: tuple[EquivalenceRule, ...],
    recipe: StructuralExplorationRecipe,
) -> IntentStructuralExploration:
    source = _alternative(root, None)
    alternatives = [source]
    for item in recipe.alternatives:
        replay_checked_alternative(
            recipe.source_term,
            item.term,
            item.certificate,
            equivalences,
            include_commutative_aliases=False,
        )
        expression = term_to_expression(item.term)
        if root.origin is not None:
            expression = replace(expression, origin=root.origin)
        if expression_semantic_identity(expression) != item.selected_value_identity:
            raise ValueError("cached structural alternative identity changed")
        alternatives.append(
            StructuralAlternative(
                expression,
                item.selected_value_identity,
                item.signature,
                item.extraction_rank,
                item.certificate,
            )
        )
    return IntentStructuralExploration(tuple(alternatives), recipe.stats)


def _origin_free_term(term: Term) -> Term:
    return replace(
        term,
        operands=tuple(_origin_free_term(item) for item in term.operands),
        origins=(),
    )


def _equivalence_identity(equivalences: tuple[EquivalenceRule, ...]) -> str:
    return stable_digest(
        {
            "schema": "zlang-intent-equivalence-set-v1",
            "rules": [
                {
                    "name": item.name,
                    "kind": item.kind,
                    "variables": item.variables,
                    "guards": [
                        {
                            "kind": guard.kind.value,
                            "arguments": guard.arguments,
                            "value": guard.value,
                        }
                        for guard in item.guards
                    ],
                    "bindings": item.bindings,
                    "operator": item.operator,
                }
                for item in equivalences
            ],
        }
    )


def structural_signature(expression: ir_expr.Expression) -> StructuralSignature:
    """Describe value-DAG structure without target or source-name facts."""

    graph = ExpressionDagIndex(
        (expression,),
        children=candidate_expression_children,
    )
    operation_counts = Counter(_operation_name(node.expression) for node in graph.nodes)

    def node_shape(
        value: ir_expr.Expression,
        children: tuple[str, ...],
    ) -> str:
        ordered = tuple(sorted(children)) if _commutative(value) else children
        payload: dict[str, object] = {
            "operation": _operation_name(value),
            "type": str(value.type),
            "children": ordered,
        }
        if isinstance(value, ir_expr.Constant):
            payload["constant"] = value.value
        return stable_digest(payload)

    root_shape = graph.root_results(node_shape)[0]
    depth = graph.logic_depth(lambda item: bool(candidate_expression_children(item)))[0]
    counts = tuple(sorted(operation_counts.items()))
    fanout = tuple(sorted(node.fanout for node in graph.nodes))
    identity = stable_digest(
        {
            "schema": "zlang-intent-structural-signature-v1",
            "root": root_shape,
            "operations": counts,
            "unique_nodes": graph.unique_node_count,
            "logic_depth": depth,
            "fanout": fanout,
        }
    )
    return StructuralSignature(
        identity,
        counts,
        graph.unique_node_count,
        depth,
        fanout,
    )


def _alternative(
    expression: ir_expr.Expression,
    certificate: CheckedValueCertificate | None,
) -> StructuralAlternative:
    identity = expression_semantic_identity(expression)
    signature = structural_signature(expression)
    return StructuralAlternative(
        expression,
        identity,
        signature,
        (signature.unique_nodes, signature.logic_depth, identity),
        certificate,
    )


def _operation_name(value: ir_expr.Expression) -> str:
    operation = getattr(value, "operator", None)
    suffix = operation.value if isinstance(operation, Enum) else None
    return type(value).__name__ + ((":" + suffix) if suffix else "")


def _commutative(value: ir_expr.Expression) -> bool:
    if isinstance(value, ir_expr.Add):
        return True
    return isinstance(value, ir_expr.Binary) and value.operator in {
        ir_expr.BinaryOperator.BIT_AND,
        ir_expr.BinaryOperator.BIT_OR,
        ir_expr.BinaryOperator.BIT_XOR,
        ir_expr.BinaryOperator.MULTIPLY,
    }


__all__ = [
    "IntentStructuralExploration",
    "IntentStructuralExplorationCache",
    "IntentStructuralExplorationKey",
    "IntentStructuralCacheInfo",
    "StructuralAlternativeRecipe",
    "StructuralExplorationRecipe",
    "StructuralAlternative",
    "StructuralExplorationStats",
    "StructuralSignature",
    "explore_intent_structures",
    "structural_signature",
]
