"""Immutable verification bundles and replayable verification reports.

The bundle is deliberately a compiler/executor boundary.  It contains only
content-addressed verification inputs.  Solver choice, proof mode, depth,
timeout, discovered tool versions, and results live in :class:`VerificationRunReport`
and therefore never affect the immutable bundle identity.

Safety jobs reuse the existing M35 runner while cover jobs use the bounded
reachability runner.  Unknown kinds fail closed as explicit skips.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Iterable, Mapping

from zlang.backend.publication import (
    SafePublicationError,
    publish_relative_files,
    validate_relative_hashes,
)
from zlang.backend.source_map import GeneratedSourceMap
from zlang.common import stable_digest, stable_json
from zlang.common.content_cache import load_json_object, publish_json_atomically
from zlang.formal import (
    FormalToolchainContext,
    run_verilog_cover,
    run_verilog_formal,
)
from zlang.formal_trace import (
    FormalTraceSnapshot,
    TraceBinding,
    decode_vcd_trace,
)
from zlang.ir.comparison_window import ComparisonWindow
from zlang.ir.cdc import (
    ClockDomain,
    clock_domain_contract_identity,
    clock_domain_data,
    clock_domain_from_data,
)
from zlang.ir.formal import (
    Counterexample,
    CoverResult,
    CoverStatus,
    CoverWitness,
    FormalResult,
    FormalStatus,
    ProofMode,
)
from zlang.ir.formal_planning import FormalExecutionPlan, FormalPlanningError
from zlang.source import SourceOrigin
from zlang.toolchain import GeneratedDiagnosticContext


VERIFICATION_BUNDLE_SCHEMA = "zlang-verification-bundle-v4"
VERIFICATION_BUNDLE_SCHEMA_VERSION = 4
VERIFICATION_IR_SCHEMA = "zlang-verification-ir-snapshot-v3"
VERIFICATION_IR_SCHEMA_VERSION = 3
VERIFICATION_RUN_REPORT_SCHEMA = "zlang-verification-run-report-v7"
VERIFICATION_RUN_REPORT_SCHEMA_VERSION = 7
VERIFICATION_RESULT_CACHE_SCHEMA = "zlang-verification-result-cache-v1"
VERIFICATION_RESULT_CACHE_SCHEMA_VERSION = 1

_HASH = re.compile(r"[0-9a-f]{64}")
_IDENTITY = re.compile(r"(?:[a-z][a-z0-9_.-]*:)?[0-9a-f]{64}")
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_KIND = re.compile(r"[a-z][a-z0-9_]*")
_FILE_PREFIX = {
    "implementation": "implementation/",
    "companion": "implementation/companions/",
    "harness": "harness/",
    "config": "config/",
    "source_map": "source-map/",
    "verification_ir": "",
}


class _ExecutionRootLease:
    """Process-wide exclusive lease for one deterministic solver workspace.

    Job paths intentionally remain content-derived and inspectable.  The lease
    prevents two concurrent replays of the same bundle/run configuration from
    corrupting those shared status, log, or trace files.  Independent bundles
    and configurations still execute concurrently because they use different
    roots and lock files.
    """

    def __init__(self, root: Path) -> None:
        self._stream = (root / ".zlang-formal.lock").open("a+", encoding="ascii")
        fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX)

    def close(self) -> None:
        if self._stream.closed:
            return
        try:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()

    def __del__(self) -> None:
        self.close()


class VerificationBundleError(ValueError):
    """An immutable verification bundle is malformed or inconsistent."""


def _require_exact_keys(
    data: Mapping[str, object], *, required: Iterable[str], description: str
) -> None:
    required_set = set(required)
    actual = set(data)
    missing = sorted(required_set - actual)
    unknown = sorted(actual - required_set)
    if missing:
        raise VerificationBundleError(
            f"{description} is missing field '{missing[0]}'"
        )
    if unknown:
        raise VerificationBundleError(
            f"{description} contains unknown field '{unknown[0]}'"
        )


def _require_string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise VerificationBundleError(f"{description} must be a non-empty string")
    if any(ord(character) < 32 for character in value):
        raise VerificationBundleError(f"{description} must not contain control characters")
    return value


def _require_integer(value: object, description: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise VerificationBundleError(
            f"{description} must be an integer greater than or equal to {minimum}"
        )
    return value


def _require_json_value(value: object, description: str) -> None:
    """Reject Python-only values instead of silently stringifying them."""

    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, list):
        for item in value:
            _require_json_value(item, description)
        return
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise VerificationBundleError(f"{description} keys must be strings")
        for item in value.values():
            _require_json_value(item, description)
        return
    raise VerificationBundleError(f"{description} must contain only JSON values")


def _validate_identity(value: object, description: str) -> str:
    identity = _require_string(value, description)
    if _IDENTITY.fullmatch(identity) is None:
        raise VerificationBundleError(
            f"{description} must be a lowercase SHA-256 identity"
        )
    return identity


def _validate_property_id(value: object) -> str:
    property_id = _require_string(value, "verification property ID")
    if len(property_id) > 512 or any(character.isspace() for character in property_id):
        raise VerificationBundleError(
            "verification property ID must be one bounded token"
        )
    return property_id


def _validate_relative_path(value: object, *, prefix: str | None = None) -> str:
    path = _require_string(value, "verification bundle path")
    if "\\" in path:
        raise VerificationBundleError(
            f"verification bundle path '{path}' must use '/' separators"
        )
    logical = PurePosixPath(path)
    if (
        logical.is_absolute()
        or path.endswith("/")
        or any(part in {"", ".", ".."} for part in logical.parts)
        or logical.as_posix() != path
    ):
        raise VerificationBundleError(
            f"unsafe verification bundle path '{path}'"
        )
    if prefix is not None and not path.startswith(prefix):
        raise VerificationBundleError(
            f"verification bundle path '{path}' must be below '{prefix}'"
        )
    return path


def _origin_to_data(origin: SourceOrigin | None) -> object:
    return None if origin is None else origin.to_data()


def _origin_from_data(value: object) -> SourceOrigin | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise VerificationBundleError("verification source origin must be an object")
    try:
        return SourceOrigin.from_data(value)
    except ValueError as error:
        raise VerificationBundleError(str(error)) from error


def _clock_domain_from_job_data(value: object) -> ClockDomain | None:
    try:
        return clock_domain_from_data(value)
    except ValueError as error:
        raise VerificationBundleError(str(error)) from error


def _identity_payload(value: object, *, top_level: bool = True) -> object:
    """Return the semantic verification payload used for identity hashing.

    Source/dependency/compiler identities and source origins remain published
    attribution.  They must not make an otherwise identical verification
    overlay acquire a different semantic verification identity.
    """

    if isinstance(value, Mapping):
        return {
            key: _identity_payload(item, top_level=False)
            for key, item in value.items()
            if key != "source_origin" and not (top_level and key == "identities")
        }
    if isinstance(value, list):
        return [_identity_payload(item, top_level=False) for item in value]
    return value


def _validate_verification_payload(
    payload: Mapping[str, object],
    *,
    property_ids: tuple[str, ...],
    jobs: tuple["VerificationJob", ...] | None = None,
) -> None:
    """Validate the executable verification snapshot and all cross-links."""

    version = payload.get("formal_ir_version")
    common_fields = (
        "formal_ir_version",
        "identities",
        "hardware",
        "scopes",
        "properties",
        "bindings",
        "vacuity_dependencies",
    )
    if version == 1:
        required_fields = common_fields
    elif version == 2:
        required_fields = (*common_fields, "binding_sets", "execution_plan")
    elif version == 3:
        required_fields = (
            *common_fields,
            "binding_sets",
            "execution_plan",
            "compiler_execution_plan",
        )
    elif version == 4:
        required_fields = (
            *common_fields,
            "binding_sets",
            "execution_plan",
            "compiler_execution_plan",
            "candidate_equivalence_records",
        )
    else:
        raise VerificationBundleError("unsupported Formal IR version")
    _require_exact_keys(
        payload,
        required=required_fields,
        description="verification IR payload",
    )

    identities = payload["identities"]
    if not isinstance(identities, Mapping):
        raise VerificationBundleError("verification identities must be an object")
    _require_exact_keys(
        identities,
        required=("source", "dependency", "compiler"),
        description="verification identities",
    )
    for name in ("source", "dependency", "compiler"):
        _validate_identity(identities[name], f"{name} identity")

    hardware = payload["hardware"]
    if not isinstance(hardware, Mapping):
        raise VerificationBundleError("verification hardware identity must be an object")
    _require_exact_keys(
        hardware,
        required=("high_level_ir_identity", "selected_ir_identity"),
        description="verification hardware identity",
    )
    _validate_identity(hardware["high_level_ir_identity"], "high-level IR identity")
    _validate_identity(hardware["selected_ir_identity"], "selected IR identity")

    properties_value = payload["properties"]
    if not isinstance(properties_value, list):
        raise VerificationBundleError("verification properties must be an array")
    property_kinds: dict[str, str] = {}
    property_generated_from: dict[str, str | None] = {}
    for value in properties_value:
        if not isinstance(value, Mapping):
            raise VerificationBundleError("verification property must be an object")
        property_fields = (
            "id", "kind", "generated_from", "predicate", "source_origin"
        )
        if version in {2, 3, 4}:
            property_fields = (*property_fields, "classification")
        _require_exact_keys(
            value,
            required=property_fields,
            description="verification property",
        )
        property_id = _validate_property_id(value["id"])
        kind = _require_string(value["kind"], "verification property kind")
        if kind not in {"safety", "cover"}:
            raise VerificationBundleError(
                f"verification property '{property_id}' has unsupported kind '{kind}'"
            )
        if version in {2, 3, 4}:
            classification = _require_string(
                value["classification"],
                "verification property classification",
            )
            valid_classifications = (
                {"bounded_reachability"}
                if kind == "cover"
                else {"behavioral", "representation_invariant"}
            )
            if classification not in valid_classifications:
                raise VerificationBundleError(
                    f"verification property '{property_id}' has invalid "
                    f"classification '{classification}'"
                )
        if property_id in property_kinds:
            raise VerificationBundleError(
                f"duplicate verification property '{property_id}'"
            )
        generated_from = value["generated_from"]
        if generated_from is not None and not isinstance(generated_from, str):
            raise VerificationBundleError(
                f"verification property '{property_id}' generated_from must be a string"
            )
        _require_json_value(value["predicate"], "verification property predicate")
        _origin_from_data(value["source_origin"])
        property_kinds[property_id] = kind
        property_generated_from[property_id] = generated_from
    if set(property_kinds) != set(property_ids):
        missing = sorted(set(property_ids) - set(property_kinds))
        extra = sorted(set(property_kinds) - set(property_ids))
        detail = missing[0] if missing else extra[0]
        raise VerificationBundleError(
            f"verification property records do not match property IDs: '{detail}'"
        )

    scopes_value = payload["scopes"]
    if not isinstance(scopes_value, list):
        raise VerificationBundleError("verification scopes must be an array")
    scope_ids: set[str] = set()
    scoped_goal_ids: set[str] = set()
    all_requirement_ids: set[str] = set()
    scope_by_goal: dict[str, tuple[str, str, str, tuple[str, ...]]] = {}
    scope_by_feasibility: dict[str, tuple[str, str, str, tuple[str, ...]]] = {}
    global_requirements_by_domain: dict[tuple[str, str], tuple[str, ...]] = {}
    for value in scopes_value:
        if not isinstance(value, Mapping):
            raise VerificationBundleError("verification scope must be an object")
        _require_exact_keys(
            value,
            required=(
                "id", "name", "clock", "reset", "requirements", "goals",
                "source_origin",
            ),
            description="verification scope",
        )
        scope_id = _require_string(value["id"], "verification scope ID")
        if scope_id in scope_ids:
            raise VerificationBundleError(f"duplicate verification scope '{scope_id}'")
        scope_ids.add(scope_id)
        scope_name = _require_string(value["name"], "verification scope name")
        scope_clock = _require_string(value["clock"], "verification scope clock")
        scope_reset = _require_string(value["reset"], "verification scope reset")
        _origin_from_data(value["source_origin"])
        requirements = value["requirements"]
        goals = value["goals"]
        if not isinstance(requirements, list) or not isinstance(goals, list):
            raise VerificationBundleError(
                f"verification scope '{scope_id}' requirements/goals must be arrays"
            )
        local_ids: set[str] = set()
        for requirement in requirements:
            if not isinstance(requirement, Mapping):
                raise VerificationBundleError("verification requirement must be an object")
            _require_exact_keys(
                requirement,
                required=("id", "name", "source_origin"),
                description="verification requirement",
            )
            item_id = _require_string(requirement["id"], "verification requirement ID")
            if item_id in local_ids:
                raise VerificationBundleError(
                    f"duplicate verification scope member '{item_id}'"
                )
            local_ids.add(item_id)
            if item_id in all_requirement_ids:
                raise VerificationBundleError(
                    f"duplicate verification requirement identity '{item_id}'"
                )
            all_requirement_ids.add(item_id)
            _require_string(requirement["name"], "verification requirement name")
            _origin_from_data(requirement["source_origin"])
        requirement_ids = tuple(
            _require_string(item["id"], "verification requirement ID")
            for item in requirements
        )
        if scope_name == "$module":
            domain_key = (scope_clock, scope_reset)
            if domain_key in global_requirements_by_domain:
                raise VerificationBundleError(
                    "verification payload contains duplicate module-global "
                    f"scope for domain '{scope_clock}/{scope_reset}'"
                )
            global_requirements_by_domain[domain_key] = requirement_ids
        scope_by_feasibility[f"{scope_id}.requirements_feasible"] = (
            scope_id, scope_clock, scope_reset, requirement_ids,
        )
        for goal in goals:
            if not isinstance(goal, Mapping):
                raise VerificationBundleError("verification scope goal must be an object")
            _require_exact_keys(
                goal,
                required=("id", "kind", "name", "source_origin"),
                description="verification scope goal",
            )
            goal_id = _validate_property_id(goal["id"])
            if goal_id in local_ids or goal_id in scoped_goal_ids:
                raise VerificationBundleError(
                    f"duplicate verification scope member '{goal_id}'"
                )
            local_ids.add(goal_id)
            scoped_goal_ids.add(goal_id)
            goal_kind = _require_string(goal["kind"], "verification scope goal kind")
            expected_kind = "cover" if goal_kind == "cover" else "safety"
            if goal_kind not in {"assert", "ensure", "cover"}:
                raise VerificationBundleError(
                    f"verification scope goal '{goal_id}' has unsupported kind '{goal_kind}'"
                )
            if property_kinds.get(goal_id) != expected_kind:
                raise VerificationBundleError(
                    f"verification scope goal '{goal_id}' does not match its property kind"
                )
            _require_string(goal["name"], "verification scope goal name")
            _origin_from_data(goal["source_origin"])
            scope_by_goal[goal_id] = (
                scope_id, scope_clock, scope_reset, requirement_ids,
            )

    bindings = payload["bindings"]
    if not isinstance(bindings, list):
        raise VerificationBundleError("verification bindings must be an array")
    binding_ids: set[str] = set()
    for value in bindings:
        if not isinstance(value, Mapping):
            raise VerificationBundleError("verification binding must be an object")
        _require_exact_keys(
            value,
            required=("semantic_signal_id", "rtl_module", "rtl_name", "width", "direction"),
            description="verification binding",
        )
        semantic_id = _require_string(value["semantic_signal_id"], "semantic signal ID")
        if semantic_id in binding_ids:
            raise VerificationBundleError(
                f"duplicate verification binding '{semantic_id}'"
            )
        binding_ids.add(semantic_id)
        _require_string(value["rtl_module"], "binding RTL module")
        _require_string(value["rtl_name"], "binding RTL name")
        _require_integer(value["width"], "binding width", minimum=1)
        direction = _require_string(value["direction"], "binding direction")
        if direction not in {"input", "output", "internal"}:
            raise VerificationBundleError(
                f"verification binding '{semantic_id}' has invalid direction '{direction}'"
            )

    binding_sets_by_route: dict[str, Mapping[str, object]] = {}
    execution_plan: FormalExecutionPlan | None = None
    if version in {2, 3, 4}:
        binding_sets_value = payload["binding_sets"]
        if not isinstance(binding_sets_value, list):
            raise VerificationBundleError(
                "verification binding sets must be an array"
            )
        for value in binding_sets_value:
            if not isinstance(value, Mapping):
                raise VerificationBundleError(
                    "verification binding set must be an object"
                )
            _require_exact_keys(
                value,
                required=(
                    "route", "backend", "artifact_hash",
                    "binding_identity", "bindings",
                ),
                description="verification binding set",
            )
            route = _validate_identity(value["route"], "verification route identity")
            if route in binding_sets_by_route:
                raise VerificationBundleError(
                    f"duplicate verification binding set route '{route}'"
                )
            _require_string(value["backend"], "verification binding-set backend")
            _validate_identity(
                value["artifact_hash"], "verification binding-set artifact hash"
            )
            binding_identity = _validate_identity(
                value["binding_identity"],
                "verification binding-set identity",
            )
            records = value["bindings"]
            if not isinstance(records, list) or not records:
                raise VerificationBundleError(
                    "verification binding set requires a non-empty bindings array"
                )
            local_ids: set[str] = set()
            for record in records:
                if not isinstance(record, Mapping):
                    raise VerificationBundleError(
                        "verification binding-set record must be an object"
                    )
                _require_exact_keys(
                    record,
                    required=(
                        "semantic_signal_id", "rtl_module", "rtl_name",
                        "width", "direction",
                    ),
                    description="verification binding-set record",
                )
                semantic_id = _require_string(
                    record["semantic_signal_id"], "semantic signal ID"
                )
                if semantic_id in local_ids:
                    raise VerificationBundleError(
                        f"duplicate verification binding '{semantic_id}' in route '{route}'"
                    )
                local_ids.add(semantic_id)
                _require_string(record["rtl_module"], "binding RTL module")
                _require_string(record["rtl_name"], "binding RTL name")
                _require_integer(record["width"], "binding width", minimum=1)
                direction = _require_string(record["direction"], "binding direction")
                if direction not in {"input", "output", "internal"}:
                    raise VerificationBundleError(
                        f"verification binding '{semantic_id}' has invalid direction '{direction}'"
                    )
            computed_binding_identity = "bindings:" + stable_digest(sorted(
                (dict(record) for record in records),
                key=lambda item: str(item["semantic_signal_id"]),
            ))
            if binding_identity != computed_binding_identity:
                raise VerificationBundleError(
                    f"verification binding-set identity does not match route '{route}'"
                )
            binding_sets_by_route[route] = value
        try:
            execution_plan = FormalExecutionPlan.from_data(
                payload["execution_plan"]
            )
        except FormalPlanningError as error:
            raise VerificationBundleError(
                f"invalid formal execution plan: {error}"
            ) from error
        if execution_plan.compilation_identity != hardware["selected_ir_identity"]:
            raise VerificationBundleError(
                "formal execution plan selected-IR identity does not match hardware"
            )
        if version in {3, 4}:
            from zlang.formal_orchestration import (
                CompilerFormalExecutionPlan,
                FormalOrchestrationError,
            )

            try:
                compiler_plan = CompilerFormalExecutionPlan.from_data(
                    payload["compiler_execution_plan"]
                )
            except FormalOrchestrationError as error:
                raise VerificationBundleError(
                    f"invalid compiler formal execution plan: {error}"
                ) from error
            if compiler_plan.verification_plan != execution_plan:
                raise VerificationBundleError(
                    "compiler formal plan does not reference the exact verification plan"
                )
            if compiler_plan.selected_ir_identity != hardware["selected_ir_identity"]:
                raise VerificationBundleError(
                    "compiler formal plan selected-IR identity does not match hardware"
                )
            records_value = (
                payload["candidate_equivalence_records"]
                if version == 4 else []
            )
            if not isinstance(records_value, list):
                raise VerificationBundleError(
                    "candidate equivalence records must be an array"
                )
            records_by_site: dict[str, Mapping[str, object]] = {}
            for record in records_value:
                if not isinstance(record, Mapping):
                    raise VerificationBundleError(
                        "candidate equivalence record must be an object"
                    )
                _require_exact_keys(
                    record,
                    required=(
                        "site_identity", "candidate_identity", "plan_identity",
                        "replay_identity", "logical_path", "content_hash",
                    ),
                    description="candidate equivalence record",
                )
                site = _require_string(
                    record["site_identity"], "candidate equivalence site identity"
                )
                if site in records_by_site:
                    raise VerificationBundleError(
                        f"duplicate candidate equivalence record for site '{site}'"
                    )
                _require_string(
                    record["candidate_identity"], "candidate identity"
                )
                _validate_identity(
                    record["plan_identity"], "candidate equivalence plan identity"
                )
                _validate_identity(
                    record["replay_identity"], "candidate replay identity"
                )
                _validate_relative_path(
                    record["logical_path"],
                    prefix="implementation/companions/candidate-equivalence/",
                )
                _validate_identity(
                    record["content_hash"], "candidate companion content hash"
                )
                records_by_site[site] = record
            plans_by_site = {
                item.site_identity: item
                for item in compiler_plan.candidate_equivalence_plans
            }
            if set(records_by_site) != set(plans_by_site):
                raise VerificationBundleError(
                    "candidate equivalence records do not match compiler plans"
                )
            if tuple(records_by_site) != tuple(plans_by_site):
                raise VerificationBundleError(
                    "candidate equivalence records are not in compiler-plan order"
                )
            for site, plan in plans_by_site.items():
                record = records_by_site[site]
                if (
                    record["candidate_identity"] != plan.candidate_identity
                    or record["plan_identity"] != plan.plan_identity
                ):
                    raise VerificationBundleError(
                        f"candidate equivalence record for site '{site}' differs "
                        "from its compiler plan"
                    )

    dependencies = payload["vacuity_dependencies"]
    if not isinstance(dependencies, Mapping):
        raise VerificationBundleError("vacuity dependencies must be an object")
    for safety_value, cover_value in dependencies.items():
        safety_id = _validate_property_id(safety_value)
        cover_id = _validate_property_id(cover_value)
        if property_kinds.get(safety_id) != "safety":
            raise VerificationBundleError(
                f"vacuity dependency source '{safety_id}' is not a safety property"
            )
        if property_kinds.get(cover_id) != "cover":
            raise VerificationBundleError(
                f"vacuity dependency target '{cover_id}' is not a cover property"
            )
        generated_from = property_generated_from.get(cover_id) or ""
        if not generated_from.startswith("verification-feasibility:"):
            raise VerificationBundleError(
                f"vacuity dependency target '{cover_id}' is not a feasibility cover"
            )

    if jobs is not None:
        jobs_by_id = {item.property_id: item for item in jobs}
        if set(jobs_by_id) != set(property_kinds):
            raise VerificationBundleError(
                "verification jobs do not match verification property records"
            )
        for property_id, kind in property_kinds.items():
            if jobs_by_id[property_id].kind != kind:
                raise VerificationBundleError(
                    f"verification job '{property_id}' kind does not match its property"
                )
        if version in {2, 3, 4}:
            assert execution_plan is not None
            planned_by_property = {
                item.property_identity: item for item in execution_plan.goals
            }
            if set(planned_by_property) != set(property_kinds):
                raise VerificationBundleError(
                    "formal execution-plan goals do not match verification properties"
                )
            for property_id, job in jobs_by_id.items():
                planned = planned_by_property[property_id]
                scoped = scope_by_goal.get(property_id) or scope_by_feasibility.get(
                    property_id
                )
                if scoped is None:
                    expected_scope_id = None
                    expected_clock = planned.clock_domain
                    expected_reset = planned.reset_domain
                    local_requirements: tuple[str, ...] = ()
                else:
                    (
                        expected_scope_id,
                        expected_clock,
                        expected_reset,
                        local_requirements,
                    ) = scoped
                # Recursive M35 scopes are instantiated from each child's
                # frozen formal design.  Their assumptions are already
                # concrete per physical instance and must not be silently
                # augmented with an unrelated root-module contract merely
                # because both happen to use the same textual domain names.
                global_requirements = (
                    ()
                    if (
                        expected_scope_id is not None
                        and expected_scope_id.startswith("recursive-scope:")
                    )
                    else global_requirements_by_domain.get(
                        (expected_clock, expected_reset), ()
                    )
                )
                expected_assumptions = tuple(dict.fromkeys(
                    (*global_requirements, *local_requirements)
                ))
                unknown_assumptions = set(planned.assumption_ids) - all_requirement_ids
                if unknown_assumptions:
                    raise VerificationBundleError(
                        f"verification goal '{property_id}' references unknown "
                        f"assumption '{sorted(unknown_assumptions)[0]}'"
                    )
                if planned.assumption_ids != expected_assumptions:
                    raise VerificationBundleError(
                        f"verification goal '{property_id}' scoped assumptions "
                        "do not match its declared contract/domain"
                    )
                if job.scope_id != expected_scope_id:
                    raise VerificationBundleError(
                        f"verification job '{property_id}' scope does not match "
                        "its declaration"
                    )
                if (
                    planned.clock_domain != expected_clock
                    or planned.reset_domain != expected_reset
                ):
                    raise VerificationBundleError(
                        f"verification goal '{property_id}' domain does not match "
                        "its declared scope"
                    )
                if planned.selected_ir_identity != job.selected_ir_identity:
                    raise VerificationBundleError(
                        f"verification job '{property_id}' selected IR does not match its plan"
                    )
                if tuple(planned.assumption_ids) != tuple(job.assumption_ids):
                    raise VerificationBundleError(
                        f"verification job '{property_id}' assumptions do not match its plan"
                    )
                if planned.clock_domain != job.clock_domain or planned.reset_domain != job.reset_domain:
                    raise VerificationBundleError(
                        f"verification job '{property_id}' domain does not match its plan"
                    )
                if (
                    planned.clock_domain_contract != job.clock_domain_contract
                    or planned.physical_domain_identity
                    != job.physical_domain_identity
                ):
                    raise VerificationBundleError(
                        f"verification job '{property_id}' physical domain does "
                        "not match its plan"
                    )
                if planned.route is None:
                    if job.executable or job.route is not None:
                        raise VerificationBundleError(
                            f"skipped verification goal '{property_id}' has an executable job"
                        )
                    continue
                if not job.executable or job.route != planned.route.identity:
                    raise VerificationBundleError(
                        f"executable verification goal '{property_id}' route mismatch"
                    )
                binding_set = binding_sets_by_route.get(job.route)
                if binding_set is None:
                    raise VerificationBundleError(
                        f"verification job '{property_id}' has no route binding set"
                    )
                artifact = planned.route.artifacts[0]
                for field, expected in (
                    ("backend", artifact.backend),
                    ("artifact_hash", artifact.artifact_identity),
                    ("binding_identity", artifact.binding_identity),
                ):
                    if binding_set[field] != expected or getattr(job, field) != expected:
                        raise VerificationBundleError(
                            f"verification job '{property_id}' {field} does not match its route"
                        )


@dataclass(frozen=True)
class VerificationBundleInput:
    """One caller-supplied immutable file."""

    logical_path: str
    kind: str
    content: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if self.kind not in _FILE_PREFIX or self.kind == "verification_ir":
            raise VerificationBundleError(
                f"unsupported verification bundle file kind '{self.kind}'"
            )
        _validate_relative_path(self.logical_path, prefix=_FILE_PREFIX[self.kind])
        if not isinstance(self.content, bytes):
            raise VerificationBundleError("verification bundle content must be bytes")


@dataclass(frozen=True)
class VerificationBundleFile:
    logical_path: str
    kind: str
    content_hash: str
    size: int

    def __post_init__(self) -> None:
        if self.kind not in _FILE_PREFIX:
            raise VerificationBundleError(
                f"unsupported verification bundle file kind '{self.kind}'"
            )
        if self.kind == "verification_ir":
            if self.logical_path != "verification-ir.json":
                raise VerificationBundleError(
                    "verification IR record must use 'verification-ir.json'"
                )
        else:
            _validate_relative_path(self.logical_path, prefix=_FILE_PREFIX[self.kind])
        if not isinstance(self.content_hash, str) or _HASH.fullmatch(self.content_hash) is None:
            raise VerificationBundleError("verification file hash must be lowercase SHA-256")
        _require_integer(self.size, "verification file size")

    @classmethod
    def from_input(cls, value: VerificationBundleInput) -> "VerificationBundleFile":
        return cls(
            value.logical_path,
            value.kind,
            hashlib.sha256(value.content).hexdigest(),
            len(value.content),
        )

    def to_data(self) -> dict[str, object]:
        return {
            "content_hash": self.content_hash,
            "kind": self.kind,
            "logical_path": self.logical_path,
            "size": self.size,
        }

    @classmethod
    def from_data(cls, data: object) -> "VerificationBundleFile":
        if not isinstance(data, Mapping):
            raise VerificationBundleError("verification file record must be an object")
        _require_exact_keys(
            data,
            required=("logical_path", "kind", "content_hash", "size"),
            description="verification file record",
        )
        return cls(
            _require_string(data["logical_path"], "verification file path"),
            _require_string(data["kind"], "verification file kind"),
            _require_string(data["content_hash"], "verification file hash"),
            _require_integer(data["size"], "verification file size"),
        )


@dataclass(frozen=True)
class VerificationJob:
    """One backend-connected property target stored in a bundle.

    ``kind`` is intentionally open-ended.  This version executes ``safety``
    and ``cover``; other well-formed kinds produce an explicit skipped result.
    """

    property_id: str
    kind: str
    top: str
    source_files: tuple[str, ...] = ()
    config_files: tuple[str, ...] = ()
    source_map_files: tuple[str, ...] = ()
    systemverilog: bool = True
    executable: bool = True
    reason: str | None = None
    source_origin: SourceOrigin | None = None
    route: str | None = None
    backend: str | None = None
    artifact_hash: str | None = None
    binding_identity: str | None = None
    selected_ir_identity: str | None = None
    scope_id: str | None = None
    assumption_ids: tuple[str, ...] = ()
    clock_domain: str | None = None
    reset_domain: str | None = None
    physical_instance_path: tuple[str, ...] = ()
    clock_domain_contract: ClockDomain | None = None
    physical_domain_identity: str | None = None

    def __post_init__(self) -> None:
        _validate_property_id(self.property_id)
        if not isinstance(self.kind, str) or _KIND.fullmatch(self.kind) is None:
            raise VerificationBundleError(
                "verification job kind must be a lowercase identifier"
            )
        if _TOKEN.fullmatch(self.top) is None:
            raise VerificationBundleError(
                f"verification job top '{self.top}' is not a legal RTL identifier"
            )
        if not isinstance(self.systemverilog, bool) or not isinstance(self.executable, bool):
            raise VerificationBundleError(
                "verification job flags must be boolean"
            )
        for path in self.source_files:
            _validate_relative_path(path)
        for path in self.config_files:
            _validate_relative_path(path, prefix=_FILE_PREFIX["config"])
        for path in self.source_map_files:
            _validate_relative_path(path, prefix=_FILE_PREFIX["source_map"])
        all_paths = (*self.source_files, *self.config_files, *self.source_map_files)
        if len(set(all_paths)) != len(all_paths):
            raise VerificationBundleError(
                f"verification job '{self.property_id}' references a file more than once"
            )
        if self.executable:
            if self.reason is not None:
                raise VerificationBundleError(
                    "executable verification jobs cannot carry a skip reason"
                )
            if not self.source_files:
                raise VerificationBundleError(
                    "executable verification jobs require source files"
                )
        elif not self.reason:
            raise VerificationBundleError(
                "non-executable verification jobs require an explicit reason"
            )
        optional_tokens = (
            (self.route, "route"),
            (self.backend, "backend"),
            (self.scope_id, "scope ID"),
            (self.clock_domain, "clock domain"),
            (self.reset_domain, "reset domain"),
        )
        for value, description in optional_tokens:
            if value is not None:
                _require_string(value, f"verification job {description}")
        for value, description in (
            (self.artifact_hash, "artifact hash"),
            (self.binding_identity, "binding identity"),
            (self.selected_ir_identity, "selected IR identity"),
        ):
            if value is not None:
                _validate_identity(value, f"verification job {description}")
        if len(set(self.assumption_ids)) != len(self.assumption_ids):
            raise VerificationBundleError("verification job assumption IDs must be unique")
        for item in self.assumption_ids:
            _validate_property_id(item)
        if any(not item for item in self.physical_instance_path):
            raise VerificationBundleError(
                "verification physical instance path entries must not be empty"
            )
        if self.executable and (self.route is None) != (self.backend is None):
            raise VerificationBundleError(
                "executable verification job route and backend must be supplied together"
            )
        if self.clock_domain_contract is not None:
            if not isinstance(self.clock_domain_contract, ClockDomain):
                raise VerificationBundleError(
                    "verification job clock-domain contract must use typed IR"
                )
            try:
                self.clock_domain_contract.validate()
            except ValueError as error:
                raise VerificationBundleError(str(error)) from error
            if (
                self.clock_domain != self.clock_domain_contract.clock
                or self.reset_domain != self.clock_domain_contract.reset
            ):
                raise VerificationBundleError(
                    "verification job clock/reset names do not match its exact "
                    "clock-domain contract"
                )
        if self.physical_domain_identity is not None:
            _validate_identity(
                self.physical_domain_identity,
                "verification job physical-domain identity",
            )
            if self.clock_domain_contract is None:
                raise VerificationBundleError(
                    "verification job physical-domain identity requires an exact "
                    "clock-domain contract"
                )
            if self.physical_domain_identity != clock_domain_contract_identity(
                self.clock_domain_contract
            ):
                raise VerificationBundleError(
                    "verification job physical-domain identity does not match its "
                    "exact clock-domain contract"
                )

    def to_data(self) -> dict[str, object]:
        result: dict[str, object] = {
            "config_files": list(self.config_files),
            "executable": self.executable,
            "kind": self.kind,
            "property_id": self.property_id,
            "reason": self.reason,
            "source_files": list(self.source_files),
            "source_map_files": list(self.source_map_files),
            "source_origin": _origin_to_data(self.source_origin),
            "systemverilog": self.systemverilog,
            "top": self.top,
        }
        provenance = {
            "route": self.route,
            "backend": self.backend,
            "artifact_hash": self.artifact_hash,
            "binding_identity": self.binding_identity,
            "selected_ir_identity": self.selected_ir_identity,
            "scope_id": self.scope_id,
            "assumption_ids": list(self.assumption_ids),
            "clock_domain": self.clock_domain,
            "reset_domain": self.reset_domain,
            "physical_instance_path": list(self.physical_instance_path),
            "clock_domain_contract": clock_domain_data(
                self.clock_domain_contract
            ),
            "physical_domain_identity": self.physical_domain_identity,
        }
        # Preserve exact identity/replay compatibility for historical bundles
        # whose jobs predate route provenance.
        if any(value not in (None, [], ()) for value in provenance.values()):
            result.update(provenance)
        return result

    @classmethod
    def from_data(cls, data: object) -> "VerificationJob":
        if not isinstance(data, Mapping):
            raise VerificationBundleError("verification job must be an object")
        legacy_keys = (
            "property_id", "kind", "top", "source_files", "config_files",
            "source_map_files", "systemverilog", "executable", "reason",
            "source_origin",
        )
        provenance_keys = (
            "route", "backend", "artifact_hash", "binding_identity",
            "selected_ir_identity", "scope_id", "assumption_ids",
            "clock_domain", "reset_domain", "physical_instance_path",
        )
        domain_provenance_keys = (
            *provenance_keys,
            "clock_domain_contract", "physical_domain_identity",
        )
        actual = frozenset(data)
        if actual not in {
            frozenset(legacy_keys),
            frozenset((*legacy_keys, *provenance_keys)),
            frozenset((*legacy_keys, *domain_provenance_keys)),
        }:
            _require_exact_keys(
                data,
                required=(*legacy_keys, *domain_provenance_keys),
                description="verification job",
            )

        def paths(name: str) -> tuple[str, ...]:
            value = data[name]
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise VerificationBundleError(
                    f"verification job {name} must be an array of strings"
                )
            return tuple(value)

        reason = data["reason"]
        if reason is not None and not isinstance(reason, str):
            raise VerificationBundleError("verification job reason must be a string")
        def optional_string(name: str) -> str | None:
            value = data.get(name)
            if value is not None and not isinstance(value, str):
                raise VerificationBundleError(
                    f"verification job {name} must be a string"
                )
            return value

        def string_tuple(name: str) -> tuple[str, ...]:
            value = data.get(name, [])
            if not isinstance(value, list) or any(
                not isinstance(item, str) for item in value
            ):
                raise VerificationBundleError(
                    f"verification job {name} must be an array of strings"
                )
            return tuple(value)

        return cls(
            _require_string(data["property_id"], "verification property ID"),
            _require_string(data["kind"], "verification job kind"),
            _require_string(data["top"], "verification job top"),
            paths("source_files"),
            paths("config_files"),
            paths("source_map_files"),
            data["systemverilog"],  # type: ignore[arg-type]
            data["executable"],  # type: ignore[arg-type]
            reason,
            _origin_from_data(data["source_origin"]),
            optional_string("route"),
            optional_string("backend"),
            optional_string("artifact_hash"),
            optional_string("binding_identity"),
            optional_string("selected_ir_identity"),
            optional_string("scope_id"),
            string_tuple("assumption_ids"),
            optional_string("clock_domain"),
            optional_string("reset_domain"),
            string_tuple("physical_instance_path"),
            _clock_domain_from_job_data(data.get("clock_domain_contract")),
            optional_string("physical_domain_identity"),
        )


def verification_identity_for(
    *,
    top: str,
    hardware_identity: str,
    property_ids: Iterable[str],
    payload: Mapping[str, object],
) -> str:
    """Compute the verification-only identity for one IR snapshot."""

    if _TOKEN.fullmatch(top) is None:
        raise VerificationBundleError(f"verification top '{top}' is not a legal identifier")
    hardware_identity = _validate_identity(hardware_identity, "hardware identity")
    properties = tuple(sorted(_validate_property_id(item) for item in property_ids))
    if not properties:
        raise VerificationBundleError("verification IR requires at least one property")
    if len(set(properties)) != len(properties):
        raise VerificationBundleError("verification property IDs must be unique")
    if not isinstance(payload, Mapping):
        raise VerificationBundleError("verification IR payload must be an object")
    _require_json_value(payload, "verification IR payload")
    _validate_verification_payload(payload, property_ids=properties)
    hardware = payload["hardware"]
    assert isinstance(hardware, Mapping)
    if hardware["selected_ir_identity"] != hardware_identity:
        raise VerificationBundleError(
            "verification payload selected IR identity does not match hardware identity"
        )
    return "verification:" + stable_digest({
        "schema": VERIFICATION_IR_SCHEMA,
        "schema_version": VERIFICATION_IR_SCHEMA_VERSION,
        "top": top,
        "hardware_identity": hardware_identity,
        "property_ids": properties,
        "payload": _identity_payload(payload),
    })


def _verification_ir_data(
    *,
    top: str,
    hardware_identity: str,
    verification_identity: str,
    property_ids: tuple[str, ...],
    payload: Mapping[str, object],
) -> dict[str, object]:
    return {
        "hardware_identity": hardware_identity,
        "payload": payload,
        "property_ids": list(property_ids),
        "schema": VERIFICATION_IR_SCHEMA,
        "schema_version": VERIFICATION_IR_SCHEMA_VERSION,
        "top": top,
        "verification_identity": verification_identity,
    }


def _validate_verification_ir(data: object) -> dict[str, object]:
    if not isinstance(data, Mapping):
        raise VerificationBundleError("verification-ir.json must contain an object")
    keys = (
        "schema", "schema_version", "top", "hardware_identity",
        "verification_identity", "property_ids", "payload",
    )
    _require_exact_keys(data, required=keys, description="verification IR snapshot")
    if data["schema"] != VERIFICATION_IR_SCHEMA:
        raise VerificationBundleError("unsupported verification IR schema")
    if data["schema_version"] != VERIFICATION_IR_SCHEMA_VERSION:
        raise VerificationBundleError("unsupported verification IR schema version")
    top = _require_string(data["top"], "verification IR top")
    hardware_identity = _validate_identity(data["hardware_identity"], "hardware identity")
    verification_identity = _validate_identity(
        data["verification_identity"], "verification identity"
    )
    property_values = data["property_ids"]
    if not isinstance(property_values, list):
        raise VerificationBundleError("verification IR property_ids must be an array")
    property_ids = tuple(_validate_property_id(item) for item in property_values)
    if tuple(sorted(property_ids)) != property_ids or len(set(property_ids)) != len(property_ids):
        raise VerificationBundleError(
            "verification IR property IDs must be unique and sorted"
        )
    payload = data["payload"]
    if not isinstance(payload, Mapping):
        raise VerificationBundleError("verification IR payload must be an object")
    _require_json_value(payload, "verification IR payload")
    expected = verification_identity_for(
        top=top,
        hardware_identity=hardware_identity,
        property_ids=property_ids,
        payload=payload,
    )
    if verification_identity != expected:
        raise VerificationBundleError(
            "verification identity does not match verification IR contents"
        )
    return dict(data)


@dataclass(frozen=True)
class VerificationBundleManifest:
    top: str
    hardware_identity: str
    verification_identity: str
    property_ids: tuple[str, ...]
    verification_ir: VerificationBundleFile
    files: tuple[VerificationBundleFile, ...]
    jobs: tuple[VerificationJob, ...]
    source_identity: str
    dependency_identity: str
    compiler_identity: str
    bundle_identity: str | None = None
    schema: str = field(default=VERIFICATION_BUNDLE_SCHEMA, init=False)
    schema_version: int = field(default=VERIFICATION_BUNDLE_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        if _TOKEN.fullmatch(self.top) is None:
            raise VerificationBundleError(
                f"verification bundle top '{self.top}' is not a legal identifier"
            )
        _validate_identity(self.hardware_identity, "hardware identity")
        _validate_identity(self.verification_identity, "verification identity")
        _validate_identity(self.source_identity, "source identity")
        _validate_identity(self.dependency_identity, "dependency identity")
        _validate_identity(self.compiler_identity, "compiler identity")
        if tuple(sorted(self.property_ids)) != self.property_ids:
            raise VerificationBundleError("verification property IDs must be sorted")
        if not self.property_ids:
            raise VerificationBundleError(
                "verification bundle requires at least one property"
            )
        if len(set(self.property_ids)) != len(self.property_ids):
            raise VerificationBundleError("verification property IDs must be unique")
        for item in self.property_ids:
            _validate_property_id(item)
        if self.verification_ir.logical_path != "verification-ir.json":
            raise VerificationBundleError(
                "verification IR record must use 'verification-ir.json'"
            )
        if self.verification_ir.kind != "verification_ir":
            raise VerificationBundleError("verification IR record has the wrong kind")
        file_paths = tuple(item.logical_path for item in self.files)
        if tuple(sorted(file_paths)) != file_paths or len(set(file_paths)) != len(file_paths):
            raise VerificationBundleError(
                "verification bundle files must be unique and sorted"
            )
        job_ids = tuple(item.property_id for item in self.jobs)
        if tuple(sorted(job_ids)) != job_ids or len(set(job_ids)) != len(job_ids):
            raise VerificationBundleError("verification jobs must be unique and sorted")
        if job_ids != self.property_ids:
            raise VerificationBundleError(
                "verification jobs must describe every property exactly once"
            )
        files_by_path = {item.logical_path: item for item in self.files}
        referenced: set[str] = set()
        for job in self.jobs:
            source_kinds: set[str] = set()
            for path in job.source_files:
                record = files_by_path.get(path)
                if record is None:
                    raise VerificationBundleError(
                        f"verification job '{job.property_id}' references missing file '{path}'"
                    )
                if record.kind not in {"implementation", "harness", "companion"}:
                    raise VerificationBundleError(
                        f"verification source '{path}' is not implementation, "
                        "harness, or companion input"
                    )
                source_kinds.add(record.kind)
                referenced.add(path)
            if job.executable and not {"implementation", "harness"}.issubset(
                source_kinds
            ):
                raise VerificationBundleError(
                    f"executable verification job '{job.property_id}' requires both "
                    "implementation and harness sources"
                )
            for path in job.config_files:
                record = files_by_path.get(path)
                if record is None or record.kind != "config":
                    raise VerificationBundleError(
                        f"verification job '{job.property_id}' references invalid config '{path}'"
                    )
                referenced.add(path)
            for path in job.source_map_files:
                record = files_by_path.get(path)
                if record is None or record.kind != "source_map":
                    raise VerificationBundleError(
                        f"verification job '{job.property_id}' references invalid source map '{path}'"
                    )
                referenced.add(path)
        # Version-4 candidate replay records are companion metadata rather
        # than M35 source/config inputs. Full bundle validation cross-links
        # every such file through the verification-IR record after loading.
        unreferenced = sorted(
            path for path in set(files_by_path) - referenced
            if files_by_path[path].kind != "companion"
        )
        if unreferenced:
            raise VerificationBundleError(
                f"verification bundle file '{unreferenced[0]}' is not referenced by a job"
            )
        expected_identity = self.computed_identity
        if self.bundle_identity is None:
            object.__setattr__(self, "bundle_identity", expected_identity)
        elif self.bundle_identity != expected_identity:
            raise VerificationBundleError(
                "verification bundle identity does not match manifest contents"
            )

    def identity_data(self) -> dict[str, object]:
        return {
            "files": [item.to_data() for item in self.files],
            "compiler_identity": self.compiler_identity,
            "dependency_identity": self.dependency_identity,
            "hardware_identity": self.hardware_identity,
            "jobs": [item.to_data() for item in self.jobs],
            "property_ids": list(self.property_ids),
            "schema": self.schema,
            "schema_version": self.schema_version,
            "source_identity": self.source_identity,
            "top": self.top,
            "verification_identity": self.verification_identity,
            "verification_ir": self.verification_ir.to_data(),
        }

    @property
    def computed_identity(self) -> str:
        return "verification-bundle:" + stable_digest(self.identity_data())

    def to_data(self) -> dict[str, object]:
        return {"bundle_identity": self.bundle_identity, **self.identity_data()}

    def to_json(self) -> str:
        return stable_json(self.to_data(), indent=2) + "\n"

    @classmethod
    def from_data(cls, data: object) -> "VerificationBundleManifest":
        if not isinstance(data, Mapping):
            raise VerificationBundleError("verification manifest must be an object")
        keys = (
            "schema", "schema_version", "bundle_identity", "top",
            "hardware_identity", "verification_identity", "property_ids",
            "verification_ir", "files", "jobs", "source_identity",
            "dependency_identity", "compiler_identity",
        )
        _require_exact_keys(data, required=keys, description="verification manifest")
        if data["schema"] != VERIFICATION_BUNDLE_SCHEMA:
            raise VerificationBundleError("unsupported verification bundle schema")
        if data["schema_version"] != VERIFICATION_BUNDLE_SCHEMA_VERSION:
            raise VerificationBundleError("unsupported verification bundle schema version")
        property_values = data["property_ids"]
        file_values = data["files"]
        job_values = data["jobs"]
        if not isinstance(property_values, list):
            raise VerificationBundleError("manifest property_ids must be an array")
        if not isinstance(file_values, list):
            raise VerificationBundleError("manifest files must be an array")
        if not isinstance(job_values, list):
            raise VerificationBundleError("manifest jobs must be an array")
        return cls(
            _require_string(data["top"], "verification manifest top"),
            _require_string(data["hardware_identity"], "hardware identity"),
            _require_string(data["verification_identity"], "verification identity"),
            tuple(_validate_property_id(item) for item in property_values),
            VerificationBundleFile.from_data(data["verification_ir"]),
            tuple(VerificationBundleFile.from_data(item) for item in file_values),
            tuple(VerificationJob.from_data(item) for item in job_values),
            _require_string(data["source_identity"], "source identity"),
            _require_string(data["dependency_identity"], "dependency identity"),
            _require_string(data["compiler_identity"], "compiler identity"),
            _require_string(data["bundle_identity"], "verification bundle identity"),
        )

    @classmethod
    def from_json(cls, text: str) -> "VerificationBundleManifest":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise VerificationBundleError(
                f"invalid verification manifest JSON at line {error.lineno} column {error.colno}"
            ) from error
        return cls.from_data(value)


@dataclass(frozen=True)
class LoadedVerificationBundle:
    directory: Path
    manifest: VerificationBundleManifest
    verification_ir: Mapping[str, object]

    def read_bytes(self, logical_path: str) -> bytes:
        path = _validate_relative_path(logical_path)
        record = next(
            (item for item in self.manifest.files if item.logical_path == path),
            None,
        )
        if record is None:
            raise VerificationBundleError(
                f"verification bundle does not publish '{path}'"
            )
        value = self.directory / path
        try:
            content = value.read_bytes()
        except OSError as error:
            raise VerificationBundleError(
                f"cannot read verification bundle file '{path}': {error}"
            ) from error
        if len(content) != record.size or hashlib.sha256(content).hexdigest() != record.content_hash:
            raise VerificationBundleError(
                f"verification bundle file '{path}' does not match its manifest"
            )
        return content


def _candidate_replay_records(
    payload: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    if payload.get("formal_ir_version") != 4:
        return ()
    values = payload.get("candidate_equivalence_records")
    if not isinstance(values, list):
        raise VerificationBundleError(
            "candidate equivalence records must be an array"
        )
    return tuple(values)  # already structurally checked by payload validation


def _validate_candidate_replay_files(
    payload: Mapping[str, object],
    *,
    files: tuple[VerificationBundleFile, ...],
    jobs: tuple[VerificationJob, ...],
    read_bytes: object,
) -> tuple[object, ...]:
    """Decode and cross-link immutable candidate companions to exact plans."""

    from zlang.candidate_equivalence import FrozenCandidateEquivalenceSite
    from zlang.formal_orchestration import FormalOrchestrationError

    records = _candidate_replay_records(payload)
    files_by_path = {item.logical_path: item for item in files}
    referenced = {
        path
        for job in jobs
        for path in (*job.source_files, *job.config_files, *job.source_map_files)
    }
    replay_paths = {str(item["logical_path"]) for item in records}
    unreferenced = set(files_by_path) - referenced
    if unreferenced != replay_paths:
        detail = sorted(unreferenced ^ replay_paths)
        raise VerificationBundleError(
            "candidate companion file set differs from replay records"
            + ("" if not detail else f": '{detail[0]}'")
        )
    restored = []
    for record in records:
        path = str(record["logical_path"])
        file_record = files_by_path.get(path)
        if file_record is None or file_record.kind != "companion":
            raise VerificationBundleError(
                f"candidate replay companion '{path}' is missing"
            )
        if file_record.content_hash != record["content_hash"]:
            raise VerificationBundleError(
                f"candidate replay companion '{path}' hash differs from its record"
            )
        try:
            content = read_bytes(path)
            if not isinstance(content, bytes):
                raise TypeError("candidate replay reader did not return bytes")
            value = json.loads(content.decode("utf-8"))
            site = FrozenCandidateEquivalenceSite.from_data(value)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise VerificationBundleError(
                f"invalid candidate replay companion '{path}': {error}"
            ) from error
        if (
            site.plan.site_identity != record["site_identity"]
            or site.plan.candidate_identity != record["candidate_identity"]
            or site.plan.plan_identity != record["plan_identity"]
            or site.replay_identity != record["replay_identity"]
        ):
            raise VerificationBundleError(
                f"candidate replay companion '{path}' differs from its record"
            )
        restored.append(site)
    return tuple(restored)


def load_candidate_equivalence_replay(
    bundle: LoadedVerificationBundle | Path,
) -> tuple[object, ...]:
    """Load strict frozen M36 inputs without compiling source."""

    loaded = load_verification_bundle(bundle) if isinstance(bundle, Path) else bundle
    payload = loaded.verification_ir.get("payload")
    if not isinstance(payload, Mapping):
        raise VerificationBundleError("verification bundle payload is invalid")
    return _validate_candidate_replay_files(
        payload,
        files=loaded.manifest.files,
        jobs=loaded.manifest.jobs,
        read_bytes=loaded.read_bytes,
    )


def _listed_files(directory: Path) -> set[str]:
    root = Path(directory)
    if not root.is_dir():
        raise VerificationBundleError(f"verification bundle directory is missing: {root}")
    result: set[str] = set()
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in tuple(directories):
            path = current_path / name
            if stat.S_ISLNK(path.lstat().st_mode):
                raise VerificationBundleError(
                    f"verification bundle contains symbolic link '{path.relative_to(root)}'"
                )
        for name in files:
            path = current_path / name
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise VerificationBundleError(
                    f"verification bundle contains non-regular file '{path.relative_to(root)}'"
                )
            result.add(path.relative_to(root).as_posix())
    return result


def publish_verification_bundle(
    directory: Path,
    *,
    top: str,
    hardware_identity: str,
    verification_identity: str,
    property_ids: Iterable[str],
    verification_ir: Mapping[str, object],
    files: Iterable[VerificationBundleInput],
    jobs: Iterable[VerificationJob],
) -> VerificationBundleManifest:
    """Publish a deterministic immutable bundle, rejecting collisions."""

    properties = tuple(sorted(_validate_property_id(item) for item in property_ids))
    if len(set(properties)) != len(properties):
        raise VerificationBundleError("verification property IDs must be unique")
    expected_verification_identity = verification_identity_for(
        top=top,
        hardware_identity=hardware_identity,
        property_ids=properties,
        payload=verification_ir,
    )
    if verification_identity != expected_verification_identity:
        raise VerificationBundleError(
            "verification identity does not match verification IR contents"
        )
    inputs = tuple(files)
    paths = tuple(item.logical_path for item in inputs)
    if len(set(paths)) != len(paths):
        raise VerificationBundleError("verification bundle input paths must be unique")
    records = tuple(sorted(
        (VerificationBundleFile.from_input(item) for item in inputs),
        key=lambda item: item.logical_path,
    ))
    ordered_jobs = tuple(sorted(jobs, key=lambda item: item.property_id))
    _validate_verification_payload(
        verification_ir,
        property_ids=properties,
        jobs=ordered_jobs,
    )
    content_by_path = {item.logical_path: item.content for item in inputs}
    _validate_candidate_replay_files(
        verification_ir,
        files=records,
        jobs=ordered_jobs,
        read_bytes=lambda path: content_by_path[path],
    )
    ir_data = _verification_ir_data(
        top=top,
        hardware_identity=hardware_identity,
        verification_identity=verification_identity,
        property_ids=properties,
        payload=verification_ir,
    )
    ir_content = (stable_json(ir_data, indent=2) + "\n").encode("utf-8")
    ir_record = VerificationBundleFile(
        "verification-ir.json",
        "verification_ir",
        hashlib.sha256(ir_content).hexdigest(),
        len(ir_content),
    )
    identities = verification_ir.get("identities", {})
    if not isinstance(identities, Mapping):
        raise VerificationBundleError("verification identities must be an object")
    source_identity = identities.get("source")
    dependency_identity = identities.get("dependency")
    compiler_identity = identities.get("compiler")
    if source_identity is None:
        source_identity = "source:" + stable_digest({
            "top": top, "hardware_identity": hardware_identity,
        })
    if dependency_identity is None:
        dependency_identity = "dependency:" + stable_digest({
            "hardware_identity": hardware_identity,
        })
    if compiler_identity is None:
        compiler_identity = "compiler:" + stable_digest({
            "verification_ir_schema": VERIFICATION_IR_SCHEMA,
        })
    manifest = VerificationBundleManifest(
        top,
        hardware_identity,
        verification_identity,
        properties,
        ir_record,
        records,
        ordered_jobs,
        _validate_identity(source_identity, "source identity"),
        _validate_identity(dependency_identity, "dependency identity"),
        _validate_identity(compiler_identity, "compiler identity"),
    )
    payloads = [
        (Path(item.logical_path), source.content)
        for item, source in zip(
            records,
            sorted(inputs, key=lambda value: value.logical_path),
            strict=True,
        )
    ]
    payloads.extend((
        (Path("verification-ir.json"), ir_content),
        (Path("manifest.json"), manifest.to_json().encode("utf-8")),
    ))
    expected_paths = {path.as_posix() for path, _ in payloads}
    destination = Path(directory)
    if destination.exists():
        actual_before = _listed_files(destination)
        unexpected_before = sorted(actual_before - expected_paths)
        if unexpected_before:
            raise VerificationBundleError(
                "verification bundle directory contains an unexpected file set: "
                f"'{unexpected_before[0]}'"
            )
    try:
        publish_relative_files(directory, payloads, existing="identical")
    except SafePublicationError as error:
        raise VerificationBundleError(str(error)) from error
    actual_paths = _listed_files(directory)
    if actual_paths != expected_paths:
        unexpected = sorted(actual_paths - expected_paths)
        missing = sorted(expected_paths - actual_paths)
        detail = unexpected[0] if unexpected else missing[0]
        raise VerificationBundleError(
            f"verification bundle directory contains an unexpected file set: '{detail}'"
        )
    return manifest


def load_verification_bundle(directory: Path) -> LoadedVerificationBundle:
    """Load and fully revalidate an immutable bundle before execution."""

    root = Path(directory)
    manifest_path = root / "manifest.json"
    try:
        if stat.S_ISLNK(manifest_path.lstat().st_mode):
            raise VerificationBundleError("verification manifest must not be a symbolic link")
        manifest = VerificationBundleManifest.from_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except FileNotFoundError as error:
        raise VerificationBundleError("verification bundle is missing manifest.json") from error
    except UnicodeDecodeError as error:
        raise VerificationBundleError("verification manifest is not UTF-8") from error
    except OSError as error:
        raise VerificationBundleError(f"cannot read verification manifest: {error}") from error
    all_records = (manifest.verification_ir, *manifest.files)
    try:
        validate_relative_hashes(
            root,
            ((Path(item.logical_path), item.content_hash) for item in all_records),
        )
    except SafePublicationError as error:
        raise VerificationBundleError(str(error)) from error
    for item in all_records:
        try:
            actual_size = (root / item.logical_path).stat().st_size
        except OSError as error:
            raise VerificationBundleError(
                f"cannot inspect verification bundle file '{item.logical_path}': {error}"
            ) from error
        if actual_size != item.size:
            raise VerificationBundleError(
                f"verification bundle file '{item.logical_path}' has the wrong size"
            )
    expected_paths = {"manifest.json", *(item.logical_path for item in all_records)}
    actual_paths = _listed_files(root)
    if actual_paths != expected_paths:
        unexpected = sorted(actual_paths - expected_paths)
        missing = sorted(expected_paths - actual_paths)
        detail = unexpected[0] if unexpected else missing[0]
        raise VerificationBundleError(
            f"verification bundle directory contains an unexpected file set: '{detail}'"
        )
    try:
        ir_text = (root / manifest.verification_ir.logical_path).read_text(encoding="utf-8")
        ir_data = _validate_verification_ir(json.loads(ir_text))
    except json.JSONDecodeError as error:
        raise VerificationBundleError(
            f"invalid verification IR JSON at line {error.lineno} column {error.colno}"
        ) from error
    except UnicodeDecodeError as error:
        raise VerificationBundleError("verification IR is not UTF-8") from error
    if (
        ir_data["top"] != manifest.top
        or ir_data["hardware_identity"] != manifest.hardware_identity
        or ir_data["verification_identity"] != manifest.verification_identity
        or tuple(ir_data["property_ids"]) != manifest.property_ids
    ):
        raise VerificationBundleError(
            "verification IR identity fields do not match the manifest"
        )
    payload = ir_data["payload"]
    assert isinstance(payload, Mapping)
    _validate_verification_payload(
        payload,
        property_ids=manifest.property_ids,
        jobs=manifest.jobs,
    )
    loaded = LoadedVerificationBundle(root, manifest, ir_data)
    _validate_candidate_replay_files(
        payload,
        files=manifest.files,
        jobs=manifest.jobs,
        read_bytes=loaded.read_bytes,
    )
    return loaded


@dataclass(frozen=True)
class VerificationRunConfig:
    mode: ProofMode = ProofMode.BMC
    engine: str = "sby"
    solver: str = "z3"
    depth: int = 20
    timeout_seconds: int = 120
    jobs: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ProofMode):
            raise VerificationBundleError("verification mode must be ProofMode")
        for value, description in ((self.engine, "engine"), (self.solver, "solver")):
            if not isinstance(value, str) or not value or any(character.isspace() for character in value):
                raise VerificationBundleError(
                    f"verification {description} must be one non-empty token"
                )
        _require_integer(self.depth, "verification depth", minimum=1)
        _require_integer(self.timeout_seconds, "verification timeout", minimum=1)
        _require_integer(self.jobs, "verification jobs", minimum=1)

    def to_data(self) -> dict[str, object]:
        return {
            "depth": self.depth,
            "engine": self.engine,
            "mode": self.mode.value,
            "solver": self.solver,
            "timeout_seconds": self.timeout_seconds,
            "jobs": self.jobs,
        }

    @classmethod
    def from_data(cls, data: object) -> "VerificationRunConfig":
        if not isinstance(data, Mapping):
            raise VerificationBundleError("verification run config must be an object")
        legacy = {"depth", "engine", "mode", "solver", "timeout_seconds"}
        current = {*legacy, "jobs"}
        if frozenset(data) not in {frozenset(legacy), frozenset(current)}:
            _require_exact_keys(
                data, required=current, description="verification run config"
            )
        try:
            mode = ProofMode(_require_string(data["mode"], "verification mode"))
        except ValueError as error:
            raise VerificationBundleError("unsupported verification proof mode") from error
        return cls(
            mode=mode,
            engine=_require_string(data["engine"], "verification engine"),
            solver=_require_string(data["solver"], "verification solver"),
            depth=_require_integer(data["depth"], "verification depth", minimum=1),
            timeout_seconds=_require_integer(
                data["timeout_seconds"], "verification timeout", minimum=1
            ),
            jobs=_require_integer(data.get("jobs", 1), "verification jobs", minimum=1),
        )


@dataclass(frozen=True)
class VerificationCounterexampleMetadata:
    """Executor-owned interpretation of one M35 counterexample frame."""

    sample_cycle: int | None = None
    reset_state: str | None = None
    comparison_valid_state: str | None = None

    def __post_init__(self) -> None:
        if self.sample_cycle is not None:
            _require_integer(
                self.sample_cycle,
                "verification counterexample sample cycle",
                minimum=0,
            )
        for value, description in (
            (self.reset_state, "reset state"),
            (self.comparison_valid_state, "comparison-valid state"),
        ):
            if value is not None:
                _require_string(value, f"verification counterexample {description}")


@dataclass(frozen=True)
class VerificationJobResult:
    property_id: str
    kind: str
    status: str
    mode: str
    engine: str
    solver: str
    depth: int
    reason: str | None = None
    counterexample: Counterexample | None = None
    witness: CoverWitness | None = None
    source_origin: SourceOrigin | None = None
    tool_versions: tuple[tuple[str, str], ...] = ()
    work_directory: str | None = None
    route: str | None = None
    backend: str | None = None
    artifact_hash: str | None = None
    binding_identity: str | None = None
    selected_ir_identity: str | None = None
    scope_id: str | None = None
    assumption_ids: tuple[str, ...] = ()
    clock_domain: str | None = None
    reset_domain: str | None = None
    physical_instance_path: tuple[str, ...] = ()
    counterexample_metadata: VerificationCounterexampleMetadata | None = None
    clock_domain_contract: ClockDomain | None = None
    physical_domain_identity: str | None = None

    def __post_init__(self) -> None:
        _validate_property_id(self.property_id)
        if not isinstance(self.kind, str) or _KIND.fullmatch(self.kind) is None:
            raise VerificationBundleError(
                "verification result kind must be a lowercase identifier"
            )
        _require_string(self.mode, "verification result mode")
        _require_string(self.engine, "verification result engine")
        _require_string(self.solver, "verification result solver")
        _require_integer(self.depth, "verification result depth", minimum=1)
        if self.reason is not None and not self.reason:
            raise VerificationBundleError("verification result reason must not be empty")

        safety_statuses = {
            FormalStatus.FAILED.value,
            FormalStatus.BOUNDED_PASS.value,
            FormalStatus.PROVEN.value,
            FormalStatus.UNKNOWN.value,
            FormalStatus.SKIPPED.value,
        }
        cover_statuses = {
            CoverStatus.WITNESSED.value,
            CoverStatus.BOUNDED_UNREACHED.value,
            CoverStatus.UNKNOWN.value,
            CoverStatus.SKIPPED.value,
        }
        if self.kind == "safety":
            if self.status not in safety_statuses:
                raise VerificationBundleError(
                    f"unsupported safety verification status '{self.status}'"
                )
            if self.mode not in {ProofMode.BMC.value, ProofMode.PROVE.value}:
                raise VerificationBundleError(
                    f"safety result has invalid proof mode '{self.mode}'"
                )
            if self.status == FormalStatus.BOUNDED_PASS.value and self.mode != ProofMode.BMC.value:
                raise VerificationBundleError("bounded_pass is only valid for BMC mode")
            if self.status == FormalStatus.PROVEN.value and self.mode != ProofMode.PROVE.value:
                raise VerificationBundleError("proven is only valid for prove mode")
            if self.witness is not None:
                raise VerificationBundleError("safety results cannot carry a cover witness")
            if (self.counterexample is not None) != (
                self.status == FormalStatus.FAILED.value
            ):
                raise VerificationBundleError(
                    "failed safety results require exactly one counterexample"
                )
        elif self.kind == "cover":
            if self.status not in cover_statuses:
                raise VerificationBundleError(
                    f"unsupported cover verification status '{self.status}'"
                )
            if self.mode != "cover":
                raise VerificationBundleError("cover results require mode 'cover'")
            if self.counterexample is not None:
                raise VerificationBundleError("cover results cannot carry a counterexample")
            if (self.witness is not None) != (
                self.status == CoverStatus.WITNESSED.value
            ):
                raise VerificationBundleError(
                    "witnessed cover results require exactly one witness"
                )
        else:
            if self.status != FormalStatus.SKIPPED.value:
                raise VerificationBundleError(
                    f"unsupported verification job kind '{self.kind}' must be skipped"
                )
            if self.mode not in {ProofMode.BMC.value, ProofMode.PROVE.value}:
                raise VerificationBundleError(
                    f"unsupported verification result has invalid mode '{self.mode}'"
                )
            if self.counterexample is not None or self.witness is not None:
                raise VerificationBundleError(
                    "unsupported verification results cannot carry trace metadata"
                )
        if self.counterexample is not None and self.counterexample.property_id != self.property_id:
            raise VerificationBundleError(
                "verification counterexample property does not match its result"
            )
        if self.counterexample_metadata is not None:
            if self.counterexample is None:
                raise VerificationBundleError(
                    "verification counterexample metadata requires a counterexample"
                )
            if not isinstance(
                self.counterexample_metadata, VerificationCounterexampleMetadata
            ):
                raise VerificationBundleError(
                    "verification counterexample metadata has invalid type"
                )
        if self.witness is not None and self.witness.property_id != self.property_id:
            raise VerificationBundleError(
                "verification witness property does not match its result"
            )
        names: set[str] = set()
        for name, value in self.tool_versions:
            _require_string(name, "verification job tool name")
            _require_string(value, "verification job tool version")
            if name in names:
                raise VerificationBundleError(
                    f"duplicate verification job tool version '{name}'"
                )
            names.add(name)
        if self.work_directory is not None and not self.work_directory:
            raise VerificationBundleError(
                "verification job work directory must not be empty"
            )
        for value, description in (
            (self.route, "route"),
            (self.backend, "backend"),
            (self.scope_id, "scope ID"),
            (self.clock_domain, "clock domain"),
            (self.reset_domain, "reset domain"),
        ):
            if value is not None:
                _require_string(value, f"verification result {description}")
        for value, description in (
            (self.artifact_hash, "artifact hash"),
            (self.binding_identity, "binding identity"),
            (self.selected_ir_identity, "selected IR identity"),
        ):
            if value is not None:
                _validate_identity(value, f"verification result {description}")
        if len(set(self.assumption_ids)) != len(self.assumption_ids):
            raise VerificationBundleError(
                "verification result assumption IDs must be unique"
            )
        for item in self.assumption_ids:
            _validate_property_id(item)
        if any(not item for item in self.physical_instance_path):
            raise VerificationBundleError(
                "verification result physical instance path entries must not be empty"
            )
        if self.clock_domain_contract is not None:
            if not isinstance(self.clock_domain_contract, ClockDomain):
                raise VerificationBundleError(
                    "verification result clock-domain contract has invalid type"
                )
            try:
                self.clock_domain_contract.validate()
            except ValueError as error:
                raise VerificationBundleError(
                    f"verification result clock-domain contract is invalid: {error}"
                ) from error
            if (
                self.clock_domain != self.clock_domain_contract.clock
                or self.reset_domain != self.clock_domain_contract.reset
            ):
                raise VerificationBundleError(
                    "verification result logical and physical domains disagree"
                )
        if self.physical_domain_identity is not None:
            _validate_identity(
                self.physical_domain_identity,
                "verification result physical domain identity",
            )
            if self.clock_domain_contract is None:
                raise VerificationBundleError(
                    "verification result physical domain identity requires its "
                    "exact contract"
                )
            if self.physical_domain_identity != clock_domain_contract_identity(
                self.clock_domain_contract
            ):
                raise VerificationBundleError(
                    "verification result physical domain identity does not match "
                    "its exact contract"
                )

    @classmethod
    def from_formal_result(
        cls, job: VerificationJob, result: FormalResult,
        *, work_directory: Path | None = None,
        trace_snapshot: FormalTraceSnapshot | None = None,
    ) -> "VerificationJobResult":
        return cls(
            job.property_id,
            job.kind,
            result.status.value,
            result.mode.value,
            result.engine or "",
            result.solver or "",
            result.depth or 0,
            result.reason,
            result.counterexample,
            None,
            result.source_origin,
            result.tool_versions,
            None if work_directory is None else str(work_directory),
            job.route,
            job.backend,
            job.artifact_hash,
            job.binding_identity,
            job.selected_ir_identity,
            job.scope_id,
            job.assumption_ids,
            job.clock_domain,
            job.reset_domain,
            job.physical_instance_path,
            (
                None
                if trace_snapshot is None
                else VerificationCounterexampleMetadata(
                    trace_snapshot.sample_cycle,
                    trace_snapshot.reset_state,
                    trace_snapshot.comparison_valid_state,
                )
            ),
            job.clock_domain_contract,
            job.physical_domain_identity,
        )

    @classmethod
    def from_cover_result(
        cls, job: VerificationJob, result: CoverResult,
        *, work_directory: Path | None = None,
    ) -> "VerificationJobResult":
        return cls(
            job.property_id,
            job.kind,
            result.status.value,
            "cover",
            result.engine or "",
            result.solver or "",
            result.depth or 0,
            result.reason,
            None,
            result.witness,
            result.source_origin,
            result.tool_versions,
            None if work_directory is None else str(work_directory),
            job.route,
            job.backend,
            job.artifact_hash,
            job.binding_identity,
            job.selected_ir_identity,
            job.scope_id,
            job.assumption_ids,
            job.clock_domain,
            job.reset_domain,
            job.physical_instance_path,
            None,
            job.clock_domain_contract,
            job.physical_domain_identity,
        )

    def to_data(self) -> dict[str, object]:
        counterexample = None
        if self.counterexample is not None:
            counterexample = {
                "cycle": self.counterexample.cycle,
                "property_id": self.counterexample.property_id,
                "raw_trace": self.counterexample.raw_trace,
                "values": [list(item) for item in self.counterexample.values],
                "sample_cycle": (
                    None
                    if self.counterexample_metadata is None
                    else self.counterexample_metadata.sample_cycle
                ),
                "reset_state": (
                    None
                    if self.counterexample_metadata is None
                    else self.counterexample_metadata.reset_state
                ),
                "comparison_valid_state": (
                    None
                    if self.counterexample_metadata is None
                    else self.counterexample_metadata.comparison_valid_state
                ),
            }
        witness = None
        if self.witness is not None:
            witness = {
                "cycle": self.witness.cycle,
                "property_id": self.witness.property_id,
                "raw_trace": self.witness.raw_trace,
                "values": [list(item) for item in self.witness.values],
            }
        return {
            "counterexample": counterexample,
            "depth": self.depth,
            "engine": self.engine,
            "kind": self.kind,
            "mode": self.mode,
            "property_id": self.property_id,
            "reason": self.reason,
            "solver": self.solver,
            "source_origin": _origin_to_data(self.source_origin),
            "status": self.status,
            "tool_versions": [list(item) for item in self.tool_versions],
            "witness": witness,
            "work_directory": self.work_directory,
            "route": self.route,
            "backend": self.backend,
            "artifact_hash": self.artifact_hash,
            "binding_identity": self.binding_identity,
            "selected_ir_identity": self.selected_ir_identity,
            "scope_id": self.scope_id,
            "assumption_ids": list(self.assumption_ids),
            "clock_domain": self.clock_domain,
            "reset_domain": self.reset_domain,
            "physical_instance_path": list(self.physical_instance_path),
            "clock_domain_contract": clock_domain_data(
                self.clock_domain_contract
            ),
            "physical_domain_identity": self.physical_domain_identity,
        }

    @classmethod
    def from_data(cls, data: object) -> "VerificationJobResult":
        if not isinstance(data, Mapping):
            raise VerificationBundleError("verification job result must be an object")
        previous_keys = (
            "property_id", "kind", "status", "mode", "engine", "solver",
            "depth", "reason", "counterexample", "witness", "source_origin",
            "tool_versions", "work_directory", "route", "backend",
            "artifact_hash", "binding_identity", "selected_ir_identity",
            "scope_id", "assumption_ids", "clock_domain", "reset_domain",
            "physical_instance_path",
        )
        keys = (
            *previous_keys,
            "clock_domain_contract",
            "physical_domain_identity",
        )
        legacy = previous_keys[:13]
        actual = frozenset(data)
        if actual not in {
            frozenset(legacy), frozenset(previous_keys), frozenset(keys),
        }:
            _require_exact_keys(
                data, required=keys, description="verification job result"
            )

        def optional_string(name: str) -> str | None:
            value = data.get(name)
            if value is not None and not isinstance(value, str):
                raise VerificationBundleError(
                    f"verification result {name} must be a string"
                )
            return value

        def string_tuple(name: str) -> tuple[str, ...]:
            value = data.get(name, [])
            if not isinstance(value, list) or any(
                not isinstance(item, str) for item in value
            ):
                raise VerificationBundleError(
                    f"verification result {name} must be an array of strings"
                )
            return tuple(value)

        tool_values = data["tool_versions"]
        if not isinstance(tool_values, list):
            raise VerificationBundleError(
                "verification result tool_versions must be an array"
            )
        versions: list[tuple[str, str]] = []
        for value in tool_values:
            if (
                not isinstance(value, list)
                or len(value) != 2
                or any(not isinstance(item, str) for item in value)
            ):
                raise VerificationBundleError(
                    "verification result tool version must be a string pair"
                )
            versions.append((value[0], value[1]))

        def trace(
            value: object, *, witness: bool
        ) -> tuple[
            Counterexample | CoverWitness | None,
            VerificationCounterexampleMetadata | None,
        ]:
            if value is None:
                return None, None
            if not isinstance(value, Mapping):
                raise VerificationBundleError("verification trace must be an object")
            legacy_trace_keys = {"cycle", "property_id", "raw_trace", "values"}
            current_trace_keys = {
                *legacy_trace_keys,
                "sample_cycle",
                "reset_state",
                "comparison_valid_state",
            }
            if set(value) not in (legacy_trace_keys, current_trace_keys):
                _require_exact_keys(
                    value,
                    required=current_trace_keys,
                    description="verification trace",
                )
            cycle = value["cycle"]
            if cycle is not None:
                cycle = _require_integer(cycle, "verification trace cycle")
            raw_trace = value["raw_trace"]
            if raw_trace is not None and not isinstance(raw_trace, str):
                raise VerificationBundleError(
                    "verification raw trace must be a string"
                )
            raw_values = value["values"]
            if not isinstance(raw_values, list):
                raise VerificationBundleError(
                    "verification trace values must be an array"
                )
            values: list[tuple[str, str]] = []
            for item in raw_values:
                if (
                    not isinstance(item, list)
                    or len(item) != 2
                    or any(not isinstance(part, str) for part in item)
                ):
                    raise VerificationBundleError(
                        "verification trace value must be a string pair"
                    )
                values.append((item[0], item[1]))
            property_id = _validate_property_id(value["property_id"])
            if witness:
                return CoverWitness(property_id, cycle, tuple(values), raw_trace), None
            metadata = None
            if set(value) == current_trace_keys:
                sample_cycle = value["sample_cycle"]
                if sample_cycle is not None:
                    sample_cycle = _require_integer(
                        sample_cycle,
                        "verification counterexample sample cycle",
                        minimum=0,
                    )
                reset_state = value["reset_state"]
                comparison_valid_state = value["comparison_valid_state"]
                for state, description in (
                    (reset_state, "reset state"),
                    (comparison_valid_state, "comparison-valid state"),
                ):
                    if state is not None and not isinstance(state, str):
                        raise VerificationBundleError(
                            f"verification counterexample {description} must be a string"
                        )
                metadata = VerificationCounterexampleMetadata(
                    sample_cycle,
                    reset_state,
                    comparison_valid_state,
                )
                if (
                    metadata.sample_cycle is None
                    and metadata.reset_state is None
                    and metadata.comparison_valid_state is None
                ):
                    metadata = None
            return Counterexample(property_id, cycle, tuple(values), raw_trace), metadata

        counterexample, counterexample_metadata = trace(
            data["counterexample"], witness=False
        )
        witness, witness_metadata = trace(data["witness"], witness=True)
        if witness_metadata is not None:
            raise VerificationBundleError(
                "verification cover witness cannot carry counterexample metadata"
            )

        return cls(
            property_id=_validate_property_id(data["property_id"]),
            kind=_require_string(data["kind"], "verification result kind"),
            status=_require_string(data["status"], "verification result status"),
            mode=_require_string(data["mode"], "verification result mode"),
            engine=_require_string(data["engine"], "verification result engine"),
            solver=_require_string(data["solver"], "verification result solver"),
            depth=_require_integer(data["depth"], "verification result depth", minimum=1),
            reason=optional_string("reason"),
            counterexample=counterexample,  # type: ignore[arg-type]
            witness=witness,  # type: ignore[arg-type]
            source_origin=_origin_from_data(data["source_origin"]),
            tool_versions=tuple(versions),
            work_directory=optional_string("work_directory"),
            route=optional_string("route"),
            backend=optional_string("backend"),
            artifact_hash=optional_string("artifact_hash"),
            binding_identity=optional_string("binding_identity"),
            selected_ir_identity=optional_string("selected_ir_identity"),
            scope_id=optional_string("scope_id"),
            assumption_ids=string_tuple("assumption_ids"),
            clock_domain=optional_string("clock_domain"),
            reset_domain=optional_string("reset_domain"),
            physical_instance_path=string_tuple("physical_instance_path"),
            counterexample_metadata=counterexample_metadata,
            clock_domain_contract=_clock_domain_from_job_data(
                data.get("clock_domain_contract")
            ),
            physical_domain_identity=optional_string(
                "physical_domain_identity"
            ),
        )


@dataclass(frozen=True)
class VerificationRunReport:
    bundle_identity: str
    top: str
    config: VerificationRunConfig
    results: tuple[VerificationJobResult, ...]
    tool_versions: tuple[tuple[str, str], ...]
    bounded_results: tuple[VerificationJobResult, ...] = ()
    schema: str = field(default=VERIFICATION_RUN_REPORT_SCHEMA, init=False)
    schema_version: int = field(default=VERIFICATION_RUN_REPORT_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        _validate_identity(self.bundle_identity, "verification bundle identity")
        if _TOKEN.fullmatch(self.top) is None:
            raise VerificationBundleError(
                f"verification report top '{self.top}' is not a legal identifier"
            )
        if not self.results:
            raise VerificationBundleError("verification report requires at least one result")
        property_ids = tuple(item.property_id for item in self.results)
        if len(set(property_ids)) != len(property_ids):
            raise VerificationBundleError("verification report property IDs must be unique")
        for item in self.results:
            expected_mode = "cover" if item.kind == "cover" else self.config.mode.value
            if item.mode != expected_mode:
                raise VerificationBundleError(
                    f"verification result '{item.property_id}' mode does not match run config"
                )
            if item.engine != self.config.engine or item.solver != self.config.solver:
                raise VerificationBundleError(
                    f"verification result '{item.property_id}' tool does not match run config"
                )
            if item.depth != self.config.depth:
                raise VerificationBundleError(
                    f"verification result '{item.property_id}' depth does not match run config"
                )
        names: set[str] = set()
        for name, value in self.tool_versions:
            _require_string(name, "verification tool name")
            _require_string(value, "verification tool version")
            if name in names:
                raise VerificationBundleError(
                    f"duplicate verification tool version '{name}'"
                )
            names.add(name)
        for item in self.results:
            if item.tool_versions != self.tool_versions:
                raise VerificationBundleError(
                    f"verification result '{item.property_id}' tool versions do not "
                    "match the run report"
                )
        if self.bounded_results:
            if self.config.mode is not ProofMode.PROVE:
                raise VerificationBundleError(
                    "bounded prerequisite evidence is valid only for a prove run"
                )
            bounded_ids = tuple(item.property_id for item in self.bounded_results)
            if len(set(bounded_ids)) != len(bounded_ids):
                raise VerificationBundleError(
                    "bounded prerequisite property IDs must be unique"
                )
            if set(bounded_ids) != set(property_ids):
                raise VerificationBundleError(
                    "bounded prerequisite evidence must cover every verification job"
                )
            final_by_property = {
                item.property_id: item for item in self.results
            }
            for item in self.bounded_results:
                expected_mode = "cover" if item.kind == "cover" else ProofMode.BMC.value
                if item.mode != expected_mode:
                    raise VerificationBundleError(
                        f"bounded prerequisite '{item.property_id}' has invalid mode"
                    )
                if (
                    item.engine != self.config.engine
                    or item.solver != self.config.solver
                    or item.depth != self.config.depth
                    or item.tool_versions != self.tool_versions
                ):
                    raise VerificationBundleError(
                        f"bounded prerequisite '{item.property_id}' execution metadata differs"
                    )
                final = final_by_property[item.property_id]
                bound_context = (
                    item.kind,
                    item.route,
                    item.backend,
                    item.artifact_hash,
                    item.binding_identity,
                    item.selected_ir_identity,
                    item.scope_id,
                    item.assumption_ids,
                    item.clock_domain,
                    item.reset_domain,
                    item.clock_domain_contract,
                    item.physical_domain_identity,
                    item.physical_instance_path,
                    item.source_origin,
                )
                final_context = (
                    final.kind,
                    final.route,
                    final.backend,
                    final.artifact_hash,
                    final.binding_identity,
                    final.selected_ir_identity,
                    final.scope_id,
                    final.assumption_ids,
                    final.clock_domain,
                    final.reset_domain,
                    final.clock_domain_contract,
                    final.physical_domain_identity,
                    final.physical_instance_path,
                    final.source_origin,
                )
                if bound_context != final_context:
                    raise VerificationBundleError(
                        f"bounded prerequisite '{item.property_id}' verification "
                        "context differs from the final result"
                    )

    @property
    def run_identity(self) -> str:
        """Content identity for execution evidence, excluding log locations/text."""

        return "verification-run:" + stable_digest({
            "schema": self.schema,
            "schema_version": self.schema_version,
            "bundle_identity": self.bundle_identity,
            "config": self.config.to_data(),
            "tool_versions": self.tool_versions,
            "results": [
                {
                    "property_id": item.property_id,
                    "kind": item.kind,
                    "status": item.status,
                    "reason": item.reason,
                    "mode": item.mode,
                    "depth": item.depth,
                    "counterexample_cycle": (
                        None if item.counterexample is None
                        else item.counterexample.cycle
                    ),
                    "counterexample_sample_cycle": (
                        None
                        if item.counterexample_metadata is None
                        else item.counterexample_metadata.sample_cycle
                    ),
                    "counterexample_reset_state": (
                        None
                        if item.counterexample_metadata is None
                        else item.counterexample_metadata.reset_state
                    ),
                    "counterexample_comparison_valid_state": (
                        None
                        if item.counterexample_metadata is None
                        else item.counterexample_metadata.comparison_valid_state
                    ),
                    "counterexample_values": (
                        () if item.counterexample is None
                        else item.counterexample.values
                    ),
                    "witness_cycle": (
                        None if item.witness is None else item.witness.cycle
                    ),
                    "witness_values": (
                        () if item.witness is None else item.witness.values
                    ),
                    "route": item.route,
                    "backend": item.backend,
                    "artifact_hash": item.artifact_hash,
                    "binding_identity": item.binding_identity,
                    "selected_ir_identity": item.selected_ir_identity,
                    "scope_id": item.scope_id,
                    "assumption_ids": item.assumption_ids,
                    "clock_domain": item.clock_domain,
                    "reset_domain": item.reset_domain,
                    "clock_domain_contract": clock_domain_data(
                        item.clock_domain_contract
                    ),
                    "physical_domain_identity": item.physical_domain_identity,
                    "physical_instance_path": item.physical_instance_path,
                    "source_origin": _origin_to_data(item.source_origin),
                }
                for item in self.results
            ],
            "bounded_results": [
                {
                    "property_id": item.property_id,
                    "kind": item.kind,
                    "status": item.status,
                    "reason": item.reason,
                    "mode": item.mode,
                    "depth": item.depth,
                    "counterexample_cycle": (
                        None if item.counterexample is None
                        else item.counterexample.cycle
                    ),
                    "counterexample_sample_cycle": (
                        None
                        if item.counterexample_metadata is None
                        else item.counterexample_metadata.sample_cycle
                    ),
                    "counterexample_reset_state": (
                        None
                        if item.counterexample_metadata is None
                        else item.counterexample_metadata.reset_state
                    ),
                    "counterexample_comparison_valid_state": (
                        None
                        if item.counterexample_metadata is None
                        else item.counterexample_metadata.comparison_valid_state
                    ),
                    "witness_cycle": (
                        None if item.witness is None else item.witness.cycle
                    ),
                    "counterexample_values": (
                        () if item.counterexample is None
                        else item.counterexample.values
                    ),
                    "witness_values": (
                        () if item.witness is None else item.witness.values
                    ),
                    "route": item.route,
                    "backend": item.backend,
                    "artifact_hash": item.artifact_hash,
                    "binding_identity": item.binding_identity,
                    "selected_ir_identity": item.selected_ir_identity,
                    "scope_id": item.scope_id,
                    "assumption_ids": item.assumption_ids,
                    "clock_domain": item.clock_domain,
                    "reset_domain": item.reset_domain,
                    "clock_domain_contract": clock_domain_data(
                        item.clock_domain_contract
                    ),
                    "physical_domain_identity": item.physical_domain_identity,
                    "physical_instance_path": item.physical_instance_path,
                    "source_origin": _origin_to_data(item.source_origin),
                }
                for item in self.bounded_results
            ],
        })

    @property
    def outcome(self) -> str:
        statuses = {item.status for item in self.results}
        if FormalStatus.FAILED.value in statuses:
            return "failed"
        if statuses & {FormalStatus.UNKNOWN.value, FormalStatus.SKIPPED.value}:
            return "incomplete"
        return "passed"

    @property
    def exit_code(self) -> int:
        return {"passed": 0, "failed": 1, "incomplete": 2}[self.outcome]

    def to_data(self) -> dict[str, object]:
        return {
            "bundle_identity": self.bundle_identity,
            "config": self.config.to_data(),
            "outcome": self.outcome,
            "results": [item.to_data() for item in self.results],
            "bounded_results": [item.to_data() for item in self.bounded_results],
            "run_identity": self.run_identity,
            "schema": self.schema,
            "schema_version": self.schema_version,
            "tool_versions": [list(item) for item in self.tool_versions],
            "top": self.top,
        }

    def to_json(self) -> str:
        return stable_json(self.to_data(), indent=2) + "\n"

    @classmethod
    def from_data(cls, data: object) -> "VerificationRunReport":
        if not isinstance(data, Mapping):
            raise VerificationBundleError("verification run report must be an object")
        legacy_keys = (
            "bundle_identity", "config", "outcome", "results", "run_identity",
            "schema", "schema_version", "tool_versions", "top",
        )
        current_keys = (*legacy_keys, "bounded_results")
        actual = frozenset(data)
        if actual not in {frozenset(legacy_keys), frozenset(current_keys)}:
            _require_exact_keys(
                data, required=current_keys, description="verification run report"
            )
        schema = data["schema"]
        version = data["schema_version"]
        legacy = (schema, version) in {
            ("zlang-verification-run-report-v4", 4),
            ("zlang-verification-run-report-v5", 5),
            ("zlang-verification-run-report-v6", 6),
        }
        if not legacy and (
            schema != VERIFICATION_RUN_REPORT_SCHEMA
            or version != VERIFICATION_RUN_REPORT_SCHEMA_VERSION
        ):
            raise VerificationBundleError("unsupported verification run report schema")
        result_values = data["results"]
        bounded_values = data.get("bounded_results", [])
        tool_values = data["tool_versions"]
        if not isinstance(result_values, list) or not isinstance(bounded_values, list):
            raise VerificationBundleError("verification report results must be arrays")
        if not isinstance(tool_values, list):
            raise VerificationBundleError("verification report tool_versions must be an array")
        versions: list[tuple[str, str]] = []
        for item in tool_values:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or any(not isinstance(value, str) for value in item)
            ):
                raise VerificationBundleError(
                    "verification report tool version must be a string pair"
                )
            versions.append((item[0], item[1]))
        report = cls(
            bundle_identity=_validate_identity(
                data["bundle_identity"], "verification bundle identity"
            ),
            top=_require_string(data["top"], "verification report top"),
            config=VerificationRunConfig.from_data(data["config"]),
            results=tuple(VerificationJobResult.from_data(item) for item in result_values),
            tool_versions=tuple(versions),
            bounded_results=tuple(
                VerificationJobResult.from_data(item) for item in bounded_values
            ),
        )
        if data["outcome"] != report.outcome:
            raise VerificationBundleError("verification report outcome is inconsistent")
        if not legacy and data["run_identity"] != report.run_identity:
            raise VerificationBundleError("verification run identity is inconsistent")
        return report

    @classmethod
    def from_json(cls, text: str) -> "VerificationRunReport":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise VerificationBundleError(
                f"invalid verification report JSON at line {error.lineno} "
                f"column {error.colno}"
            ) from error
        return cls.from_data(data)

    def to_text(self) -> str:
        lines = [
            f"verification bundle {self.bundle_identity}",
            f"run {self.run_identity}",
            f"top {self.top}",
        ]
        for item in self.results:
            detail = (
                f" mode={item.mode} depth={item.depth} "
                f"engine={item.engine} solver={item.solver}"
            )
            if item.reason:
                detail += f" reason={item.reason}"
            if item.source_origin is not None:
                unit = item.source_origin.source_unit or "<unknown-source>"
                detail += f" at={unit}:{item.source_origin.span.render()}"
            trace = item.counterexample or item.witness
            if trace is not None:
                detail += f" cycle={trace.cycle}"
                if trace.values:
                    detail += " values=" + ",".join(
                        f"{name}={value}" for name, value in trace.values
                    )
            if item.counterexample_metadata is not None:
                metadata = item.counterexample_metadata
                if metadata.sample_cycle is not None:
                    detail += f" sample_cycle={metadata.sample_cycle}"
                if metadata.reset_state is not None:
                    detail += f" reset={metadata.reset_state}"
                if metadata.comparison_valid_state is not None:
                    detail += (
                        " comparison_valid="
                        f"{metadata.comparison_valid_state}"
                    )
            if item.work_directory is not None:
                detail += f" work={item.work_directory}"
            lines.append(
                f"{item.property_id} [{item.kind}] {item.status}{detail}"
            )
        if self.bounded_results:
            lines.append(
                f"bounded prerequisite: {len(self.bounded_results)} job(s) retained"
            )
        lines.append(f"summary {self.outcome}: {len(self.results)} job(s)")
        return "\n".join(lines) + "\n"


def _execution_inputs(
    loaded: LoadedVerificationBundle,
    job: VerificationJob,
) -> tuple[str, Mapping[str, bytes]]:
    """Return Verilog text and flat auxiliary files for one immutable job."""

    records = {item.logical_path: item for item in loaded.manifest.files}
    source_parts: list[str] = []
    auxiliary: dict[str, bytes] = {}
    for logical_path in job.source_files:
        record = records.get(logical_path)
        if record is None:
            raise VerificationBundleError(
                f"verification job '{job.property_id}' references an unknown source"
            )
        content = loaded.read_bytes(logical_path)
        if record.kind == "companion":
            name = PurePosixPath(logical_path).name
            previous = auxiliary.get(name)
            if previous is not None and previous != content:
                raise VerificationBundleError(
                    f"verification job '{job.property_id}' has conflicting "
                    f"auxiliary file '{name}'"
                )
            auxiliary[name] = content
            continue
        if record.kind not in {"implementation", "harness"}:
            raise VerificationBundleError(
                f"verification job '{job.property_id}' cannot execute bundle "
                f"file kind '{record.kind}' as Verilog"
            )
        try:
            source_parts.append(content.decode("utf-8"))
        except UnicodeDecodeError as error:
            raise VerificationBundleError(
                f"verification source for '{job.property_id}' is not UTF-8"
            ) from error
    return "\n".join(source_parts), auxiliary


def _execution_diagnostic_sources(
    loaded: LoadedVerificationBundle,
    job: VerificationJob,
) -> tuple[GeneratedDiagnosticContext, ...]:
    """Bind published source maps to exact implementation slices.

    Malformed compatibility sidecars are ignored for attribution only; their
    immutable file hashes remain validated by bundle loading and they never
    affect solver execution or result classification.
    """

    records = {item.logical_path: item for item in loaded.manifest.files}
    maps: list[GeneratedSourceMap] = []
    for path in job.source_map_files:
        try:
            maps.append(GeneratedSourceMap.from_json(loaded.read_bytes(path)))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    if not maps:
        return ()
    contexts: list[GeneratedDiagnosticContext] = []
    line_offset = 0
    for logical_path in job.source_files:
        record = records.get(logical_path)
        if record is None or record.kind == "companion":
            continue
        if record.kind not in {"implementation", "harness"}:
            continue
        try:
            text = loaded.read_bytes(logical_path).decode("utf-8")
        except UnicodeDecodeError:
            continue
        if record.kind == "implementation":
            digest = hashlib.sha256(text.encode()).hexdigest()
            for source_map in maps:
                if (
                    source_map.artifact_hash == digest
                    and (job.backend is None or source_map.backend == job.backend)
                    and (
                        job.selected_ir_identity is None
                        or source_map.selected_ir_identity
                        == job.selected_ir_identity
                    )
                ):
                    contexts.append(GeneratedDiagnosticContext(
                        source_map,
                        text,
                        line_offset,
                    ))
        # _execution_inputs joins each non-companion text with one newline.
        line_offset += text.count("\n") + 1
    return tuple(contexts)


def _trace_binding_records(
    loaded: LoadedVerificationBundle,
    *,
    route: str | None = None,
) -> tuple[TraceBinding, ...]:
    payload = loaded.verification_ir.get("payload", {})
    raw: object = ()
    if isinstance(payload, Mapping):
        binding_sets = payload.get("binding_sets")
        if isinstance(binding_sets, list):
            matches = tuple(
                item for item in binding_sets
                if isinstance(item, Mapping) and item.get("route") == route
            )
            if route is not None and len(matches) == 1:
                raw = matches[0].get("bindings", ())
            elif route is None and len(binding_sets) == 1:
                only = binding_sets[0]
                raw = only.get("bindings", ()) if isinstance(only, Mapping) else ()
        if raw == ():
            raw = payload.get("bindings", ())
    records: list[TraceBinding] = []
    if not isinstance(raw, list):
        return ()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        semantic_id = item.get("semantic_signal_id")
        rtl_name = item.get("rtl_name")
        width = item.get("width")
        if (
            isinstance(semantic_id, str)
            and isinstance(rtl_name, str)
            and isinstance(width, int)
            and not isinstance(width, bool)
        ):
            canonical_type = (
                item.get("canonical_type")
                if isinstance(item.get("canonical_type"), str)
                else None
            )
            signedness = (
                item.get("signedness")
                if isinstance(item.get("signedness"), str)
                else None
            )
            if semantic_id in {
                "clock", "reset", "trace:reset", "physical_reset",
            } and width == 1:
                canonical_type = "bit"
                signedness = "bit"
            records.append(TraceBinding(
                semantic_id,
                rtl_name,
                width,
                canonical_type=canonical_type,
                signedness=signedness,
            ))
    return tuple(sorted(records, key=lambda item: item.semantic_signal_id))


def _vcd_snapshot(
    path: Path,
    *,
    cycle: int | None,
    bindings: tuple[TraceBinding, ...],
) -> tuple[tuple[str, str], ...]:
    """Compatibility wrapper around the shared M35/M36 decoder."""

    return decode_vcd_trace(path, cycle=cycle, bindings=bindings).values


def _semantic_trace_values(
    work_directory: Path | None,
    *,
    cycle: int | None,
    bindings: tuple[TraceBinding, ...],
) -> tuple[tuple[str, str], ...]:
    if work_directory is None:
        return ()
    traces = tuple(sorted(work_directory.glob("**/trace*.vcd")))
    if not traces:
        return ()
    return _vcd_snapshot(traces[-1], cycle=cycle, bindings=bindings)


def _semantic_trace_snapshot(
    work_directory: Path | None,
    *,
    cycle: int | None,
    bindings: tuple[TraceBinding, ...],
) -> FormalTraceSnapshot:
    """Decode one same-cycle M35 frame through the common trace utility."""

    if work_directory is None:
        return FormalTraceSnapshot(cycle, cycle, None, None, ())
    traces = tuple(sorted(work_directory.glob("**/trace*.vcd")))
    if not traces:
        return FormalTraceSnapshot(cycle, cycle, None, None, ())
    return decode_vcd_trace(
        traces[-1],
        cycle=cycle,
        bindings=bindings,
        comparison_window=ComparisonWindow.same_cycle(),
    )


def _relevant_verification_tool_versions(
    config: VerificationRunConfig,
    versions: Iterable[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    """Return only tools which can affect the configured execution route."""

    required = (
        {"yosys", "sby", "yosys-smtbmc", config.solver}
        if config.engine == "sby"
        else {config.engine, config.solver}
    )
    selected: dict[str, str] = {}
    for name, version in versions:
        _require_string(name, "verification cache tool name")
        _require_string(version, "verification cache tool version")
        if name in selected:
            raise VerificationBundleError(
                f"duplicate verification cache tool version '{name}'"
            )
        if name in required:
            selected[name] = version
    return tuple(sorted(selected.items()))


def _verification_result_cache_identity(
    loaded: LoadedVerificationBundle,
    job: VerificationJob,
    *,
    config: VerificationRunConfig,
    tool_versions: Iterable[tuple[str, str]],
) -> dict[str, object]:
    job_data = job.to_data()
    route = {
        "artifact_hash": job.artifact_hash,
        "backend": job.backend,
        "binding_identity": job.binding_identity,
        "route": job.route,
        "selected_ir_identity": job.selected_ir_identity,
    }
    # ``jobs`` controls host scheduling only.  It cannot change one solver
    # invocation and therefore deliberately does not fragment result reuse.
    execution_config = {
        "depth": config.depth,
        "engine": config.engine,
        "mode": "cover" if job.kind == "cover" else config.mode.value,
        "solver": config.solver,
        "timeout_seconds": config.timeout_seconds,
    }
    return {
        "bundle_identity": (
            loaded.manifest.bundle_identity or loaded.manifest.computed_identity
        ),
        "config": execution_config,
        "job": job_data,
        "job_identity": stable_digest(job_data),
        "route": route,
        "run_report_schema": VERIFICATION_RUN_REPORT_SCHEMA,
        "run_report_schema_version": VERIFICATION_RUN_REPORT_SCHEMA_VERSION,
        "schema": VERIFICATION_RESULT_CACHE_SCHEMA,
        "schema_version": VERIFICATION_RESULT_CACHE_SCHEMA_VERSION,
        "tool_versions": [
            list(item)
            for item in _relevant_verification_tool_versions(config, tool_versions)
        ],
    }


def verification_result_cache_key(
    bundle: LoadedVerificationBundle | Path,
    job: VerificationJob,
    *,
    config: VerificationRunConfig,
    tool_versions: Iterable[tuple[str, str]],
) -> str:
    """Return the exact content key for one verification-job execution."""

    loaded = load_verification_bundle(bundle) if isinstance(bundle, Path) else bundle
    return stable_digest(_verification_result_cache_identity(
        loaded,
        job,
        config=config,
        tool_versions=tool_versions,
    ))


def _decisive_verification_result(result: VerificationJobResult) -> bool:
    if result.kind == "safety":
        return result.status in {
            FormalStatus.FAILED.value,
            FormalStatus.BOUNDED_PASS.value,
            FormalStatus.PROVEN.value,
        }
    if result.kind == "cover":
        return result.status in {
            CoverStatus.WITNESSED.value,
            CoverStatus.BOUNDED_UNREACHED.value,
        }
    return False


def _validate_cached_verification_result(
    result: VerificationJobResult,
    job: VerificationJob,
    *,
    config: VerificationRunConfig,
    relevant_tool_versions: tuple[tuple[str, str], ...],
) -> None:
    expected = {
        "property_id": job.property_id,
        "kind": job.kind,
        "mode": "cover" if job.kind == "cover" else config.mode.value,
        "engine": config.engine,
        "solver": config.solver,
        "depth": config.depth,
        "source_origin": job.source_origin,
        "route": job.route,
        "backend": job.backend,
        "artifact_hash": job.artifact_hash,
        "binding_identity": job.binding_identity,
        "selected_ir_identity": job.selected_ir_identity,
        "scope_id": job.scope_id,
        "assumption_ids": job.assumption_ids,
        "clock_domain": job.clock_domain,
        "reset_domain": job.reset_domain,
        "physical_instance_path": job.physical_instance_path,
    }
    for field_name, expected_value in expected.items():
        if getattr(result, field_name) != expected_value:
            raise VerificationBundleError(
                f"cached verification result {field_name.replace('_', ' ')} "
                "does not match the requested job"
            )
    if result.work_directory is not None:
        raise VerificationBundleError(
            "cached verification results must not retain a mutable work directory"
        )
    if result.tool_versions != relevant_tool_versions:
        raise VerificationBundleError(
            "cached verification result tool versions do not match the execution route"
        )
    if not _decisive_verification_result(result):
        raise VerificationBundleError(
            "cached verification result is not a decisive solver outcome"
        )


def _load_verification_result_cache(
    cache_directory: Path | None,
    loaded: LoadedVerificationBundle,
    job: VerificationJob,
    *,
    config: VerificationRunConfig,
    tool_versions: tuple[tuple[str, str], ...],
) -> tuple[VerificationJobResult | None, str | None, dict[str, object] | None]:
    if cache_directory is None:
        return None, None, None
    identity = _verification_result_cache_identity(
        loaded, job, config=config, tool_versions=tool_versions
    )
    key = stable_digest(identity)
    canonical = cache_directory / "M35" / "results" / f"{key}.json"
    legacy = cache_directory / f"{key}.json"
    payload, diagnostic = load_json_object(canonical)
    if payload is None and diagnostic is None:
        payload, diagnostic = load_json_object(legacy)
    if payload is None or diagnostic is not None:
        return None, key, identity
    try:
        _require_exact_keys(
            payload,
            required=(
                "identity", "identity_hash", "key", "result", "result_hash",
                "schema", "schema_version",
            ),
            description="verification result cache entry",
        )
        if (
            payload["schema"] != VERIFICATION_RESULT_CACHE_SCHEMA
            or payload["schema_version"] != VERIFICATION_RESULT_CACHE_SCHEMA_VERSION
        ):
            raise VerificationBundleError(
                "verification result cache schema does not match"
            )
        stored_key = payload["key"]
        identity_hash = payload["identity_hash"]
        result_hash = payload["result_hash"]
        for value, description in (
            (stored_key, "verification result cache key"),
            (identity_hash, "verification result cache identity hash"),
            (result_hash, "verification result cache result hash"),
        ):
            if not isinstance(value, str) or _HASH.fullmatch(value) is None:
                raise VerificationBundleError(
                    f"{description} must be lowercase SHA-256"
                )
        stored_identity = payload["identity"]
        result_data = payload["result"]
        if not isinstance(stored_identity, Mapping):
            raise VerificationBundleError(
                "verification result cache identity must be an object"
            )
        if not isinstance(result_data, Mapping):
            raise VerificationBundleError(
                "verification result cache payload must be an object"
            )
        if stored_identity != identity:
            raise VerificationBundleError(
                "verification result cache identity does not match the request"
            )
        if stable_digest(stored_identity) != identity_hash or identity_hash != key:
            raise VerificationBundleError(
                "verification result cache identity hash does not match"
            )
        if stored_key != key:
            raise VerificationBundleError(
                "verification result cache key does not match"
            )
        if stable_digest(result_data) != result_hash:
            raise VerificationBundleError(
                "verification result cache payload hash does not match"
            )
        result = VerificationJobResult.from_data(result_data)
        relevant_versions = _relevant_verification_tool_versions(
            config, tool_versions
        )
        _validate_cached_verification_result(
            result,
            job,
            config=config,
            relevant_tool_versions=relevant_versions,
        )
        # Reports describe the current discovery snapshot.  The entry itself
        # stores only route-relevant versions, so an unrelated installed tool
        # neither invalidates the cache nor leaks stale inventory into a report.
        return replace(result, tool_versions=tool_versions), key, identity
    except (KeyError, TypeError, VerificationBundleError):
        # A malformed, partial, stale, or tampered entry is never evidence.
        # Treat it as a miss so real execution can repair it atomically.
        return None, key, identity


def _publish_verification_result_cache(
    cache_directory: Path | None,
    key: str | None,
    identity: Mapping[str, object] | None,
    job: VerificationJob,
    result: VerificationJobResult,
    *,
    config: VerificationRunConfig,
    tool_versions: tuple[tuple[str, str], ...],
) -> None:
    if cache_directory is None or key is None or identity is None:
        return
    if not _decisive_verification_result(result):
        return
    relevant_versions = _relevant_verification_tool_versions(config, tool_versions)
    stored_result = replace(
        result,
        tool_versions=relevant_versions,
        work_directory=None,
    )
    _validate_cached_verification_result(
        stored_result,
        job,
        config=config,
        relevant_tool_versions=relevant_versions,
    )
    # Defend the internal caller boundary too: the path key must bind the exact
    # identity published inside the cache envelope.
    if identity.get("job") != job.to_data():
        raise VerificationBundleError(
            "verification result cache publication job does not match its identity"
        )
    if _HASH.fullmatch(key) is None or stable_digest(identity) != key:
        raise VerificationBundleError(
            "verification result cache publication key does not match its identity"
        )
    result_data = stored_result.to_data()
    publish_json_atomically(
        cache_directory / "M35" / "results" / f"{key}.json",
        {
            "identity": dict(identity),
            "identity_hash": key,
            "key": key,
            "result": result_data,
            "result_hash": stable_digest(result_data),
            "schema": VERIFICATION_RESULT_CACHE_SCHEMA,
            "schema_version": VERIFICATION_RESULT_CACHE_SCHEMA_VERSION,
        },
    )


@dataclass(frozen=True)
class _VerificationExecution:
    result: VerificationJobResult
    job: VerificationJob
    cache_key: str | None = None
    cache_identity: Mapping[str, object] | None = None
    cache_hit: bool = False


def _run_verification_bundle_unlocked(
    bundle: LoadedVerificationBundle | Path,
    *,
    config: VerificationRunConfig = VerificationRunConfig(),
    work_directory: Path | None = None,
    cache_directory: Path | None = None,
    job_kinds: frozenset[str] | None = None,
    toolchain: FormalToolchainContext | None = None,
) -> VerificationRunReport:
    """Replay executable safety and bounded-cover jobs."""

    loaded = (
        load_verification_bundle(bundle)
        if isinstance(bundle, Path)
        else bundle
    )
    bundle_root = loaded.directory.resolve(strict=False)
    execution_root: Path | None = None
    if work_directory is not None:
        candidate = Path(work_directory).resolve(strict=False)
        try:
            candidate.relative_to(bundle_root)
        except ValueError:
            pass
        else:
            raise VerificationBundleError(
                "verification work directory must be outside the immutable bundle"
            )
        bundle_token = stable_digest(
            loaded.manifest.bundle_identity or loaded.manifest.computed_identity
        )[:16]
        run_token = stable_digest(config.to_data())[:16]
        execution_root = candidate / (
            f"{bundle_token}-{config.mode.value}-{run_token}"
        )
        execution_root.mkdir(parents=True, exist_ok=True)
    cache_root: Path | None = None
    if cache_directory is not None:
        candidate = Path(cache_directory).resolve(strict=False)
        try:
            candidate.relative_to(bundle_root)
        except ValueError:
            pass
        else:
            raise VerificationBundleError(
                "verification cache directory must be outside the immutable bundle"
            )
        cache_root = candidate
    selected_jobs = tuple(
        (ordinal, job)
        for ordinal, job in enumerate(loaded.manifest.jobs)
        if job_kinds is None or job.kind in job_kinds
    )
    if not selected_jobs:
        raise VerificationBundleError("verification execution selected no jobs")

    # Structured skips are reports, not solver requests.  In particular, a
    # bundle whose assumptions or observations could not be connected must be
    # replayable without probing the host for Yosys/SBY/a solver.  Discover one
    # immutable snapshot only when at least one selected job can actually use
    # the supported executor.
    needs_toolchain = config.engine == "sby" and any(
        job.executable and job.kind in {"safety", "cover"}
        for _, job in selected_jobs
    )
    if needs_toolchain:
        toolchain = toolchain or FormalToolchainContext.discover(
            engine=config.engine, solver=config.solver
        )
        if toolchain.engine != config.engine or toolchain.solver != config.solver:
            raise VerificationBundleError(
                "verification toolchain context does not match the run configuration"
            )
        versions = toolchain.versions
    else:
        toolchain = None
        versions = ()

    def result_mode(job: VerificationJob) -> str:
        return "cover" if job.kind == "cover" else config.mode.value

    def validate_attribution(
        job: VerificationJob,
        *,
        mode: str,
        engine: str | None,
        solver: str | None,
        depth: int | None,
        source_origin: SourceOrigin | None,
    ) -> None:
        if mode != result_mode(job):
            raise VerificationBundleError(
                f"verification runner returned mode '{mode}' for job '{job.property_id}'"
            )
        if engine != config.engine or solver != config.solver or depth != config.depth:
            raise VerificationBundleError(
                f"verification runner returned mismatched execution metadata for "
                f"job '{job.property_id}'"
            )
        if source_origin != job.source_origin:
            raise VerificationBundleError(
                f"verification runner returned mismatched source origin for "
                f"job '{job.property_id}'"
            )

    def skipped(
        job: VerificationJob,
        reason: str,
    ) -> VerificationJobResult:
        return VerificationJobResult(
            property_id=job.property_id,
            kind=job.kind,
            status=FormalStatus.SKIPPED.value,
            mode=result_mode(job),
            engine=config.engine,
            solver=config.solver,
            depth=config.depth,
            reason=reason,
            source_origin=job.source_origin,
            tool_versions=versions,
            route=job.route,
            backend=job.backend,
            artifact_hash=job.artifact_hash,
            binding_identity=job.binding_identity,
            selected_ir_identity=job.selected_ir_identity,
            scope_id=job.scope_id,
            assumption_ids=job.assumption_ids,
            clock_domain=job.clock_domain,
            reset_domain=job.reset_domain,
            physical_instance_path=job.physical_instance_path,
            clock_domain_contract=job.clock_domain_contract,
            physical_domain_identity=job.physical_domain_identity,
        )

    def execute(entry: tuple[int, VerificationJob]) -> _VerificationExecution:
        ordinal, job = entry
        trace_bindings = _trace_binding_records(loaded, route=job.route)
        diagnostic_sources = _execution_diagnostic_sources(loaded, job)
        job_work_directory = (
            None
            if execution_root is None
            else execution_root
            / f"job-{ordinal:04d}-{stable_digest(job.property_id)[:16]}"
        )
        if job.kind not in {"safety", "cover"}:
            return _VerificationExecution(
                skipped(
                    job,
                    f"verification executor for job kind '{job.kind}' is unavailable",
                ),
                job,
            )
        if not job.executable:
            return _VerificationExecution(
                skipped(job, job.reason or "verification job is not executable"),
                job,
            )
        if config.engine != "sby":
            return _VerificationExecution(
                skipped(
                    job, f"verification engine '{config.engine}' is unsupported"
                ),
                job,
            )
        cached, cache_key, cache_identity = _load_verification_result_cache(
            cache_root,
            loaded,
            job,
            config=config,
            tool_versions=versions,
        )
        if cached is not None:
            return _VerificationExecution(
                cached, job, cache_key, cache_identity, True
            )
        source, auxiliary_files = _execution_inputs(loaded, job)
        if job.kind == "cover":
            cover_result = run_verilog_cover(
                source,
                top=job.top,
                property_id=job.property_id,
                depth=config.depth,
                solver=config.solver,
                engine=config.engine,
                source_origin=job.source_origin,
                systemverilog=job.systemverilog,
                timeout_seconds=config.timeout_seconds,
                work_directory=job_work_directory,
                auxiliary_files=auxiliary_files,
                toolchain=toolchain,
                diagnostic_sources=diagnostic_sources,
            )
            if cover_result.property_id != job.property_id:
                raise VerificationBundleError(
                    f"verification runner returned property '{cover_result.property_id}' "
                    f"for job '{job.property_id}'"
                )
            if (
                cover_result.witness is not None
                and cover_result.witness.property_id != job.property_id
            ):
                raise VerificationBundleError(
                    f"verification witness property does not match job '{job.property_id}'"
                )
            validate_attribution(
                job,
                mode="cover",
                engine=cover_result.engine,
                solver=cover_result.solver,
                depth=cover_result.depth,
                source_origin=cover_result.source_origin,
            )
            if cover_result.tool_versions != versions:
                raise VerificationBundleError(
                    f"verification job '{job.property_id}' changed tool inventory"
                )
            if cover_result.witness is not None:
                values = _semantic_trace_values(
                    job_work_directory,
                    cycle=cover_result.witness.cycle,
                    bindings=trace_bindings,
                )
                if values:
                    cover_result = replace(
                        cover_result,
                        witness=replace(cover_result.witness, values=values),
                    )
            return _VerificationExecution(
                VerificationJobResult.from_cover_result(
                    job, cover_result, work_directory=job_work_directory
                ),
                job,
                cache_key,
                cache_identity,
            )
        formal_result = run_verilog_formal(
            source,
            top=job.top,
            property_id=job.property_id,
            mode=config.mode,
            depth=config.depth,
            solver=config.solver,
            engine=config.engine,
            source_origin=job.source_origin,
            systemverilog=job.systemverilog,
            timeout_seconds=config.timeout_seconds,
            work_directory=job_work_directory,
            auxiliary_files=auxiliary_files,
            toolchain=toolchain,
            diagnostic_sources=diagnostic_sources,
        )
        if formal_result.property_id != job.property_id:
            raise VerificationBundleError(
                f"verification runner returned property '{formal_result.property_id}' "
                f"for job '{job.property_id}'"
            )
        if (
            formal_result.counterexample is not None
            and formal_result.counterexample.property_id != job.property_id
        ):
            raise VerificationBundleError(
                f"verification counterexample property does not match job '{job.property_id}'"
            )
        validate_attribution(
            job,
            mode=formal_result.mode.value,
            engine=formal_result.engine,
            solver=formal_result.solver,
            depth=formal_result.depth,
            source_origin=formal_result.source_origin,
        )
        if formal_result.tool_versions != versions:
            raise VerificationBundleError(
                f"verification job '{job.property_id}' changed tool inventory"
            )
        trace_snapshot = None
        if formal_result.counterexample is not None:
            trace_snapshot = _semantic_trace_snapshot(
                job_work_directory,
                cycle=formal_result.counterexample.cycle,
                bindings=trace_bindings,
            )
            formal_result = replace(
                formal_result,
                counterexample=replace(
                    formal_result.counterexample,
                    cycle=trace_snapshot.failure_cycle,
                    values=(
                        trace_snapshot.values
                        if trace_snapshot.values
                        else formal_result.counterexample.values
                    ),
                ),
            )
        return _VerificationExecution(
            VerificationJobResult.from_formal_result(
                job,
                formal_result,
                work_directory=job_work_directory,
                trace_snapshot=trace_snapshot,
            ),
            job,
            cache_key,
            cache_identity,
        )

    if config.jobs == 1 or len(selected_jobs) == 1:
        executions = [execute(item) for item in selected_jobs]
    else:
        with ThreadPoolExecutor(max_workers=min(config.jobs, len(selected_jobs))) as pool:
            # ``executor.map`` retains input order even though tools complete
            # independently, keeping reports and identities deterministic.
            executions = list(pool.map(execute, selected_jobs))
    results = [item.result for item in executions]
    payload = loaded.verification_ir.get("payload", {})
    dependencies = (
        payload.get("vacuity_dependencies", {})
        if isinstance(payload, Mapping) else {}
    )
    if isinstance(dependencies, Mapping) and job_kinds is None:
        by_id = {item.property_id: item for item in results}
        revised: list[VerificationJobResult] = []
        for item in results:
            feasibility_id = dependencies.get(item.property_id)
            feasibility = by_id.get(feasibility_id) if isinstance(feasibility_id, str) else None
            if feasibility_id is not None and (
                feasibility is None or feasibility.kind != "cover"
            ):
                raise VerificationBundleError(
                    f"verification result '{item.property_id}' has an invalid "
                    "vacuity dependency"
                )
            if (
                feasibility is not None
                and feasibility.status in {
                    CoverStatus.BOUNDED_UNREACHED.value,
                    CoverStatus.UNKNOWN.value,
                    CoverStatus.SKIPPED.value,
                }
                and item.status in {
                    FormalStatus.BOUNDED_PASS.value,
                    FormalStatus.PROVEN.value,
                }
            ):
                revised.append(replace(
                    item,
                    status=FormalStatus.UNKNOWN.value,
                    reason=(
                        f"verification scope is vacuous: feasibility cover "
                        f"'{feasibility.property_id}' has status "
                        f"'{feasibility.status}'"
                    ),
                ))
            else:
                revised.append(item)
        results = revised
    for execution, result in zip(executions, results, strict=True):
        if execution.cache_hit:
            continue
        _publish_verification_result_cache(
            cache_root,
            execution.cache_key,
            execution.cache_identity,
            execution.job,
            result,
            config=config,
            tool_versions=versions,
        )
    report = VerificationRunReport(
        loaded.manifest.bundle_identity or loaded.manifest.computed_identity,
        loaded.manifest.top,
        config,
        tuple(results),
        versions,
    )
    return report


def run_verification_bundle(
    bundle: LoadedVerificationBundle | Path,
    *,
    config: VerificationRunConfig = VerificationRunConfig(),
    work_directory: Path | None = None,
    cache_directory: Path | None = None,
    job_kinds: frozenset[str] | None = None,
    toolchain: FormalToolchainContext | None = None,
) -> VerificationRunReport:
    """Replay jobs under one exclusive deterministic execution-root lease."""

    loaded = load_verification_bundle(bundle) if isinstance(bundle, Path) else bundle
    lease: _ExecutionRootLease | None = None
    if work_directory is not None:
        bundle_root = loaded.directory.resolve(strict=False)
        candidate = Path(work_directory).resolve(strict=False)
        try:
            candidate.relative_to(bundle_root)
        except ValueError:
            pass
        else:
            raise VerificationBundleError(
                "verification work directory must be outside the immutable bundle"
            )
        bundle_token = stable_digest(
            loaded.manifest.bundle_identity or loaded.manifest.computed_identity
        )[:16]
        run_token = stable_digest(config.to_data())[:16]
        execution_root = candidate / (
            f"{bundle_token}-{config.mode.value}-{run_token}"
        )
        execution_root.mkdir(parents=True, exist_ok=True)
        lease = _ExecutionRootLease(execution_root)
    try:
        return _run_verification_bundle_unlocked(
            loaded,
            config=config,
            work_directory=work_directory,
            cache_directory=cache_directory,
            job_kinds=job_kinds,
            toolchain=toolchain,
        )
    finally:
        if lease is not None:
            lease.close()


def run_verification_bundle_staged(
    bundle: LoadedVerificationBundle | Path,
    *,
    config: VerificationRunConfig = VerificationRunConfig(),
    work_directory: Path | None = None,
    cache_directory: Path | None = None,
    tool_resolver: object | None = None,
) -> VerificationRunReport:
    """Run BMC before an optional unbounded proof attempt.

    A prove request never bypasses the bounded mutation-catching pass.  The
    returned evidence is the BMC report when that prerequisite is not clean,
    otherwise it is the proof report.  The two executions use distinct
    configuration-derived subdirectories below ``work_directory``.
    """

    loaded = load_verification_bundle(bundle) if isinstance(bundle, Path) else bundle
    needs_toolchain = config.engine == "sby" and any(
        job.executable and job.kind in {"safety", "cover"}
        for job in loaded.manifest.jobs
    )
    toolchain = None
    if needs_toolchain:
        resolve = getattr(tool_resolver, "formal_context", None)
        toolchain = (
            resolve(engine=config.engine, solver=config.solver)
            if callable(resolve)
            else FormalToolchainContext.discover(
                engine=config.engine, solver=config.solver
            )
        )
    if config.mode is ProofMode.BMC:
        return run_verification_bundle(
            loaded,
            config=config,
            work_directory=work_directory,
            cache_directory=cache_directory,
            toolchain=toolchain,
        )
    bounded = run_verification_bundle(
        loaded,
        config=replace(config, mode=ProofMode.BMC),
        work_directory=work_directory,
        cache_directory=cache_directory,
        toolchain=toolchain,
    )
    # Covers are advisory unless they are the explicit feasibility dependency
    # of a safety goal.  Vacuity processing above has already converted such
    # dependent safety results to UNKNOWN.  An unrelated unavailable cover
    # must therefore remain visible in the final report without preventing an
    # otherwise clean safety job from advancing to PROVE.
    bounded_safety = tuple(
        item for item in bounded.results if item.kind == "safety"
    )
    if any(
        item.status != FormalStatus.BOUNDED_PASS.value
        for item in bounded_safety
    ):
        return bounded
    if not bounded_safety:
        return bounded
    proof = run_verification_bundle(
        loaded,
        config=config,
        work_directory=work_directory,
        cache_directory=cache_directory,
        job_kinds=frozenset({"safety"}),
        toolchain=toolchain,
    )
    proof_by_id = {item.property_id: item for item in proof.results}
    merged = tuple(
        proof_by_id.get(item.property_id, item)
        for item in bounded.results
    )
    return VerificationRunReport(
        proof.bundle_identity,
        proof.top,
        config,
        merged,
        proof.tool_versions,
        bounded.results,
    )


__all__ = [
    "LoadedVerificationBundle",
    "VerificationBundleError",
    "VerificationBundleFile",
    "VerificationBundleInput",
    "VerificationBundleManifest",
    "VerificationCounterexampleMetadata",
    "VerificationJob",
    "VerificationJobResult",
    "VerificationRunConfig",
    "VerificationRunReport",
    "load_candidate_equivalence_replay",
    "load_verification_bundle",
    "publish_verification_bundle",
    "run_verification_bundle",
    "run_verification_bundle_staged",
    "verification_identity_for",
    "verification_result_cache_key",
]
