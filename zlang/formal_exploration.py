"""M39 formal eligibility, deterministic scheduling, and proof caching."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from zlang.common import stable_digest
from zlang.common.content_cache import load_json_object, publish_json_atomically
from zlang.formal import tool_versions
from zlang.formal_artifact_provider import (
    FormalArtifactNamespace,
    FormalArtifactProvider,
    FormalArtifactProviderError,
    FormalArtifactRecipe,
)
from zlang.ir.cross_backend import CrossBackendCounterexample
from zlang.ir.equivalence import EquivalenceCounterexample
from zlang.ir.formal import Counterexample, FormalStatus, ProofMode
from zlang.ir.module import dependency_context_identity
from zlang.source import SourceOrigin


_CACHE_RESULT_SCHEMA = "zlang-formal-proof-cache-result-v3"
_PREPARATION_INDEX_SCHEMA = "zlang-m39-preparation-index-v1"
_RESULT_CACHE_NAMESPACE = Path(FormalArtifactNamespace.M39.value) / "results"
CACHE_STATE_NOT_RUN = "not-run"
_CACHEABLE_FORMAL_STATUSES = frozenset({
    FormalStatus.BOUNDED_PASS,
    FormalStatus.PROVEN,
    FormalStatus.FAILED,
})


class FormalExplorationError(ValueError):
    """A required formal exploration policy could not establish eligibility."""

    def __init__(
        self,
        message: str,
        *,
        records: tuple["FormalExplorationRecord", ...] = (),
    ) -> None:
        super().__init__(message)
        self.records = tuple(records)


class FormalPolicy(str, Enum):
    OFF = "off"
    AVAILABLE = "available"
    REQUIRED_BMC = "required_bmc"
    REQUIRED_PROVEN = "required_proven"


@dataclass(frozen=True)
class FormalExplorationConfig:
    policy: FormalPolicy = FormalPolicy.OFF
    max_formal_candidates: int = 8
    bmc_depth: int = 32
    timeout_seconds: int = 120
    engine: str = "sby"
    solver: str = "z3"
    cache_directory: Path | None = None
    schema_version: str = "zlang-formal-exploration-v1"
    dependency_identity: str | None = None
    artifact_provider: object | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    tool_resolver: object | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    # Execution workspace is operational metadata.  It must not participate in
    # candidate, property, proof-cache, or implementation identities.
    work_directory: Path | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.max_formal_candidates < 1 or self.bmc_depth < 1 or self.timeout_seconds < 1:
            raise ValueError("formal exploration bounds must be positive")
        object.__setattr__(self, "policy", FormalPolicy(self.policy))
        if (
            self.dependency_identity is not None
            and re.fullmatch(r"[0-9a-f]{64}", self.dependency_identity) is None
        ):
            raise ValueError("formal dependency identity must be a SHA-256 digest")


@dataclass(frozen=True)
class FormalExplorationRecord:
    candidate_identity: str
    rank: int
    semantic_legality: str
    formal_route: str
    policy: FormalPolicy
    mode: ProofMode | None
    depth: int | None
    status: FormalStatus | None
    cache_state: str
    eligible: bool
    reason: str
    backend: str | None = None
    artifact_hash: str | None = None
    counterexample: Any | None = None
    engine: str | None = None
    solver: str | None = None
    proof_reason: str = ""
    property_identity: str | None = None
    harness_hash: str | None = None
    assumptions_identity: str | None = None
    backend_identity: str | None = None
    reference_artifact_hash: str | None = None
    implementation_artifact_hash: str | None = None
    source_origin: SourceOrigin | None = None
    selected_origin: SourceOrigin | None = None
    # Operational evidence location.  It is deliberately excluded from every
    # semantic/proof identity and from the persistent proof-cache payload.
    work_directory: str | None = None
    # Exact M39 execution recipe (proof key plus preparation/compiler recipe).
    # Candidate M36 orchestration may reuse a retained result only when this
    # identity matches its current timeout, tools, dependencies, and schemas.
    execution_recipe_identity: str | None = None

    def __str__(self) -> str:
        status = self.status.value if self.status else "not_requested"
        return (f"rank={self.rank} candidate={self.candidate_identity} route={self.formal_route} "
                f"policy={self.policy.value} mode={self.mode.value if self.mode else 'none'} "
                f"depth={self.depth if self.depth is not None else 'none'} status={status} "
                f"engine={self.engine or 'none'} solver={self.solver or 'none'} "
                f"cache={self.cache_state} counterexample={'yes' if self.counterexample else 'no'} "
                f"eligible={'yes' if self.eligible else 'no'} "
                f"reason={self.reason} proof_reason={self.proof_reason or 'none'}")


@dataclass(frozen=True)
class FormalGateResult:
    eligible: tuple[Any, ...]
    records: tuple[FormalExplorationRecord, ...]

    @property
    def report(self) -> str:
        lines = []
        for item in self.records:
            status = item.status.value if item.status else "not_requested"
            lines.append(
                f"rank={item.rank} candidate={item.candidate_identity} "
                f"route={item.formal_route} policy={item.policy.value} "
                f"status={status} mode={item.mode.value if item.mode else 'none'} "
                f"depth={item.depth if item.depth is not None else 'none'} "
                f"engine={item.engine or 'none'} solver={item.solver or 'none'} "
                f"cache={item.cache_state} "
                f"eligible={'yes' if item.eligible else 'no'} reason={item.reason} "
                f"proof_reason={item.proof_reason or 'none'}"
            )
        return "\n".join(lines) + ("\n" if lines else "")


def proof_cache_key(candidate: Any, *, property_identity: str,
                    artifact_hash: str | None, harness_hash: str,
                    assumptions_identity: str, config: FormalExplorationConfig,
                    dependency_identity: str | None = None,
                    backend_identity: str | None = None,
                    reference_artifact_hash: str | None = None,
                    implementation_artifact_hash: str | None = None) -> str:
    requested_tools = tuple(dict.fromkeys(
        ("yosys", "sby", "yosys-smtbmc", config.solver)
        if config.engine == "sby"
        else (config.engine, config.solver)
    ))
    payload = {
        "schema": config.schema_version,
        "candidate": getattr(candidate, "implementation_identity", repr(candidate)),
        "semantic": getattr(candidate, "semantic_identity", ""),
        "property": property_identity,
        "implementation_artifact": (
            implementation_artifact_hash
            or artifact_hash
            or getattr(candidate, "artifact_hash", None)
        ),
        "reference_artifact": reference_artifact_hash,
        "harness": harness_hash or getattr(candidate, "harness_hash", ""),
        "assumptions": assumptions_identity or getattr(candidate, "assumptions_identity", "none"),
        "timing": repr(getattr(candidate, "timing_relation", None)),
        "mode": "prove" if config.policy is FormalPolicy.REQUIRED_PROVEN else "bmc",
        "depth": config.bmc_depth,
        "timeout_seconds": config.timeout_seconds,
        "engine": config.engine,
        "solver": config.solver,
        # Only the tools on this exact route can affect its evidence.  Merely
        # installing another solver must not invalidate a Z3 proof cache key.
        "tool_versions": _formal_tool_versions(config, requested_tools),
    }
    if backend_identity is not None:
        payload["backend_identity"] = backend_identity
    resolved_dependency_identity = (
        dependency_identity
        or config.dependency_identity
        or getattr(candidate, "dependency_identity", None)
        or dependency_context_identity(candidate)
    )
    if resolved_dependency_identity is not None:
        payload["dependency_identity"] = resolved_dependency_identity
    return stable_digest(payload)


def _formal_tool_versions(
    config: FormalExplorationConfig,
    requested_tools: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    resolver = config.tool_resolver
    resolve = getattr(resolver, "formal_context", None)
    if callable(resolve):
        context = resolve(engine=config.engine, solver=config.solver)
        versions = tuple(getattr(context, "versions", ()))
        allowed = set(requested_tools)
        return tuple(item for item in versions if item[0] in allowed)
    return tool_versions(requested_tools)


_CONNECTED_CACHE_IDENTITY_FIELDS = frozenset({
    "property_identity",
    "artifact_hash",
    "reference_artifact_hash",
    "implementation_artifact_hash",
    "harness_hash",
    "assumptions_identity",
    "backend_identity",
})


def _proof_key_for_identity(
    candidate: Any,
    config: FormalExplorationConfig,
    cache_identity: Mapping[str, str],
) -> str:
    return proof_cache_key(
        candidate,
        property_identity=cache_identity.get(
            "property_identity",
            getattr(
                candidate,
                "property_identity",
                getattr(candidate, "semantic_identity", "candidate"),
            ),
        ),
        artifact_hash=cache_identity.get(
            "artifact_hash", getattr(candidate, "artifact_hash", None)
        ),
        harness_hash=cache_identity.get(
            "harness_hash", getattr(candidate, "harness_hash", "unbound")
        ),
        assumptions_identity=cache_identity.get(
            "assumptions_identity",
            getattr(candidate, "assumptions_identity", "none"),
        ),
        config=config,
        dependency_identity=cache_identity.get("dependency_identity"),
        backend_identity=cache_identity.get("backend_identity"),
        reference_artifact_hash=cache_identity.get("reference_artifact_hash"),
        implementation_artifact_hash=cache_identity.get(
            "implementation_artifact_hash"
        ),
    )


def _preparation_recipe(
    candidate: Any,
    config: FormalExplorationConfig,
    verifier: object | None,
) -> FormalArtifactRecipe | None:
    provider = getattr(verifier, "preparation_cache_recipe", None)
    if not callable(provider):
        return None
    value = provider(candidate, config)
    if value is None:
        return None
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise FormalExplorationError(
            "formal verifier preparation recipe must be a JSON object"
        )
    try:
        return FormalArtifactRecipe(
            FormalArtifactNamespace.M39,
            "connected-proof-preparation-index-v1",
            value,
        )
    except FormalArtifactProviderError as error:
        raise FormalExplorationError(
            f"formal verifier preparation recipe is invalid: {error}"
        ) from error


def _execution_recipe_identity(
    proof_key: str,
    preparation_recipe: FormalArtifactRecipe | None,
) -> str:
    return "m39-execution:" + stable_digest({
        "schema": "zlang-m39-execution-recipe-v1",
        "proof_key": proof_key,
        "preparation_recipe": (
            None if preparation_recipe is None else preparation_recipe.identity
        ),
    })


def formal_execution_recipe_identity(
    candidate: Any,
    config: FormalExplorationConfig,
    verifier: object | None,
    cache_identity: Mapping[str, str],
) -> str:
    """Return the exact recipe identity retained on one M39 result record."""

    preparation_recipe = _preparation_recipe(candidate, config, verifier)
    proof_key = _proof_key_for_identity(candidate, config, cache_identity)
    return _execution_recipe_identity(proof_key, preparation_recipe)


def _preparation_index_path(
    config: FormalExplorationConfig,
    verifier: object | None,
    recipe: FormalArtifactRecipe,
) -> Path | None:
    if config.cache_directory is None:
        return None
    provider = getattr(verifier, "artifact_provider", None)
    if not isinstance(provider, FormalArtifactProvider):
        configured = config.artifact_provider
        provider = (
            configured
            if isinstance(configured, FormalArtifactProvider)
            else None
        )
    root = (
        provider.cache_root
        if provider is not None and provider.cache_root is not None
        else config.cache_directory / "artifacts"
    )
    return (
        root
        / FormalArtifactNamespace.M39.value
        / "preparation-index"
        / f"{recipe.digest}.json"
    )


def _connected_cache_identity(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise FormalExplorationError(
            "preparation index cache identity must be an object"
        )
    if set(value) != _CONNECTED_CACHE_IDENTITY_FIELDS:
        raise FormalExplorationError(
            "preparation index cache identity fields are invalid"
        )
    result: dict[str, str] = {}
    for key in sorted(_CONNECTED_CACHE_IDENTITY_FIELDS):
        item = value.get(key)
        if not isinstance(item, str) or not item:
            raise FormalExplorationError(
                f"preparation index {key.replace('_', ' ')} must be non-empty"
            )
        result[key] = item
    if result["artifact_hash"] != result["implementation_artifact_hash"]:
        raise FormalExplorationError(
            "preparation index artifact aliases do not match"
        )
    return result


def _load_preparation_index(
    candidate: Any,
    config: FormalExplorationConfig,
    verifier: object | None,
    recipe: FormalArtifactRecipe | None,
) -> tuple[Mapping[str, str] | None, str | None, str]:
    if recipe is None:
        return None, None, "miss"
    path = _preparation_index_path(config, verifier, recipe)
    if path is None:
        return None, None, "miss"
    payload, diagnostic = load_json_object(path)
    if diagnostic is not None:
        return None, None, f"corrupt-ignored ({diagnostic})"
    if payload is None:
        return None, None, "miss"
    try:
        expected_fields = {
            "schema",
            "recipe",
            "recipe_identity",
            "cache_identity",
            "proof_key",
            "index_hash",
        }
        if set(payload) != expected_fields:
            raise FormalExplorationError(
                "preparation index envelope fields are invalid"
            )
        if payload.get("schema") != _PREPARATION_INDEX_SCHEMA:
            raise FormalExplorationError(
                "preparation index schema is unsupported"
            )
        cached_recipe = FormalArtifactRecipe.from_data(payload.get("recipe"))
        if cached_recipe.identity != recipe.identity:
            raise FormalExplorationError(
                "preparation index recipe does not match"
            )
        if payload.get("recipe_identity") != recipe.identity:
            raise FormalExplorationError(
                "preparation index recipe hash does not match"
            )
        identity = _connected_cache_identity(payload.get("cache_identity"))
        proof_key = payload.get("proof_key")
        if not isinstance(proof_key, str) or not proof_key:
            raise FormalExplorationError(
                "preparation index proof key must be non-empty"
            )
        expected_key = _proof_key_for_identity(candidate, config, identity)
        if proof_key != expected_key:
            raise FormalExplorationError(
                "preparation index proof key does not match"
            )
        expected_hash = stable_digest({
            "schema": _PREPARATION_INDEX_SCHEMA,
            "recipe_identity": recipe.identity,
            "cache_identity": identity,
            "proof_key": proof_key,
        })
        if payload.get("index_hash") != expected_hash:
            raise FormalExplorationError(
                "preparation index hash does not match"
            )
        return identity, proof_key, "hit"
    except (FormalArtifactProviderError, FormalExplorationError) as error:
        return None, None, f"corrupt-ignored ({error})"


def _save_preparation_index(
    candidate: Any,
    config: FormalExplorationConfig,
    verifier: object | None,
    recipe: FormalArtifactRecipe | None,
    cache_identity: Mapping[str, str],
) -> None:
    if recipe is None:
        return
    path = _preparation_index_path(config, verifier, recipe)
    if path is None:
        return
    identity = _connected_cache_identity(cache_identity)
    proof_key = _proof_key_for_identity(candidate, config, identity)
    identity_payload = {
        "schema": _PREPARATION_INDEX_SCHEMA,
        "recipe_identity": recipe.identity,
        "cache_identity": identity,
        "proof_key": proof_key,
    }
    publish_json_atomically(path, {
        "schema": _PREPARATION_INDEX_SCHEMA,
        "recipe": recipe.to_data(),
        "recipe_identity": recipe.identity,
        "cache_identity": identity,
        "proof_key": proof_key,
        "index_hash": stable_digest(identity_payload),
    })


@dataclass(frozen=True)
class _CachedProof:
    status: FormalStatus
    mode: ProofMode
    depth: int
    engine: str | None
    solver: str | None
    backend: str | None
    artifact_hash: str | None
    reason: str
    counterexample: Counterexample | EquivalenceCounterexample | CrossBackendCounterexample | None
    property_identity: str | None = None
    harness_hash: str | None = None
    assumptions_identity: str | None = None
    backend_identity: str | None = None
    reference_artifact_hash: str | None = None
    implementation_artifact_hash: str | None = None
    source_origin: SourceOrigin | None = None
    selected_origin: SourceOrigin | None = None
    work_directory: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.depth, bool) or self.depth < 1:
            raise FormalExplorationError("cached proof depth must be positive")
        if self.counterexample is not None and not isinstance(
            self.counterexample,
            (Counterexample, EquivalenceCounterexample, CrossBackendCounterexample),
        ):
            raise FormalExplorationError(
                "formal proof cache requires a typed M35, M36, or M38 counterexample"
            )
        if (self.counterexample is not None) != (
            self.status is FormalStatus.FAILED
        ):
            raise FormalExplorationError(
                "failed M39 proof requires exactly one counterexample"
            )
        if self.status is FormalStatus.BOUNDED_PASS and self.mode is not ProofMode.BMC:
            raise FormalExplorationError("cached bounded_pass requires BMC mode")
        if self.status is FormalStatus.PROVEN and self.mode is not ProofMode.PROVE:
            raise FormalExplorationError("cached proven result requires prove mode")
        if self.status is FormalStatus.FAILED:
            if not isinstance(self.counterexample, EquivalenceCounterexample):
                raise FormalExplorationError(
                    "failed M39 proof requires typed M36 counterexample metadata"
                )
        if self.status in {
            FormalStatus.BOUNDED_PASS,
            FormalStatus.PROVEN,
            FormalStatus.FAILED,
        }:
            if self.backend != "clash":
                raise FormalExplorationError(
                    "decisive M39 proof evidence requires the authoritative Clash backend"
                )
            for label, value in (
                ("property identity", self.property_identity),
                ("harness hash", self.harness_hash),
                ("assumptions identity", self.assumptions_identity),
                ("backend identity", self.backend_identity),
                ("reference artifact hash", self.reference_artifact_hash),
                ("implementation artifact hash", self.implementation_artifact_hash),
            ):
                if not value:
                    raise FormalExplorationError(
                        f"decisive M39 proof evidence requires {label}"
                    )
            for label, value in (
                ("harness hash", self.harness_hash),
                ("assumptions identity", self.assumptions_identity),
                ("backend identity", self.backend_identity),
                ("reference artifact hash", self.reference_artifact_hash),
                ("implementation artifact hash", self.implementation_artifact_hash),
            ):
                if re.fullmatch(r"[0-9a-f]{64}", value or "") is None:
                    raise FormalExplorationError(
                        f"decisive M39 proof {label} must be a SHA-256 digest"
                    )
            if self.artifact_hash != self.implementation_artifact_hash:
                raise FormalExplorationError(
                    "M39 artifact hash does not match the implementation artifact"
                )
            if (
                isinstance(self.counterexample, EquivalenceCounterexample)
                and self.counterexample.property_id != self.property_identity
            ):
                raise FormalExplorationError(
                    "M39 counterexample property does not match proof identity"
                )

    def to_data(self) -> dict[str, object]:
        return {
            "result_schema": _CACHE_RESULT_SCHEMA,
            "status": self.status.value,
            "mode": self.mode.value,
            "depth": self.depth,
            "engine": self.engine,
            "solver": self.solver,
            "backend": self.backend,
            "artifact_hash": self.artifact_hash,
            "reason": self.reason,
            "counterexample": _counterexample_to_data(self.counterexample),
            "property_identity": self.property_identity,
            "harness_hash": self.harness_hash,
            "assumptions_identity": self.assumptions_identity,
            "backend_identity": self.backend_identity,
            "reference_artifact_hash": self.reference_artifact_hash,
            "implementation_artifact_hash": self.implementation_artifact_hash,
            "source_origin": _origin_to_data(self.source_origin),
            "selected_origin": _origin_to_data(self.selected_origin),
        }


def _cache_identity_from_decisive_proof(
    proof: _CachedProof,
) -> dict[str, str]:
    if proof.status not in _CACHEABLE_FORMAL_STATUSES:
        raise FormalExplorationError(
            "only decisive formal evidence may publish a preparation index"
        )
    values = {
        "property_identity": proof.property_identity,
        "artifact_hash": proof.artifact_hash,
        "reference_artifact_hash": proof.reference_artifact_hash,
        "implementation_artifact_hash": proof.implementation_artifact_hash,
        "harness_hash": proof.harness_hash,
        "assumptions_identity": proof.assumptions_identity,
        "backend_identity": proof.backend_identity,
    }
    return _connected_cache_identity(values)


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise FormalExplorationError(f"cached proof {label} must be a string or null")
    return value


def _required_string(value: object, label: str) -> str:
    result = _optional_string(value, label)
    if not result:
        raise FormalExplorationError(f"cached proof {label} must not be empty")
    return result


def _origin_to_data(value: SourceOrigin | None) -> dict[str, object] | None:
    return None if value is None else value.to_data()


def _origin_from_data(value: object, label: str) -> SourceOrigin | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise FormalExplorationError(f"cached proof {label} must be an object or null")
    try:
        return SourceOrigin.from_data(value)
    except (TypeError, ValueError) as error:
        raise FormalExplorationError(
            f"cached proof {label} is invalid: {error}"
        ) from error


def _verifier_origin(value: object, label: str) -> SourceOrigin | None:
    if value is None or isinstance(value, SourceOrigin):
        return value
    return _origin_from_data(value, label)


def _integer_or_none(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise FormalExplorationError(f"cached counterexample {label} must be an integer or null")
    return value


def _value_pairs(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise FormalExplorationError("cached counterexample values must be an array")
    result: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2 or not all(
            isinstance(part, str) for part in item
        ):
            raise FormalExplorationError(
                "cached counterexample values must contain string pairs"
            )
        result.append((item[0], item[1]))
    return tuple(result)


def _counterexample_to_data(
    value: Counterexample | EquivalenceCounterexample | CrossBackendCounterexample | None,
) -> dict[str, object] | None:
    if value is None:
        return None
    if isinstance(value, Counterexample):
        return {
            "kind": "m35",
            "property_id": value.property_id,
            "cycle": value.cycle,
            "values": [list(item) for item in value.values],
            "raw_trace": value.raw_trace,
        }
    if isinstance(value, EquivalenceCounterexample):
        return {
            "kind": "m36",
            "property_id": value.property_id,
            "failure_cycle": value.failure_cycle,
            "sample_cycle": value.sample_cycle,
            "values": [list(item) for item in value.values],
            "raw_trace": value.raw_trace,
        }
    if isinstance(value, CrossBackendCounterexample):
        return {
            "kind": "m38",
            "property_id": value.property_id,
            "semantic_signal_id": value.semantic_signal_id,
            "cycle": value.cycle,
            "sample_cycle": value.sample_cycle,
            "left_backend": value.left_backend,
            "right_backend": value.right_backend,
            "left_artifact_hash": value.left_artifact_hash,
            "right_artifact_hash": value.right_artifact_hash,
            "left_rtl_path": value.left_rtl_path,
            "right_rtl_path": value.right_rtl_path,
            "values": [list(item) for item in value.values],
            "raw_trace": value.raw_trace,
            "source_origin": (
                None if value.source_origin is None else value.source_origin.to_data()
            ),
        }
    raise FormalExplorationError(
        "formal proof cache requires typed M35, M36, or M38 counterexample metadata"
    )


def _counterexample_from_data(value: object):
    if value is None:
        return None
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise FormalExplorationError("cached counterexample must be an object or null")
    kind = value.get("kind")
    property_id = value.get("property_id")
    if not isinstance(property_id, str) or not property_id:
        raise FormalExplorationError("cached counterexample property_id is invalid")
    values = _value_pairs(value.get("values"))
    raw_trace = _optional_string(value.get("raw_trace"), "counterexample raw_trace")
    if kind == "m35":
        expected = {"kind", "property_id", "cycle", "values", "raw_trace"}
        if set(value) != expected:
            raise FormalExplorationError("cached M35 counterexample fields are invalid")
        return Counterexample(
            property_id, _integer_or_none(value.get("cycle"), "cycle"), values,
            raw_trace,
        )
    if kind == "m36":
        expected = {
            "kind", "property_id", "failure_cycle", "sample_cycle", "values",
            "raw_trace",
        }
        if set(value) != expected:
            raise FormalExplorationError("cached M36 counterexample fields are invalid")
        return EquivalenceCounterexample(
            property_id,
            _integer_or_none(value.get("failure_cycle"), "failure_cycle"),
            _integer_or_none(value.get("sample_cycle"), "sample_cycle"),
            values,
            raw_trace,
        )
    if kind == "m38":
        expected = {
            "kind", "property_id", "semantic_signal_id", "cycle", "sample_cycle",
            "left_backend", "right_backend", "left_artifact_hash",
            "right_artifact_hash", "left_rtl_path", "right_rtl_path", "values",
            "raw_trace", "source_origin",
        }
        if set(value) != expected:
            raise FormalExplorationError("cached M38 counterexample fields are invalid")
        origin_data = value.get("source_origin")
        try:
            if origin_data is not None and (
                not isinstance(origin_data, Mapping)
                or set(origin_data) != {"construct", "digest", "source_unit", "span"}
                or not isinstance(origin_data.get("span"), Mapping)
                or set(origin_data["span"]) != {
                    "start_line", "start_column", "end_line", "end_column"
                }
            ):
                raise ValueError("source origin fields are invalid")
            origin = None if origin_data is None else SourceOrigin.from_data(origin_data)
        except (TypeError, ValueError) as error:
            raise FormalExplorationError(
                f"cached M38 counterexample source origin is invalid: {error}"
            ) from error
        return CrossBackendCounterexample(
            property_id,
            _optional_string(value.get("semantic_signal_id"), "semantic_signal_id"),
            _integer_or_none(value.get("cycle"), "cycle"),
            _integer_or_none(value.get("sample_cycle"), "sample_cycle"),
            _required_string(value.get("left_backend"), "left_backend"),
            _required_string(value.get("right_backend"), "right_backend"),
            _required_string(value.get("left_artifact_hash"), "left_artifact_hash"),
            _required_string(value.get("right_artifact_hash"), "right_artifact_hash"),
            _optional_string(value.get("left_rtl_path"), "left_rtl_path"),
            _optional_string(value.get("right_rtl_path"), "right_rtl_path"),
            values,
            raw_trace,
            origin,
        )
    raise FormalExplorationError(f"unsupported cached counterexample kind: {kind}")


def _proof_from_data(value: object) -> _CachedProof:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise FormalExplorationError("cached proof result must be an object")
    expected = {
        "result_schema", "status", "mode", "depth", "engine", "solver",
        "backend", "artifact_hash", "reason", "counterexample",
        "property_identity", "harness_hash", "assumptions_identity",
        "backend_identity", "reference_artifact_hash",
        "implementation_artifact_hash", "source_origin", "selected_origin",
    }
    if set(value) != expected or value.get("result_schema") != _CACHE_RESULT_SCHEMA:
        raise FormalExplorationError("cached proof result schema/fields are invalid")
    depth = value.get("depth")
    if isinstance(depth, bool) or not isinstance(depth, int):
        raise FormalExplorationError("cached proof depth must be an integer")
    try:
        return _CachedProof(
            FormalStatus(value.get("status")),
            ProofMode(value.get("mode")),
            depth,
            _optional_string(value.get("engine"), "engine"),
            _optional_string(value.get("solver"), "solver"),
            _optional_string(value.get("backend"), "backend"),
            _optional_string(value.get("artifact_hash"), "artifact_hash"),
            _optional_string(value.get("reason"), "reason") or "",
            _counterexample_from_data(value.get("counterexample")),
            _optional_string(value.get("property_identity"), "property_identity"),
            _optional_string(value.get("harness_hash"), "harness_hash"),
            _optional_string(
                value.get("assumptions_identity"), "assumptions_identity"
            ),
            _optional_string(value.get("backend_identity"), "backend_identity"),
            _optional_string(
                value.get("reference_artifact_hash"), "reference_artifact_hash"
            ),
            _optional_string(
                value.get("implementation_artifact_hash"),
                "implementation_artifact_hash",
            ),
            _origin_from_data(value.get("source_origin"), "source origin"),
            _origin_from_data(value.get("selected_origin"), "selected origin"),
        )
    except ValueError as error:
        raise FormalExplorationError(f"cached proof enum value is invalid: {error}") from error


def _bound_identity(
    value: Mapping[object, object],
    cache_identity: Mapping[str, str],
    key: str,
    *,
    aliases: tuple[str, ...] = (),
) -> str | None:
    supplied: list[str] = []
    for source in (cache_identity, value):
        for name in (key, *aliases):
            item = source.get(name)
            if item is not None:
                supplied.append(_required_string(item, name))
    if supplied and any(item != supplied[0] for item in supplied[1:]):
        raise FormalExplorationError(
            f"formal verifier {key} does not match its cache identity"
        )
    return supplied[0] if supplied else None


def _proof_from_verifier(
    value: object,
    config: FormalExplorationConfig,
    *,
    cache_identity: Mapping[str, str],
    route: str,
) -> _CachedProof:
    if not isinstance(value, Mapping):
        raise FormalExplorationError("formal verifier must return a mapping")
    expected_mode = (
        ProofMode.PROVE
        if config.policy is FormalPolicy.REQUIRED_PROVEN
        else ProofMode.BMC
    )
    try:
        status = FormalStatus(value.get("status", FormalStatus.UNKNOWN))
        mode = ProofMode(value.get("mode", expected_mode))
    except ValueError as error:
        raise FormalExplorationError(f"formal verifier returned an invalid status/mode: {error}") from error
    depth = value.get("depth", config.bmc_depth)
    if isinstance(depth, bool) or not isinstance(depth, int):
        raise FormalExplorationError("formal verifier depth must be an integer")
    engine = _optional_string(value.get("engine", config.engine), "engine")
    solver = _optional_string(value.get("solver", config.solver), "solver")
    decisive = status in {
        FormalStatus.BOUNDED_PASS,
        FormalStatus.PROVEN,
        FormalStatus.FAILED,
    }
    if mode is not expected_mode:
        raise FormalExplorationError(
            "formal verifier mode does not match the requested policy"
        )
    if decisive:
        required_cache_identity = {
            "property_identity": cache_identity.get("property_identity"),
            "harness_hash": cache_identity.get("harness_hash"),
            "assumptions_identity": cache_identity.get("assumptions_identity"),
            "backend_identity": cache_identity.get("backend_identity"),
            "reference_artifact_hash": cache_identity.get(
                "reference_artifact_hash"
            ),
            "implementation_artifact_hash": (
                cache_identity.get("implementation_artifact_hash")
                or cache_identity.get("artifact_hash")
            ),
        }
        missing_identity = tuple(
            label for label, item in required_cache_identity.items() if not item
        )
        if missing_identity:
            raise FormalExplorationError(
                "decisive M39 verifier result has no bound cache identity for: "
                + ", ".join(missing_identity)
            )
        if route != "M36_clash":
            raise FormalExplorationError(
                "decisive M39 proof evidence requires the M36_clash route"
            )
        if depth != config.bmc_depth:
            raise FormalExplorationError(
                "formal verifier depth does not match the requested proof depth"
            )
        if engine != config.engine or solver != config.solver:
            raise FormalExplorationError(
                "formal verifier engine/solver do not match the requested route"
            )
    implementation_hash = _bound_identity(
        value,
        cache_identity,
        "implementation_artifact_hash",
        aliases=("artifact_hash",),
    )
    proof = _CachedProof(
        status,
        mode,
        depth,
        engine,
        solver,
        _optional_string(value.get("backend"), "backend"),
        implementation_hash,
        _optional_string(value.get("reason"), "reason") or "",
        value.get("counterexample"),
        _bound_identity(value, cache_identity, "property_identity"),
        _bound_identity(value, cache_identity, "harness_hash"),
        _bound_identity(value, cache_identity, "assumptions_identity"),
        _bound_identity(value, cache_identity, "backend_identity"),
        _bound_identity(value, cache_identity, "reference_artifact_hash"),
        implementation_hash,
        _verifier_origin(value.get("source_origin"), "source origin"),
        _verifier_origin(value.get("selected_origin"), "selected origin"),
        _optional_string(value.get("work_directory"), "work directory"),
    )
    return proof


def _cached_proof_mismatch(
    proof: _CachedProof,
    cache_identity: Mapping[str, str],
    config: FormalExplorationConfig,
) -> str | None:
    expected = {
        "property_identity": cache_identity.get("property_identity"),
        "harness_hash": cache_identity.get("harness_hash"),
        "assumptions_identity": cache_identity.get("assumptions_identity"),
        "backend_identity": cache_identity.get("backend_identity"),
        "reference_artifact_hash": cache_identity.get("reference_artifact_hash"),
        "implementation_artifact_hash": (
            cache_identity.get("implementation_artifact_hash")
            or cache_identity.get("artifact_hash")
        ),
    }
    for field, expected_value in expected.items():
        if expected_value is not None and getattr(proof, field) != expected_value:
            return f"cached {field.replace('_', ' ')} does not match route"
    expected_mode = (
        ProofMode.PROVE
        if config.policy is FormalPolicy.REQUIRED_PROVEN
        else ProofMode.BMC
    )
    if proof.mode is not expected_mode:
        return "cached proof mode does not match requested policy"
    if proof.depth != config.bmc_depth:
        return "cached proof depth does not match requested depth"
    if proof.engine != config.engine or proof.solver != config.solver:
        return "cached proof engine/solver does not match requested route"
    return None


def _load_cache(
    config: FormalExplorationConfig, key: str,
) -> tuple[_CachedProof | None, str]:
    if config.cache_directory is None:
        return None, "miss"
    canonical = config.cache_directory / _RESULT_CACHE_NAMESPACE / f"{key}.json"
    legacy = config.cache_directory / f"{key}.json"
    payload, diagnostic = load_json_object(canonical)
    if payload is None and diagnostic is None:
        payload, diagnostic = load_json_object(legacy)
    if diagnostic is not None:
        return None, f"corrupt-ignored ({diagnostic})"
    if payload is None:
        return None, "miss"
    try:
        fields = set(payload)
        current_fields = {"schema", "key", "result", "result_hash"}
        if fields != current_fields:
            raise FormalExplorationError("cache envelope fields are invalid")
        if payload.get("schema") != config.schema_version:
            raise FormalExplorationError("cache envelope schema does not match")
        if payload.get("key") != key:
            raise FormalExplorationError("cache envelope key does not match")
        result_data = payload.get("result")
        result_hash = payload.get("result_hash")
        if (
            not isinstance(result_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", result_hash) is None
            or result_hash != stable_digest(result_data)
        ):
            raise FormalExplorationError(
                "cache result hash does not match"
            )
        return _proof_from_data(result_data), "hit"
    except FormalExplorationError as error:
        return None, f"corrupt-ignored ({error})"


def _save_cache(config: FormalExplorationConfig, key: str, proof: _CachedProof) -> None:
    if config.cache_directory is None:
        return
    result_data = proof.to_data()
    publish_json_atomically(
        config.cache_directory / _RESULT_CACHE_NAMESPACE / f"{key}.json",
        {
            "schema": config.schema_version,
            "key": key,
            "result": result_data,
            "result_hash": stable_digest(result_data),
        },
    )


def _eligible(status: FormalStatus | None, policy: FormalPolicy) -> tuple[bool, str]:
    if policy is FormalPolicy.OFF:
        return True, "formal verification disabled"
    if policy is FormalPolicy.AVAILABLE:
        if status is FormalStatus.FAILED:
            return True, (
                "counterexample found; advisory policy does not alter "
                "static eligibility"
            )
        if status in (FormalStatus.PROVEN, FormalStatus.BOUNDED_PASS):
            return True, "formal evidence recorded; advisory policy"
        return True, (
            "formal route unavailable or inconclusive; advisory policy does "
            "not alter static eligibility"
        )
    if status is FormalStatus.PROVEN:
        return True, "unbounded proof satisfied"
    if policy is FormalPolicy.REQUIRED_BMC and status is FormalStatus.BOUNDED_PASS:
        return True, "bounded proof satisfied"
    return False, "formal status does not satisfy required policy"


def _expected_mode(config: FormalExplorationConfig) -> ProofMode:
    return (
        ProofMode.PROVE
        if config.policy is FormalPolicy.REQUIRED_PROVEN
        else ProofMode.BMC
    )


def _execute_stage(
    candidate: Any,
    config: FormalExplorationConfig,
    verifier: Callable[[Any, FormalExplorationConfig], Any] | None,
    *,
    route: str,
) -> tuple[_CachedProof, str, bool, str]:
    """Load or execute exactly one BMC/PROVE stage for one candidate.

    The stage config, rather than the outer selection policy, owns the cache
    mode.  This lets ``required_proven`` retain separate BMC and PROVE evidence
    without conflating either result or cache entry.
    """

    preparation_recipe = _preparation_recipe(
        candidate,
        config,
        verifier,
    )
    indexed_identity, indexed_key, index_state = _load_preparation_index(
        candidate,
        config,
        verifier,
        preparation_recipe,
    )
    if indexed_identity is not None and indexed_key is not None:
        indexed_proof, indexed_proof_state = _load_cache(config, indexed_key)
        if indexed_proof is not None:
            mismatch = _cached_proof_mismatch(
                indexed_proof,
                indexed_identity,
                config,
            )
            if indexed_proof.status not in _CACHEABLE_FORMAL_STATUSES:
                mismatch = "preparation index references an inconclusive result"
            if mismatch is None:
                # This is the only cross-session fast path.  The recipe binds
                # candidate/reference semantics, exact configuration,
                # dependency/compiler schemas, and relevant tool versions;
                # the indexed full identity is then checked against the
                # decisive proof before any backend generator is consulted.
                return (
                    indexed_proof,
                    "hit",
                    False,
                    _execution_recipe_identity(indexed_key, preparation_recipe),
                )
            index_state = f"corrupt-ignored ({mismatch})"
        elif indexed_proof_state.startswith("corrupt-ignored"):
            index_state = indexed_proof_state

    cache_identity: Mapping[str, str] = {}
    identity_provider = getattr(verifier, "cache_identity", None)
    if callable(identity_provider):
        provided = identity_provider(candidate, config)
        if not isinstance(provided, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in provided.items()
        ):
            raise FormalExplorationError(
                "formal verifier cache identity must be a string mapping"
            )
        cache_identity = provided
    unavailable_reason = cache_identity.get("unavailable_reason")
    key = _proof_key_for_identity(candidate, config, cache_identity)
    execution_recipe_identity = _execution_recipe_identity(
        key, preparation_recipe
    )
    proof, loaded_state = (
        (None, "miss") if unavailable_reason else _load_cache(config, key)
    )
    if proof is not None:
        mismatch = _cached_proof_mismatch(proof, cache_identity, config)
        if mismatch is not None:
            proof = None
            loaded_state = f"corrupt-ignored ({mismatch})"
    corrupt_state = next(
        (
            item for item in (index_state, loaded_state)
            if item.startswith("corrupt-ignored")
        ),
        None,
    )
    cache_state = "hit" if proof is not None else (
        CACHE_STATE_NOT_RUN
        if verifier is None or unavailable_reason
        else corrupt_state or "executed"
    )
    invalid_result = False
    executed = False
    if proof is None:
        if verifier is not None and not unavailable_reason:
            executed = True
            result = verifier(candidate, config)
        else:
            result = {
                "status": FormalStatus.SKIPPED,
                "mode": _expected_mode(config),
                "depth": config.bmc_depth,
                "engine": config.engine,
                "solver": config.solver,
                "backend": "clash" if unavailable_reason else None,
                "reason": (
                    unavailable_reason
                    or "M36 semantic-reference proof route is not bound"
                ),
            }
        try:
            proof = _proof_from_verifier(
                result,
                config,
                cache_identity=cache_identity,
                route=route,
            )
        except FormalExplorationError as error:
            invalid_result = True
            cache_state = "invalid"
            proof = _CachedProof(
                FormalStatus.UNKNOWN,
                _expected_mode(config),
                config.bmc_depth,
                config.engine,
                config.solver,
                None,
                None,
                f"invalid formal verifier result: {error}",
                None,
            )
        if (
            executed
            and not invalid_result
            and proof.status in _CACHEABLE_FORMAL_STATUSES
        ):
            _save_cache(config, key, proof)
    if (
        not invalid_result
        and proof.status in _CACHEABLE_FORMAL_STATUSES
        and preparation_recipe is not None
    ):
        _save_preparation_index(
            candidate,
            config,
            verifier,
            preparation_recipe,
            _cache_identity_from_decisive_proof(proof),
        )
    assert proof is not None
    return proof, cache_state, invalid_result, execution_recipe_identity


def _record_for_stage(
    candidate: Any,
    rank: int,
    outer_policy: FormalPolicy,
    route: str,
    proof: _CachedProof,
    cache_state: str,
    execution_recipe_identity: str,
    *,
    eligible: bool,
    reason: str,
) -> FormalExplorationRecord:
    return FormalExplorationRecord(
        candidate.implementation_identity,
        rank,
        "valid",
        route,
        outer_policy,
        proof.mode,
        proof.depth,
        proof.status,
        cache_state,
        eligible,
        reason,
        proof.backend,
        proof.artifact_hash,
        proof.counterexample,
        proof.engine,
        proof.solver,
        proof.reason,
        proof.property_identity,
        proof.harness_hash,
        proof.assumptions_identity,
        proof.backend_identity,
        proof.reference_artifact_hash,
        proof.implementation_artifact_hash,
        proof.source_origin,
        proof.selected_origin,
        proof.work_directory,
        execution_recipe_identity,
    )


def _attempt_summary(records: tuple[FormalExplorationRecord, ...]) -> str:
    if not records:
        return ""
    return "; attempted formal stages: " + ", ".join(
        "rank="
        f"{item.rank} candidate={item.candidate_identity} "
        f"mode={item.mode.value if item.mode else 'none'} "
        f"status={item.status.value if item.status else 'not_run'}"
        for item in records
    )


def _raise_with_attempts(
    message: str,
    records: list[FormalExplorationRecord],
) -> None:
    attempted = tuple(records)
    raise FormalExplorationError(
        message + _attempt_summary(attempted),
        records=attempted,
    )


def _inconclusive(status: FormalStatus) -> bool:
    return status in {FormalStatus.UNKNOWN, FormalStatus.SKIPPED}


def gate_candidates(candidates: tuple[Any, ...], evaluations: tuple[Any, ...],
                    config: FormalExplorationConfig,
                    verifier: Callable[[Any, FormalExplorationConfig], Any] | None = None,
                    *, route: str = "M36_clash") -> FormalGateResult:
    """Verify candidates in exact M28 rank order; never schedule all eagerly."""
    ranked = sorted((item for item in evaluations if item.legal), key=lambda item: item.objective_key)
    route = str(getattr(verifier, "formal_route", route))
    if config.policy is FormalPolicy.OFF:
        return FormalGateResult(tuple(item.candidate for item in ranked), tuple(
            FormalExplorationRecord(item.candidate.implementation_identity, rank, "valid", "none",
                                    config.policy, None, None, None, CACHE_STATE_NOT_RUN, True,
                                    "formal verification disabled")
            for rank, item in enumerate(ranked, 1)))
    selected: list[Any] = []
    records: list[FormalExplorationRecord] = []
    if verifier is None and config.policy in {
        FormalPolicy.REQUIRED_BMC,
        FormalPolicy.REQUIRED_PROVEN,
    }:
        raise FormalExplorationError(
            "required formal policy has no connected M36 verifier route"
        )

    # ``available`` is observational only: execute BMC for the exact rank-1
    # candidate and preserve the unchanged M28-eligible set regardless of the
    # result.  Later candidates are explicitly not run.
    if config.policy is FormalPolicy.AVAILABLE:
        selected = [item.candidate for item in ranked]
        if ranked:
            candidate = ranked[0].candidate
            proof, cache_state, invalid_result, execution_recipe_identity = _execute_stage(
                candidate, config, verifier, route=route,
            )
            eligible, reason = _eligible(proof.status, config.policy)
            if invalid_result:
                reason = (
                    "formal verifier evidence is invalid; advisory policy does "
                    "not alter static eligibility"
                )
            records.append(_record_for_stage(
                candidate,
                1,
                config.policy,
                route,
                proof,
                cache_state,
                execution_recipe_identity,
                eligible=eligible,
                reason=reason,
            ))
        records.extend(
            FormalExplorationRecord(
                item.candidate.implementation_identity,
                rank,
                "valid",
                route,
                config.policy,
                None,
                None,
                None,
                CACHE_STATE_NOT_RUN,
                True,
                "advisory policy checks only the statically selected candidate",
            )
            for rank, item in enumerate(ranked[1:], 2)
        )
        return FormalGateResult(tuple(selected), tuple(records))

    attempted_candidates = 0
    for rank, evaluation in enumerate(ranked, 1):
        candidate = evaluation.candidate
        if attempted_candidates >= config.max_formal_candidates:
            _raise_with_attempts(
                "formal budget exhausted before a candidate satisfied required "
                "formal verification",
                records,
            )
        attempted_candidates += 1

        stage_configs = (
            (
                replace(config, policy=FormalPolicy.REQUIRED_BMC),
                replace(config, policy=FormalPolicy.REQUIRED_PROVEN),
            )
            if config.policy is FormalPolicy.REQUIRED_PROVEN
            else (config,)
        )
        candidate_failed = False
        for stage_index, stage_config in enumerate(stage_configs):
            proof, cache_state, invalid_result, execution_recipe_identity = _execute_stage(
                candidate, stage_config, verifier, route=route,
            )
            is_final_stage = stage_index == len(stage_configs) - 1
            eligible, reason = _eligible(proof.status, config.policy)
            if invalid_result:
                eligible = False
                reason = "formal verifier evidence is invalid"
            elif (
                config.policy is FormalPolicy.REQUIRED_PROVEN
                and proof.mode is ProofMode.BMC
                and proof.status is FormalStatus.BOUNDED_PASS
            ):
                eligible = False
                reason = "bounded precheck passed; unbounded proof still required"
            records.append(_record_for_stage(
                candidate,
                rank,
                config.policy,
                route,
                proof,
                cache_state,
                execution_recipe_identity,
                eligible=eligible and is_final_stage,
                reason=reason,
            ))

            if _inconclusive(proof.status):
                _raise_with_attempts(
                    "required formal proof is inconclusive for candidate "
                    f"'{candidate.implementation_identity}': "
                    + (proof.reason or "solver returned unknown"),
                    records,
                )
            if proof.status is FormalStatus.FAILED:
                candidate_failed = True
                break
            if not is_final_stage and proof.status is not FormalStatus.BOUNDED_PASS:
                _raise_with_attempts(
                    "required_proven BMC precheck returned an unusable status "
                    f"for candidate '{candidate.implementation_identity}'",
                    records,
                )
            if is_final_stage and eligible:
                selected.append(candidate)
                break
        if selected:
            break
        if candidate_failed:
            continue
    if not selected:
        _raise_with_attempts(
            "no candidate satisfies required formal verification",
            records,
        )
    return FormalGateResult(tuple(selected), tuple(records))


__all__ = [
    "CACHE_STATE_NOT_RUN",
    "FormalExplorationConfig",
    "FormalExplorationError",
    "FormalExplorationRecord",
    "FormalGateResult",
    "FormalPolicy",
    "formal_execution_recipe_identity",
    "gate_candidates",
    "proof_cache_key",
]
