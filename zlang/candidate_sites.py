"""Deterministic compiler-owned catalog of retained M28/M39 candidate sites.

Semantic analysis owns candidate *generation* and static M28 ranking.  Formal
execution belongs to compilation selection.  This module records the boundary
between those phases without changing any candidate or proof semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
import json
from typing import Iterable, Mapping

from zlang.common import stable_digest, stable_json
from zlang.costs import (
    candidate_cost_from_alternative,
    extract_best,
)
from zlang.exploration import ExplorationResult
from zlang.ir import expressions as expr
from zlang.ir.cdc import ClockDomain, PowerUpPolicy
from zlang.ir.hierarchy import candidate_specialization_identity
from zlang.ir.module import Module
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.source import SourceOrigin, SourceSpan


CANDIDATE_SITE_LEDGER_SCHEMA = 1


class CandidateSiteError(ValueError):
    """A retained candidate-site catalog is malformed or ambiguous."""


def module_candidate_owner_identity(module: Module) -> str:
    """Return the candidate owner encoded by a concrete typed module."""

    return candidate_specialization_identity(module.name, module.parameters)


def candidate_owner_formal_domain(
    module: Module,
    owner_identity: str | None,
) -> tuple[ClockDomain | None, str | None]:
    """Resolve the exact physical domain of one retained candidate owner.

    Candidate execution is intentionally delayed until after semantic analysis,
    so a verifier must recover its domain from the typed module tree rather than
    fabricate the historical ``clock/reset`` pair.  A callable-owned
    implementation region is associated with the module that publishes that
    monomorphic definition.

    Zero-domain combinational candidates retain the existing same-cycle route.
    Multi-domain owners are outside the frozen M36/M39 subset and therefore fail
    closed before a verifier can be constructed.
    """

    matches: list[Module] = []

    def visit(current: Module) -> None:
        module_owner = module_candidate_owner_identity(current)
        owns_callable = False
        if owner_identity is not None and owner_identity.startswith("callable:"):
            callee = owner_identity.removeprefix("callable:")
            owns_callable = any(
                item.callee_identity == callee
                for item in (*current.functions, *current.callable_definitions)
            )
        if (
            owner_identity is None
            or owner_identity == "<anonymous-module>"
            or owner_identity == module_owner
            or owns_callable
        ):
            matches.append(current)
        for child in current.children:
            visit(child)

    visit(module)
    # An anonymous result belongs to the root rather than every descendant.
    if owner_identity in {None, "<anonymous-module>"}:
        matches = [module]
    if len(matches) != 1:
        raise CandidateSiteError(
            "formal candidate owner does not resolve to one typed module: "
            f"{owner_identity or '<anonymous-module>'}"
        )
    domains = matches[0].clock_domains
    if not domains:
        return None, None
    if len(domains) != 1:
        return None, (
            "M36/M39 candidate equivalence requires exactly one physical "
            "clock/reset domain per candidate owner"
        )
    domain = domains[0]
    if domain.power_up is not PowerUpPolicy.UNSPECIFIED:
        return None, (
            "M36/M39 candidate equivalence does not support power_up reset "
            "semantics"
        )
    return domain, None


def candidate_owner_clock_domain(
    module: Module,
    owner_identity: str | None,
) -> ClockDomain | None:
    """Return one executable owner domain, rejecting an unsupported owner."""

    domain, limitation = candidate_owner_formal_domain(module, owner_identity)
    if limitation is not None:
        raise CandidateSiteError(limitation)
    return domain


class CandidateSiteKind(str, Enum):
    IMPLEMENT = "implement"
    SOURCE_EXPLORE = "source_explore"
    EXPRESSION_EXPLORE = "expression_explore"
    CHOICE_AUTO = "choice_auto"
    ARCHITECTURE_AUTO = "architecture_auto"
    STANDALONE_PIPELINE = "standalone_pipeline"
    ELASTIC_PIPELINE = "elastic_pipeline"
    EXTERNAL_PROFILE = "external_profile"


class CandidateRewriteKind(str, Enum):
    OUTPUT_ASSIGNMENT = "output_assignment"
    EXPRESSION_IDENTITY = "expression_identity"
    STATIC_ONLY = "static_only"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class _M39Candidate:
    """Small adapter from retained M29/M32 records to the frozen M39 gate."""

    expression: object
    semantic_identity: str
    implementation_identity: str
    cost: object
    candidate_class: str
    stages: tuple[str, ...]


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CandidateSiteError(f"{label} must be a non-empty string")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise CandidateSiteError(f"{label} must be an object with string keys")
    return value  # type: ignore[return-value]


def _exact_keys(
    data: Mapping[str, object], expected: set[str], label: str
) -> None:
    if set(data) != expected:
        raise CandidateSiteError(
            f"{label} fields differ: expected {sorted(expected)}, "
            f"got {sorted(data)}"
        )


def _json_key(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, tuple):
        return [_json_key(item) for item in value]
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int, float, bool)):
        return enum_value
    raise CandidateSiteError(
        f"candidate objective key contains unsupported {type(value).__name__}"
    )


def _origin_from_data(value: object) -> SourceOrigin | SourceSpan | None:
    if value is None:
        return None
    try:
        data = _mapping(value, "candidate-site origin")
        if set(data) == {
            "start_line", "start_column", "end_line", "end_column",
        }:
            return SourceSpan.from_data(data)
        return SourceOrigin.from_data(data)
    except ValueError as error:
        raise CandidateSiteError(str(error)) from error


@dataclass(frozen=True)
class CandidateRankRecord:
    candidate_identity: str
    semantic_identity: str
    rank: int
    objective_key: tuple[object, ...]

    def __post_init__(self) -> None:
        _require_string(self.candidate_identity, "candidate identity")
        _require_string(self.semantic_identity, "candidate semantic identity")
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 1:
            raise CandidateSiteError("candidate rank must be a positive integer")
        if not isinstance(self.objective_key, tuple):
            raise CandidateSiteError("candidate objective key must be a tuple")
        _json_key(self.objective_key)

    def to_data(self) -> dict[str, object]:
        return {
            "candidate_identity": self.candidate_identity,
            "semantic_identity": self.semantic_identity,
            "rank": self.rank,
            "objective_key": _json_key(self.objective_key),
        }

    @classmethod
    def from_data(cls, value: object) -> "CandidateRankRecord":
        data = _mapping(value, "candidate rank record")
        _exact_keys(
            data,
            {"candidate_identity", "semantic_identity", "rank", "objective_key"},
            "candidate rank record",
        )
        key = data["objective_key"]
        if not isinstance(key, list):
            raise CandidateSiteError("candidate objective key must be an array")
        rank = data["rank"]
        if isinstance(rank, bool) or not isinstance(rank, int):
            raise CandidateSiteError("candidate rank must be an integer")
        return cls(
            _require_string(data["candidate_identity"], "candidate identity"),
            _require_string(
                data["semantic_identity"], "candidate semantic identity"
            ),
            rank,
            tuple(key),
        )


@dataclass(frozen=True)
class CandidateSiteRecord:
    kind: CandidateSiteKind
    owner_identity: str
    output: str | None
    source_semantic_identity: str
    selected_candidate_identity: str
    candidates: tuple[CandidateRankRecord, ...]
    rewrite_kind: CandidateRewriteKind
    blocker: str | None = None
    source_origin: SourceOrigin | SourceSpan | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "kind", CandidateSiteKind(self.kind))
            object.__setattr__(
                self, "rewrite_kind", CandidateRewriteKind(self.rewrite_kind)
            )
        except ValueError as error:
            raise CandidateSiteError(str(error)) from error
        _require_string(self.owner_identity, "candidate-site owner")
        if self.output is not None:
            _require_string(self.output, "candidate-site output")
        _require_string(
            self.source_semantic_identity, "candidate-site source identity"
        )
        _require_string(
            self.selected_candidate_identity, "selected candidate identity"
        )
        if not isinstance(self.candidates, tuple) or not self.candidates:
            raise CandidateSiteError("candidate site must retain ranked candidates")
        ranks = tuple(item.rank for item in self.candidates)
        if ranks != tuple(range(1, len(ranks) + 1)):
            raise CandidateSiteError("candidate ranks must be contiguous and ordered")
        identities = tuple(item.candidate_identity for item in self.candidates)
        if len(identities) != len(set(identities)):
            raise CandidateSiteError("candidate identities must be unique per site")
        if self.selected_candidate_identity not in identities:
            raise CandidateSiteError("selected candidate is absent from its site")
        if self.rewrite_kind is CandidateRewriteKind.OUTPUT_ASSIGNMENT:
            if self.output is None:
                raise CandidateSiteError("output rewrite requires an output name")
        if self.rewrite_kind in {
            CandidateRewriteKind.STATIC_ONLY,
            CandidateRewriteKind.UNSUPPORTED,
        }:
            _require_string(self.blocker, "candidate-site blocker")
        elif self.blocker is not None:
            raise CandidateSiteError("gateable candidate site cannot carry a blocker")
        if self.source_origin is not None and not isinstance(
            self.source_origin, (SourceOrigin, SourceSpan)
        ):
            raise CandidateSiteError(
                "candidate-site origin must be a SourceOrigin, SourceSpan, or null"
            )

    @property
    def identity(self) -> str:
        return "candidate-site:" + stable_digest({
            "schema": CANDIDATE_SITE_LEDGER_SCHEMA,
            "kind": self.kind.value,
            "owner": self.owner_identity,
            "output": self.output,
            "source": self.source_semantic_identity,
            "candidates": [
                item.candidate_identity for item in self.candidates
            ],
        })

    def to_data(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "kind": self.kind.value,
            "owner_identity": self.owner_identity,
            "output": self.output,
            "source_semantic_identity": self.source_semantic_identity,
            "selected_candidate_identity": self.selected_candidate_identity,
            "candidates": [item.to_data() for item in self.candidates],
            "rewrite_kind": self.rewrite_kind.value,
            "blocker": self.blocker,
            "source_origin": (
                None if self.source_origin is None else self.source_origin.to_data()
            ),
        }

    @classmethod
    def from_data(cls, value: object) -> "CandidateSiteRecord":
        data = _mapping(value, "candidate site")
        _exact_keys(
            data,
            {
                "identity", "kind", "owner_identity", "output",
                "source_semantic_identity", "selected_candidate_identity",
                "candidates", "rewrite_kind", "blocker", "source_origin",
            },
            "candidate site",
        )
        candidates = data["candidates"]
        if not isinstance(candidates, list):
            raise CandidateSiteError("candidate-site candidates must be an array")
        output = data["output"]
        blocker = data["blocker"]
        if output is not None and not isinstance(output, str):
            raise CandidateSiteError("candidate-site output must be a string or null")
        if blocker is not None and not isinstance(blocker, str):
            raise CandidateSiteError("candidate-site blocker must be a string or null")
        try:
            item = cls(
                CandidateSiteKind(_require_string(data["kind"], "candidate-site kind")),
                _require_string(data["owner_identity"], "candidate-site owner"),
                output,
                _require_string(
                    data["source_semantic_identity"], "candidate-site source identity"
                ),
                _require_string(
                    data["selected_candidate_identity"],
                    "selected candidate identity",
                ),
                tuple(CandidateRankRecord.from_data(value) for value in candidates),
                CandidateRewriteKind(_require_string(
                    data["rewrite_kind"], "candidate rewrite kind"
                )),
                blocker,
                _origin_from_data(data["source_origin"]),
            )
        except ValueError as error:
            raise CandidateSiteError(str(error)) from error
        if data["identity"] != item.identity:
            raise CandidateSiteError("candidate-site identity does not match payload")
        return item


@dataclass(frozen=True)
class CandidateSiteLedger:
    sites: tuple[CandidateSiteRecord, ...]
    schema_version: int = CANDIDATE_SITE_LEDGER_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != CANDIDATE_SITE_LEDGER_SCHEMA:
            raise CandidateSiteError("unsupported candidate-site ledger schema")
        if not isinstance(self.sites, tuple):
            raise CandidateSiteError("candidate-site ledger sites must be a tuple")
        identities = tuple(item.identity for item in self.sites)
        if identities != tuple(sorted(identities)):
            raise CandidateSiteError("candidate sites must be identity ordered")
        if len(identities) != len(set(identities)):
            raise CandidateSiteError("candidate-site identities must be unique")

    @property
    def identity(self) -> str:
        return "candidate-ledger:" + stable_digest(self.to_identity_data())

    def to_identity_data(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            # Origins are attribution, not candidate-site semantics.  The
            # individual site identity already uses only owner/source/candidate
            # semantic identities; keep the enclosing ledger equally stable
            # when a source declaration merely moves between lines.
            "sites": [
                {
                    key: value
                    for key, value in item.to_data().items()
                    if key != "source_origin"
                }
                for item in self.sites
            ],
        }

    def to_data(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "sites": [item.to_data() for item in self.sites],
            "identity": self.identity,
        }

    def to_json(self) -> str:
        return stable_json(self.to_data())

    @classmethod
    def from_data(cls, value: object) -> "CandidateSiteLedger":
        data = _mapping(value, "candidate-site ledger")
        _exact_keys(data, {"schema_version", "sites", "identity"}, "candidate-site ledger")
        version = data["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise CandidateSiteError("candidate-site schema version must be an integer")
        sites = data["sites"]
        if not isinstance(sites, list):
            raise CandidateSiteError("candidate-site ledger sites must be an array")
        ledger = cls(
            tuple(CandidateSiteRecord.from_data(item) for item in sites),
            version,
        )
        if data["identity"] != ledger.identity:
            raise CandidateSiteError("candidate-site ledger identity does not match payload")
        return ledger

    @classmethod
    def from_json(cls, payload: str) -> "CandidateSiteLedger":
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as error:
            raise CandidateSiteError("candidate-site ledger is not valid JSON") from error
        return cls.from_data(value)


@dataclass(frozen=True)
class SelectedCandidateSite:
    """One selected concrete candidate and its exact semantic reference.

    The ledger intentionally serializes identities only.  Compiler-owned M36
    preparation also needs the already-typed expressions, so this ephemeral
    view joins those objects to the exact site using the same constructors as
    :func:`build_candidate_site_ledger`.  It is never serialized or inferred
    from generated names.
    """

    site: CandidateSiteRecord
    candidate: object
    reference_expression: object
    candidate_class: str

    def __post_init__(self) -> None:
        if getattr(self.candidate, "implementation_identity", None) != (
            self.site.selected_candidate_identity
        ):
            raise CandidateSiteError(
                "selected candidate object does not match its exact site"
            )
        _require_string(self.candidate_class, "selected candidate class")


def _candidate_identity(schema: str, source: str, name: str, expression: object) -> str:
    return stable_digest({
        "schema": schema,
        "source": source,
        "name": name,
        "expression": expression_semantic_identity(expression),
    })


def _rank_records(evaluations: Iterable[object]) -> tuple[CandidateRankRecord, ...]:
    ranked = sorted(
        (item for item in evaluations if item.legal),
        key=lambda item: item.objective_key,
    )
    return tuple(
        CandidateRankRecord(
            str(item.candidate.implementation_identity),
            str(item.candidate.semantic_identity),
            rank,
            tuple(_json_key(item.objective_key)),  # type: ignore[arg-type]
        )
        for rank, item in enumerate(ranked, 1)
    )


def _exploration_site(result: ExplorationResult) -> CandidateSiteRecord:
    try:
        kind = CandidateSiteKind(result.site_kind)
    except ValueError as error:
        raise CandidateSiteError(
            f"unknown retained exploration site kind '{result.site_kind}'"
        ) from error
    candidates = _rank_records(result.extraction.evaluations)
    rewrite = (
        CandidateRewriteKind.OUTPUT_ASSIGNMENT
        if result.site_output is not None
        else CandidateRewriteKind.EXPRESSION_IDENTITY
    )
    return CandidateSiteRecord(
        kind,
        result.site_owner or "<anonymous-module>",
        result.site_output,
        result.source_semantic_identity,
        result.selected_candidate.implementation_identity,
        candidates,
        rewrite,
        source_origin=result.request.source_origin,
    )


def _pipeline_site(
    module: Module,
    pipeline: object,
    *,
    elastic: bool = False,
    owner_identity: str | None = None,
):
    from zlang.formal_candidate import standalone_pipeline_candidate_space

    _, _, extraction = standalone_pipeline_candidate_space(pipeline)
    candidates = _rank_records(extraction.evaluations)
    selected_name = getattr(pipeline, "selected")
    wrapped = tuple(
        item for item in extraction.evaluations
        if item.candidate.pipeline_candidate.name == selected_name
    )
    if len(wrapped) != 1:
        raise CandidateSiteError("retained pipeline selected candidate is ambiguous")
    return CandidateSiteRecord(
        (
            CandidateSiteKind.ELASTIC_PIPELINE
            if elastic else CandidateSiteKind.IMPLEMENT
        ),
        owner_identity or module_candidate_owner_identity(module),
        (
            getattr(pipeline, "destination_endpoint")
            if elastic else getattr(pipeline, "output")
        ),
        expression_semantic_identity(pipeline.source_expression),
        wrapped[0].candidate.implementation_identity,
        candidates,
        (
            CandidateRewriteKind.UNSUPPORTED
            if elastic else CandidateRewriteKind.OUTPUT_ASSIGNMENT
        ),
        (
            "variable-latency elastic pipelines have no M36 candidate route"
            if elastic else None
        ),
        getattr(pipeline, "source_origin", None)
        or getattr(pipeline.source_expression, "origin", None),
    )


def candidate_site_key(
    owner_identity: str | None,
    output: str | None,
    source_identity: str,
) -> tuple[str, str | None, str]:
    """Return the shared typed key used to join one implementation region.

    Site classification is semantic bookkeeping, not source provenance.  The
    key intentionally contains only the monomorphic owner, output boundary,
    and origin-insensitive typed source identity.
    """

    return (owner_identity or "<anonymous-module>", output, source_identity)


def exploration_site_key(result: ExplorationResult) -> tuple[str, str | None, str]:
    """Key a unified exploration result without consulting source origin."""

    return candidate_site_key(
        result.site_owner,
        result.site_output,
        expression_semantic_identity(result.request.root),
    )


def pipeline_site_key(
    module: Module,
    pipeline: object,
) -> tuple[str, str | None, str]:
    """Key retained pipeline metadata using the same typed site contract."""

    return candidate_site_key(
        module_candidate_owner_identity(module),
        getattr(pipeline, "output", None),
        expression_semantic_identity(pipeline.source_expression),
    )


def unified_implementation_site_keys(
    results: Iterable[ExplorationResult],
) -> set[tuple[str, str | None, str]]:
    """Collect canonical implementation owners from retained results."""

    return {
        exploration_site_key(result)
        for result in results
        if result.site_kind == CandidateSiteKind.IMPLEMENT.value
    }


def _pipeline_is_canonical_implement(
    module: Module,
    pipeline: object,
    unified_site_keys: set[tuple[str, str | None, str]],
) -> bool:
    """Return whether a pipeline record is derived metadata for ``implement``.

    Canonical implementation regions retain a typed ``PipelineExploration`` so
    target planning and reports can inspect pipeline candidates.  The unified
    M34 exploration result is nevertheless the one public M39/M36 candidate
    site.  Older persisted modules may contain a standalone pipeline record;
    those remain formal sites when no matching unified result exists.
    """

    return pipeline_site_key(module, pipeline) in unified_site_keys


def _architecture_site(
    module: Module,
    architecture: object,
    *,
    owner_identity: str | None = None,
) -> CandidateSiteRecord:
    wrapped, extraction = _architecture_candidate_space(architecture)
    ranks = _rank_records(extraction.evaluations)
    source = expression_semantic_identity(architecture.source_expression)
    selected_identity = next(
        item.implementation_identity for item in wrapped
        if item.stages[-1] == architecture.selected
    )
    return CandidateSiteRecord(
        CandidateSiteKind.ARCHITECTURE_AUTO,
        owner_identity or module_candidate_owner_identity(module),
        architecture.output,
        source,
        selected_identity,
        ranks,
        CandidateRewriteKind.OUTPUT_ASSIGNMENT,
        source_origin=architecture.source_expression.origin,
    )


def _choice_site(
    module: Module,
    assignment: object,
    *,
    owner_identity: str | None = None,
) -> CandidateSiteRecord:
    choice = assignment.expression
    wrapped, extraction = _choice_candidate_space(choice)
    source = stable_digest({
        "schema": "zlang-choice-source-v1",
        "alternatives": [
            expression_semantic_identity(item.expression)
            for item in choice.alternatives
        ],
    })
    selected_identity = (
        extraction.selected.implementation_identity
        if choice.selected is None
        else next(
            item.implementation_identity for item in wrapped
            if item.stages[-1] == choice.selected.value
        )
    )
    return CandidateSiteRecord(
        CandidateSiteKind.CHOICE_AUTO,
        owner_identity or module_candidate_owner_identity(module),
        assignment.target.name,
        source,
        selected_identity,
        _rank_records(extraction.evaluations),
        CandidateRewriteKind.OUTPUT_ASSIGNMENT,
        source_origin=choice.origin,
    )


def _choice_candidate_space(choice: object):
    policy = choice.cost_policy
    if policy is None:
        raise CandidateSiteError("explicit implementation choice is not an M39 site")
    source = stable_digest({
        "schema": "zlang-choice-source-v1",
        "alternatives": [
            expression_semantic_identity(item.expression)
            for item in choice.alternatives
        ],
    })
    wrapped = tuple(
        _M39Candidate(
            alternative.expression,
            source,
            _candidate_identity(
                "zlang-choice-ledger-candidate-v1",
                source,
                alternative.kind.value,
                alternative.expression,
            ),
            candidate_cost_from_alternative(alternative),
            "m29",
            ("choice_auto", alternative.kind.value),
        )
        for alternative in choice.alternatives
    )
    by_kind = {
        item.kind: wrapped[index]
        for index, item in enumerate(choice.alternatives)
    }
    extraction = extract_best(
        choice.alternatives,
        objective=policy.goal,
        constraints=policy.constraints,
        cost_fn=candidate_cost_from_alternative,
    )
    adapted = replace(
        extraction,
        selected=by_kind[extraction.selected.kind],
        evaluations=tuple(
            replace(item, candidate=by_kind[item.candidate.kind])
            for item in extraction.evaluations
        ),
    )
    return wrapped, adapted


def _architecture_candidate_space(architecture: object):
    from zlang.architectures import _architecture_cost

    source = expression_semantic_identity(architecture.source_expression)
    wrapped = tuple(
        _M39Candidate(
            candidate.expression,
            source,
            _candidate_identity(
                "zlang-architecture-ledger-candidate-v1",
                source,
                candidate.name,
                candidate.expression,
            ),
            _architecture_cost(candidate),
            "m32",
            ("architecture_auto", candidate.name),
        )
        for candidate in architecture.candidates
    )
    by_name = {
        item.name: wrapped[index]
        for index, item in enumerate(architecture.candidates)
    }
    extraction = extract_best(
        tuple(item for item in architecture.candidates if item.legal),
        objective=expr.CostMetric.FMAX_EST,
        cost_fn=_architecture_cost,
    )
    adapted = replace(
        extraction,
        selected=by_name[extraction.selected.name],
        evaluations=tuple(
            replace(item, candidate=by_name[item.candidate.name])
            for item in extraction.evaluations
        ),
    )
    return wrapped, adapted


def build_candidate_site_ledger(
    module: Module,
    exploration_results: Iterable[ExplorationResult] = (),
) -> CandidateSiteLedger:
    """Catalog every retained frozen candidate entry point exactly once."""

    retained_results = tuple(exploration_results)
    sites = [_exploration_site(item) for item in retained_results]
    unified_site_keys = unified_implementation_site_keys(retained_results)

    def visit(current: Module) -> None:
        owner_identity = module_candidate_owner_identity(current)
        for assignment in current.assignments:
            if (
                assignment.signal is None
                and assignment.channel is None
                and isinstance(assignment.expression, expr.ImplementationChoice)
                and assignment.expression.cost_policy is not None
            ):
                sites.append(_choice_site(
                    current, assignment, owner_identity=owner_identity
                ))
        sites.extend(
            _architecture_site(current, item, owner_identity=owner_identity)
            for item in current.architecture_explorations
        )
        sites.extend(
            _pipeline_site(current, item, owner_identity=owner_identity)
            for item in current.pipeline_explorations
            if not _pipeline_is_canonical_implement(
                current, item, unified_site_keys
            )
        )
        sites.extend(
            _pipeline_site(
                current, item, elastic=True, owner_identity=owner_identity
            )
            for item in current.elastic_pipeline_regions
        )
        for child in current.children:
            visit(child)

    visit(module)
    by_identity: dict[str, CandidateSiteRecord] = {}
    for site in sites:
        existing = by_identity.get(site.identity)
        if existing is not None and existing != site:
            raise CandidateSiteError(
                f"candidate site '{site.identity}' has conflicting retained records"
            )
        by_identity[site.identity] = site
    return CandidateSiteLedger(tuple(sorted(by_identity.values(), key=lambda item: item.identity)))


def selected_candidate_sites(
    module: Module,
    exploration_results: Iterable[ExplorationResult] = (),
) -> tuple[SelectedCandidateSite, ...]:
    """Return typed selected candidates joined to exact retained sites.

    Only the frozen M36-eligible scalar candidate entry points are returned.
    Elastic/static/unsupported sites remain represented in
    :class:`CandidateSiteLedger` but deliberately have no executable candidate
    equivalence view.
    """

    selected: list[SelectedCandidateSite] = []
    unified_site_keys: set[tuple[str, str | None, str]] = set()

    for exploration in exploration_results:
        from zlang.formal_candidate import candidate_equivalence_class

        site = _exploration_site(exploration)
        candidate = exploration.selected_candidate
        selected.append(SelectedCandidateSite(
            site,
            candidate,
            exploration.request.root,
            candidate_equivalence_class(candidate),
        ))
        if site.kind is CandidateSiteKind.IMPLEMENT:
            unified_site_keys.add(exploration_site_key(exploration))

    def chosen(
        site: CandidateSiteRecord,
        candidates: tuple[object, ...],
        reference: object,
        candidate_class: str,
    ) -> None:
        matches = tuple(
            item for item in candidates
            if getattr(item, "implementation_identity", None)
            == site.selected_candidate_identity
        )
        if len(matches) != 1:
            raise CandidateSiteError(
                f"selected candidate for site '{site.identity}' is ambiguous"
            )
        selected.append(SelectedCandidateSite(
            site, matches[0], reference, candidate_class
        ))

    def visit(current: Module) -> None:
        owner_identity = module_candidate_owner_identity(current)
        for assignment in current.assignments:
            choice = assignment.expression
            if (
                assignment.signal is None
                and assignment.channel is None
                and isinstance(choice, expr.ImplementationChoice)
                and choice.cost_policy is not None
            ):
                site = _choice_site(
                    current, assignment, owner_identity=owner_identity
                )
                wrapped, _ = _choice_candidate_space(choice)
                chosen(site, wrapped, choice.alternatives[0].expression, "m29")
        for architecture in current.architecture_explorations:
            site = _architecture_site(
                current, architecture, owner_identity=owner_identity
            )
            wrapped, _ = _architecture_candidate_space(architecture)
            chosen(site, wrapped, architecture.source_expression, "m32")
        for pipeline in current.pipeline_explorations:
            if _pipeline_is_canonical_implement(
                current, pipeline, unified_site_keys
            ):
                continue
            from zlang.formal_candidate import standalone_pipeline_candidate_space

            site = _pipeline_site(
                current, pipeline, owner_identity=owner_identity
            )
            wrapped, _, _ = standalone_pipeline_candidate_space(pipeline)
            chosen(site, wrapped, pipeline.source_expression, "m31")
        for child in current.children:
            visit(child)

    visit(module)
    by_site: dict[str, SelectedCandidateSite] = {}
    for item in selected:
        previous = by_site.get(item.site.identity)
        if previous is not None and previous != item:
            raise CandidateSiteError(
                f"selected candidate site '{item.site.identity}' is inconsistent"
            )
        by_site[item.site.identity] = item
    return tuple(by_site[key] for key in sorted(by_site))


def _replace_exact_expression(value: object, old: object, new: object) -> tuple[object, int]:
    """Replace one retained typed-expression object without structural guessing."""

    if value is old:
        return new, 1
    if isinstance(value, tuple):
        changed = 0
        items = []
        for item in value:
            updated, count = _replace_exact_expression(item, old, new)
            items.append(updated)
            changed += count
        return (tuple(items), changed) if changed else (value, 0)
    if not is_dataclass(value) or isinstance(value, type):
        return value, 0
    updates: dict[str, object] = {}
    changed = 0
    for field in fields(value):
        if not field.init or field.name in {"origin", "source_origin"}:
            continue
        current = getattr(value, field.name)
        updated, count = _replace_exact_expression(current, old, new)
        if count:
            updates[field.name] = updated
            changed += count
    if not changed:
        return value, 0
    try:
        return replace(value, **updates), changed
    except (TypeError, ValueError) as error:
        raise CandidateSiteError(
            "retained expression-local candidate cannot be rewritten without "
            f"changing semantic IR: {error}"
        ) from error


def _replace_output_expression(
    module: Module,
    *,
    owner: str | None,
    output: str,
    expression: object,
) -> tuple[Module, int]:
    assignments = list(module.assignments)
    changed = 0
    if owner is None or module_candidate_owner_identity(module) == owner:
        matches = tuple(
            index for index, assignment in enumerate(assignments)
            if assignment.signal is None
            and assignment.channel is None
            and assignment.target.name == output
        )
        if len(matches) > 1:
            raise CandidateSiteError(
                "candidate output rewrite "
                f"'{module_candidate_owner_identity(module)}.{output}' is ambiguous"
            )
        if matches:
            index = matches[0]
            assignments[index] = replace(assignments[index], expression=expression)
            changed += 1
    children = []
    for child in module.children:
        updated, count = _replace_output_expression(
            child,
            owner=owner,
            output=output,
            expression=expression,
        )
        children.append(updated)
        changed += count
    return replace(
        module,
        assignments=tuple(assignments),
        children=tuple(children),
    ), changed


def _replace_callable_body(
    module: Module,
    *,
    callee_identity: str,
    expression: object,
) -> tuple[Module, int]:
    """Replace one retained monomorphic callable body by exact identity."""

    functions = list(module.functions)
    definitions = list(module.callable_definitions)
    changed = 0
    for collection in (functions, definitions):
        matches = tuple(
            index for index, function in enumerate(collection)
            if function.callee_identity == callee_identity
        )
        if len(matches) > 1:
            raise CandidateSiteError(
                f"callable candidate rewrite '{callee_identity}' is ambiguous"
            )
        if matches:
            index = matches[0]
            collection[index] = replace(collection[index], body=expression)
            changed += 1
    children = []
    for child in module.children:
        updated, count = _replace_callable_body(
            child,
            callee_identity=callee_identity,
            expression=expression,
        )
        children.append(updated)
        changed += count
    return replace(
        module,
        functions=tuple(functions),
        callable_definitions=tuple(definitions),
        children=tuple(children),
    ), changed


def gate_retained_explorations(
    module: Module,
    results: Iterable[ExplorationResult],
    config: object,
    verifier: object | None = None,
    *,
    backend: str = "clash",
) -> tuple[Module, tuple[ExplorationResult, ...]]:
    """Apply the existing M39 gate after semantic typing to retained explores."""

    from zlang.formal_candidate import (
        M36ClashCandidateVerifier,
        M36DirectSystemVerilogCandidateVerifier,
    )
    from zlang.formal_exploration import FormalPolicy, gate_candidates

    retained = tuple(results)
    if config.policy is FormalPolicy.OFF:
        return module, retained
    updated_module = module
    updated_results: list[ExplorationResult] = []
    processed: dict[str, ExplorationResult] = {}
    for result in retained:
        site = _exploration_site(result)
        previous = processed.get(site.identity)
        if previous is not None:
            updated_results.append(previous)
            continue
        domain, domain_limitation = candidate_owner_formal_domain(
            module, result.site_owner
        )
        selected_verifier = (
            verifier
            if verifier is not None and domain_limitation is None
            else (
                M36DirectSystemVerilogCandidateVerifier
                if backend == "direct_systemverilog"
                else M36ClashCandidateVerifier
            )(
                result.request.root,
                artifact_provider=getattr(config, "artifact_provider", None),
                clock_domain_contract=domain,
                unavailable_reason=domain_limitation,
            )
        )
        gate = gate_candidates(
            result.generated_candidates,
            result.extraction.evaluations,
            config,
            selected_verifier,
        )
        extraction = extract_best(
            gate.eligible,
            objective=result.request.objective,
            constraints=result.request.constraints,
            source_policy=result.request.source_policy,
            cost_fn=lambda item: item.cost,
        )
        gated = replace(
            result,
            request=replace(
                result.request,
                formal_config=config,
                # Verifiers are execution resources, never retained semantic
                # or report identities.
                formal_verifier=None,
            ),
            selected_candidate=extraction.selected,
            extraction=replace(
                result.extraction,
                selected=extraction.selected,
                selected_cost=extraction.selected_cost,
                reason=extraction.reason,
            ),
            formal_records=gate.records,
        )
        if result.site_output is not None:
            updated_module, replacements = _replace_output_expression(
                updated_module,
                owner=result.site_owner,
                output=result.site_output,
                expression=extraction.selected.expression,
            )
        elif (result.site_owner or "").startswith("callable:"):
            updated_module, replacements = _replace_callable_body(
                updated_module,
                callee_identity=result.site_owner.removeprefix("callable:"),
                expression=extraction.selected.expression,
            )
        else:
            updated, replacements = _replace_exact_expression(
                updated_module,
                result.selected_candidate.expression,
                extraction.selected.expression,
            )
            if not isinstance(updated, Module):
                raise CandidateSiteError("candidate rewrite did not preserve module IR")
            updated_module = updated
        if replacements < 1:
            locator = (
                f"{result.site_owner or '<anonymous>'}.{result.site_output}"
                if result.site_output is not None
                else "expression-local retained object"
            )
            raise CandidateSiteError(
                f"candidate site '{site.identity}' cannot be rewritten at {locator}; "
                "the current semantic IR did not retain its exact selection boundary"
            )
        processed[site.identity] = gated
        updated_results.append(gated)
    return updated_module, tuple(updated_results)


def gate_structured_candidate_sites(
    module: Module,
    config: object,
    verifier: object | None = None,
    *,
    backend: str = "clash",
) -> Module:
    """Gate retained choice/architecture candidate records.

    ``choice`` remains a source construct, while architecture records can be
    restored from older semantic/evidence products. Both retain an exact typed
    candidate table and output boundary; adapt them to the same frozen M39 rank
    gate and keep evidence outside canonical RTL identity.
    """

    from zlang.formal_candidate import (
        M36ClashCandidateVerifier,
        M36DirectSystemVerilogCandidateVerifier,
    )
    from zlang.formal_exploration import FormalPolicy, gate_candidates

    if config.policy is FormalPolicy.OFF:
        return module

    assignments = list(module.assignments)
    for index, assignment in enumerate(assignments):
        choice = assignment.expression
        if not (
            assignment.signal is None
            and assignment.channel is None
            and isinstance(choice, expr.ImplementationChoice)
            and choice.cost_policy is not None
        ):
            continue
        wrapped, extraction = _choice_candidate_space(choice)
        domain, domain_limitation = candidate_owner_formal_domain(
            module, module_candidate_owner_identity(module)
        )
        selected_verifier = (
            verifier
            if verifier is not None and domain_limitation is None
            else (
                M36DirectSystemVerilogCandidateVerifier
                if backend == "direct_systemverilog"
                else M36ClashCandidateVerifier
            )(
                choice.alternatives[0].expression,
                candidate_class="m29",
                artifact_provider=getattr(config, "artifact_provider", None),
                clock_domain_contract=domain,
                unavailable_reason=domain_limitation,
            )
        )
        gate = gate_candidates(
            wrapped,
            extraction.evaluations,
            config,
            selected_verifier,
        )
        selected = extract_best(
            gate.eligible,
            objective=choice.cost_policy.goal,
            constraints=choice.cost_policy.constraints,
            cost_fn=lambda item: item.cost,
        ).selected
        selected_kind = expr.ImplementationKind(selected.stages[-1])
        assignments[index] = replace(
            assignment,
            expression=replace(
                choice,
                selected=selected_kind,
                formal_records=gate.records,
                formal_eligible=tuple(
                    expr.ImplementationKind(item.stages[-1])
                    for item in gate.eligible
                ),
            ),
        )

    architectures = []
    for architecture in module.architecture_explorations:
        wrapped, extraction = _architecture_candidate_space(architecture)
        domain, domain_limitation = candidate_owner_formal_domain(
            module, module_candidate_owner_identity(module)
        )
        selected_verifier = (
            verifier
            if verifier is not None and domain_limitation is None
            else (
                M36DirectSystemVerilogCandidateVerifier
                if backend == "direct_systemverilog"
                else M36ClashCandidateVerifier
            )(
                architecture.source_expression,
                candidate_class="m32",
                artifact_provider=getattr(config, "artifact_provider", None),
                clock_domain_contract=domain,
                unavailable_reason=domain_limitation,
            )
        )
        gate = gate_candidates(
            wrapped,
            extraction.evaluations,
            config,
            selected_verifier,
        )
        selected = extract_best(
            gate.eligible,
            objective=expr.CostMetric.FMAX_EST,
            cost_fn=lambda item: item.cost,
        ).selected
        selected_name = selected.stages[-1]
        architectures.append(replace(
            architecture,
            selected=selected_name,
            formal_records=gate.records,
        ))
        matches = tuple(
            assignment_index
            for assignment_index, assignment in enumerate(assignments)
            if assignment.signal is None
            and assignment.channel is None
            and assignment.target.name == architecture.output
        )
        if len(matches) != 1:
            raise CandidateSiteError(
                "architecture(auto) formal gate requires one exact output assignment"
            )
        selected_expression = next(
            item.expression for item in architecture.candidates
            if item.name == selected_name
        )
        assignment_index = matches[0]
        assignments[assignment_index] = replace(
            assignments[assignment_index],
            expression=selected_expression,
        )

    children = tuple(
        gate_structured_candidate_sites(
            child, config, verifier, backend=backend
        )
        for child in module.children
    )
    return replace(
        module,
        assignments=tuple(assignments),
        architecture_explorations=tuple(architectures),
        children=children,
    )


def candidate_formal_records(
    module: Module,
    exploration_results: Iterable[ExplorationResult] = (),
) -> tuple[object, ...]:
    """Enumerate all retained M39 evidence in deterministic source order."""

    records: list[object] = []
    retained = tuple(exploration_results)
    unified_site_keys = unified_implementation_site_keys(retained)
    records.extend(
        record
        for exploration in retained
        for record in exploration.formal_records
    )

    def visit(current: Module) -> None:
        records.extend(
            record
            for assignment in current.assignments
            if assignment.signal is None
            and assignment.channel is None
            and isinstance(assignment.expression, expr.ImplementationChoice)
            for record in assignment.expression.formal_records
        )
        for exploration in current.pipeline_explorations:
            if _pipeline_is_canonical_implement(
                current, exploration, unified_site_keys
            ):
                continue
            records.extend(exploration.formal_records)
        records.extend(
            record
            for region in current.elastic_pipeline_regions
            for record in region.formal_records
        )
        records.extend(
            record
            for exploration in current.architecture_explorations
            for record in exploration.formal_records
        )
        for child in current.children:
            visit(child)

    visit(module)
    return tuple(records)


def candidate_formal_record_sites(
    module: Module,
    exploration_results: Iterable[ExplorationResult] = (),
) -> tuple[tuple[CandidateSiteRecord, object], ...]:
    """Associate retained M39 records with their exact typed candidate site.

    Candidate implementation identities are intentionally insufficient here:
    the same implementation can occur at two distinct semantic sites.  This
    traversal reuses the same site constructors as the ledger and therefore
    never guesses ownership from generated names.
    """

    result: list[tuple[CandidateSiteRecord, object]] = []
    retained = tuple(exploration_results)
    unified_site_keys = unified_implementation_site_keys(retained)
    for exploration in retained:
        site = _exploration_site(exploration)
        result.extend((site, record) for record in exploration.formal_records)

    def visit(current: Module) -> None:
        for assignment in current.assignments:
            if (
                assignment.signal is None
                and assignment.channel is None
                and isinstance(assignment.expression, expr.ImplementationChoice)
                and assignment.expression.cost_policy is not None
            ):
                site = _choice_site(current, assignment)
                result.extend(
                    (site, record)
                    for record in assignment.expression.formal_records
                )
        for exploration in current.pipeline_explorations:
            if _pipeline_is_canonical_implement(
                current, exploration, unified_site_keys
            ):
                # Formal records for canonical implementation regions are
                # already attached to their unified ExplorationResult.  The
                # planner-only pipeline table must not publish a second site.
                continue
            site = _pipeline_site(current, exploration)
            result.extend((site, record) for record in exploration.formal_records)
        for region in current.elastic_pipeline_regions:
            site = _pipeline_site(current, region, elastic=True)
            result.extend((site, record) for record in region.formal_records)
        for exploration in current.architecture_explorations:
            site = _architecture_site(current, exploration)
            result.extend((site, record) for record in exploration.formal_records)
        for child in current.children:
            visit(child)

    visit(module)
    return tuple(result)


__all__ = [
    "CANDIDATE_SITE_LEDGER_SCHEMA",
    "CandidateRankRecord",
    "CandidateRewriteKind",
    "CandidateSiteError",
    "CandidateSiteKind",
    "CandidateSiteLedger",
    "CandidateSiteRecord",
    "SelectedCandidateSite",
    "build_candidate_site_ledger",
    "candidate_site_key",
    "candidate_formal_records",
    "candidate_formal_record_sites",
    "candidate_owner_clock_domain",
    "candidate_owner_formal_domain",
    "exploration_site_key",
    "gate_retained_explorations",
    "gate_structured_candidate_sites",
    "module_candidate_owner_identity",
    "pipeline_site_key",
    "selected_candidate_sites",
    "unified_implementation_site_keys",
]
