# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Immutable verification bundle model and strict payload codec."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Iterable, Mapping

from zlang import verification_codec_support as codec_support
from zlang.common import stable_digest, stable_pretty_json
from zlang.ir import cdc as ir_cdc
from zlang.ir import formal_planning as formal_planning
from zlang import source as source


VERIFICATION_BUNDLE_SCHEMA = "zlang-verification-bundle-v5"
VERIFICATION_BUNDLE_SCHEMA_VERSION = 5
VERIFICATION_IR_SCHEMA = "zlang-verification-ir-snapshot-v4"
VERIFICATION_IR_SCHEMA_VERSION = 4
VERIFICATION_RUN_REPORT_SCHEMA = "zlang-verification-run-report-v9"
VERIFICATION_RUN_REPORT_SCHEMA_VERSION = 9
VERIFICATION_RESULT_CACHE_SCHEMA = "zlang-verification-result-cache-v4"
VERIFICATION_RESULT_CACHE_SCHEMA_VERSION = 4

_HASH = codec_support.HASH_PATTERN
_TOKEN = codec_support.TOKEN_PATTERN
_KIND = codec_support.KIND_PATTERN
_FILE_PREFIX = {
    "implementation": "implementation/",
    "companion": "implementation/companions/",
    "harness": "harness/",
    "config": "config/",
    "source_map": "source-map/",
    "verification_ir": "",
}

VerificationBundleError = codec_support.VerificationBundleError
_require_exact_keys = codec_support.require_exact_keys
_require_string = codec_support.require_string
_optional_string_field = codec_support.optional_string_field
_string_tuple_field = codec_support.string_tuple_field
_require_integer = codec_support.require_integer
_require_json_value = codec_support.require_json_value
_validate_identity = codec_support.validate_identity
_validate_property_id = codec_support.validate_property_id
_validate_relative_path = codec_support.validate_relative_path
_origin_from_data = codec_support.origin_from_data
_clock_domain_from_job_data = codec_support.clock_domain_from_job_data


def _validate_binding_record(
    value: object,
    *,
    description: str,
) -> str:
    """Validate one compiler-owned semantic-to-RTL binding record."""

    if not isinstance(value, Mapping):
        raise VerificationBundleError(f"{description} must be an object")
    _require_exact_keys(
        value,
        required=("semantic_signal_id", "rtl_module", "rtl_name", "width", "direction"),
        description=description,
    )
    semantic_id = _require_string(value["semantic_signal_id"], "semantic signal ID")
    _require_string(value["rtl_module"], "binding RTL module")
    _require_string(value["rtl_name"], "binding RTL name")
    _require_integer(value["width"], "binding width", minimum=1)
    direction = _require_string(value["direction"], "binding direction")
    if direction not in {"input", "output", "internal"}:
        raise VerificationBundleError(
            f"verification binding '{semantic_id}' has invalid direction '{direction}'"
        )
    return semantic_id


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
    required_fields = (
        "formal_ir_version",
        "identities",
        "hardware",
        "scopes",
        "properties",
        "vacuity_dependencies",
        "binding_sets",
        "execution_plan",
        "compiler_execution_plan",
        "candidate_equivalence_records",
    )
    if version != 4:
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
            "id", "kind", "generated_from", "predicate", "source_origin",
            "classification",
        )
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

    binding_sets_by_route: dict[str, Mapping[str, object]] = {}
    execution_plan: formal_planning.FormalExecutionPlan | None = None
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
                semantic_id = _validate_binding_record(
                    record,
                    description="verification binding-set record",
                )
                if semantic_id in local_ids:
                    raise VerificationBundleError(
                        f"duplicate verification binding '{semantic_id}' in route '{route}'"
                    )
                local_ids.add(semantic_id)
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
        execution_plan = formal_planning.FormalExecutionPlan.from_data(
            payload["execution_plan"]
        )
    except formal_planning.FormalPlanningError as error:
        raise VerificationBundleError(
            f"invalid formal execution plan: {error}"
        ) from error
    if execution_plan.compilation_identity != hardware["selected_ir_identity"]:
        raise VerificationBundleError(
            "formal execution plan selected-IR identity does not match hardware"
        )
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
    records_value = payload["candidate_equivalence_records"]
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
        _require_string(record["candidate_identity"], "candidate identity")
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
        _validate_identity(record["content_hash"], "candidate companion content hash")
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
        _validate_verification_jobs(
            jobs,
            property_kinds=property_kinds,
            execution_plan=execution_plan,
            scope_by_goal=scope_by_goal,
            scope_by_feasibility=scope_by_feasibility,
            global_requirements_by_domain=global_requirements_by_domain,
            all_requirement_ids=all_requirement_ids,
            binding_sets_by_route=binding_sets_by_route,
        )


def _validate_verification_jobs(
    jobs: tuple["VerificationJob", ...],
    *,
    property_kinds: dict[str, str],
    execution_plan: formal_planning.FormalExecutionPlan,
    scope_by_goal: dict[str, tuple[str, str, str, tuple[str, ...]]],
    scope_by_feasibility: dict[str, tuple[str, str, str, tuple[str, ...]]],
    global_requirements_by_domain: dict[tuple[str, str], tuple[str, ...]],
    all_requirement_ids: set[str],
    binding_sets_by_route: dict[str, Mapping[str, object]],
) -> None:
    """Validate current jobs against their exact scope, plan, and route."""

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
                f"verification job '{property_id}' scope does not match its declaration"
            )
        if (
            planned.clock_domain != expected_clock
            or planned.reset_domain != expected_reset
        ):
            raise VerificationBundleError(
                f"verification goal '{property_id}' domain does not match its declared scope"
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
            or planned.physical_domain_identity != job.physical_domain_identity
        ):
            raise VerificationBundleError(
                f"verification job '{property_id}' physical domain does not match its plan"
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
        for field_name, expected in (
            ("backend", artifact.backend),
            ("artifact_hash", artifact.artifact_identity),
            ("binding_identity", artifact.binding_identity),
        ):
            if (
                binding_set[field_name] != expected
                or getattr(job, field_name) != expected
            ):
                raise VerificationBundleError(
                    f"verification job '{property_id}' {field_name} does not match its route"
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
    source_origin: source.SourceOrigin | None = None
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
    clock_domain_contract: ir_cdc.ClockDomain | None = None
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
            if not isinstance(self.clock_domain_contract, ir_cdc.ClockDomain):
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
            if self.physical_domain_identity != ir_cdc.clock_domain_contract_identity(
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
            "source_origin": source.source_origin_to_data(self.source_origin),
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
            "clock_domain_contract": ir_cdc.clock_domain_data(
                self.clock_domain_contract
            ),
            "physical_domain_identity": self.physical_domain_identity,
        }
        result.update(provenance)
        return result

    @classmethod
    def from_data(cls, data: object) -> "VerificationJob":
        if not isinstance(data, Mapping):
            raise VerificationBundleError("verification job must be an object")
        required_keys = (
            "property_id", "kind", "top", "source_files", "config_files",
            "source_map_files", "systemverilog", "executable", "reason",
            "source_origin",
        )
        required_keys += (
            "route", "backend", "artifact_hash", "binding_identity",
            "selected_ir_identity", "scope_id", "assumption_ids",
            "clock_domain", "reset_domain", "physical_instance_path",
        )
        required_keys += (
            "clock_domain_contract", "physical_domain_identity",
        )
        _require_exact_keys(
            data,
            required=required_keys,
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
            _optional_string_field(data, "route", "verification job"),
            _optional_string_field(data, "backend", "verification job"),
            _optional_string_field(data, "artifact_hash", "verification job"),
            _optional_string_field(data, "binding_identity", "verification job"),
            _optional_string_field(data, "selected_ir_identity", "verification job"),
            _optional_string_field(data, "scope_id", "verification job"),
            _string_tuple_field(data, "assumption_ids", "verification job"),
            _optional_string_field(data, "clock_domain", "verification job"),
            _optional_string_field(data, "reset_domain", "verification job"),
            _string_tuple_field(data, "physical_instance_path", "verification job"),
            _clock_domain_from_job_data(data.get("clock_domain_contract")),
            _optional_string_field(
                data, "physical_domain_identity", "verification job"
            ),
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
        # than safety verification source/config inputs. Full bundle validation cross-links
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
        return stable_pretty_json(self.to_data())

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
