# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Immutable verification execution configuration and result records."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Mapping

from zlang.common import stable_digest, stable_pretty_json
from zlang import formal_trace as formal_trace
from zlang.ir import cdc as ir_cdc
from zlang.ir import formal as ir_formal
from zlang import source as source


from zlang import verification_bundle_codec as bundle_codec
from zlang import verification_codec_support as codec_support

@dataclass(frozen=True)
class VerificationRunConfig:
    mode: ir_formal.ProofMode = ir_formal.ProofMode.BMC
    engine: str = "sby"
    solver: str = "z3"
    depth: int = 20
    timeout_seconds: int = 120
    jobs: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ir_formal.ProofMode):
            raise codec_support.VerificationBundleError("verification mode must be ProofMode")
        for value, description in ((self.engine, "engine"), (self.solver, "solver")):
            if not isinstance(value, str) or not value or any(character.isspace() for character in value):
                raise codec_support.VerificationBundleError(
                    f"verification {description} must be one non-empty token"
                )
        codec_support.require_integer(self.depth, "verification depth", minimum=1)
        codec_support.require_integer(self.timeout_seconds, "verification timeout", minimum=1)
        codec_support.require_integer(self.jobs, "verification jobs", minimum=1)

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
            raise codec_support.VerificationBundleError("verification run config must be an object")
        codec_support.require_exact_keys(
            data,
            required={"depth", "engine", "jobs", "mode", "solver", "timeout_seconds"},
            description="verification run config",
        )
        try:
            mode = ir_formal.ProofMode(codec_support.require_string(data["mode"], "verification mode"))
        except ValueError as error:
            raise codec_support.VerificationBundleError("unsupported verification proof mode") from error
        return cls(
            mode=mode,
            engine=codec_support.require_string(data["engine"], "verification engine"),
            solver=codec_support.require_string(data["solver"], "verification solver"),
            depth=codec_support.require_integer(data["depth"], "verification depth", minimum=1),
            timeout_seconds=codec_support.require_integer(
                data["timeout_seconds"], "verification timeout", minimum=1
            ),
            jobs=codec_support.require_integer(data["jobs"], "verification jobs", minimum=1),
        )


@dataclass(frozen=True)
class VerificationCounterexampleMetadata:
    """Executor-owned interpretation of one safety verification counterexample frame."""

    sample_cycle: int | None = None
    reset_state: str | None = None
    comparison_valid_state: str | None = None

    def __post_init__(self) -> None:
        if self.sample_cycle is not None:
            codec_support.require_integer(
                self.sample_cycle,
                "verification counterexample sample cycle",
                minimum=0,
            )
        for value, description in (
            (self.reset_state, "reset state"),
            (self.comparison_valid_state, "comparison-valid state"),
        ):
            if value is not None:
                codec_support.require_string(value, f"verification counterexample {description}")


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
    counterexample: ir_formal.Counterexample | None = None
    witness: ir_formal.CoverWitness | None = None
    source_origin: source.SourceOrigin | None = None
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
    clock_domain_contract: ir_cdc.ClockDomain | None = None
    physical_domain_identity: str | None = None

    def __post_init__(self) -> None:
        codec_support.validate_property_id(self.property_id)
        if not isinstance(self.kind, str) or codec_support.KIND_PATTERN.fullmatch(self.kind) is None:
            raise codec_support.VerificationBundleError(
                "verification result kind must be a lowercase identifier"
            )
        codec_support.require_string(self.mode, "verification result mode")
        codec_support.require_string(self.engine, "verification result engine")
        codec_support.require_string(self.solver, "verification result solver")
        codec_support.require_integer(self.depth, "verification result depth", minimum=1)
        if self.reason is not None and not self.reason:
            raise codec_support.VerificationBundleError("verification result reason must not be empty")

        safety_statuses = {
            ir_formal.FormalStatus.FAILED.value,
            ir_formal.FormalStatus.BOUNDED_PASS.value,
            ir_formal.FormalStatus.PROVEN.value,
            ir_formal.FormalStatus.UNKNOWN.value,
            ir_formal.FormalStatus.SKIPPED.value,
        }
        cover_statuses = {
            ir_formal.CoverStatus.WITNESSED.value,
            ir_formal.CoverStatus.BOUNDED_UNREACHED.value,
            ir_formal.CoverStatus.UNKNOWN.value,
            ir_formal.CoverStatus.SKIPPED.value,
        }
        if self.kind == "safety":
            if self.status not in safety_statuses:
                raise codec_support.VerificationBundleError(
                    f"unsupported safety verification status '{self.status}'"
                )
            if self.mode not in {ir_formal.ProofMode.BMC.value, ir_formal.ProofMode.PROVE.value}:
                raise codec_support.VerificationBundleError(
                    f"safety result has invalid proof mode '{self.mode}'"
                )
            if self.status == ir_formal.FormalStatus.BOUNDED_PASS.value and self.mode != ir_formal.ProofMode.BMC.value:
                raise codec_support.VerificationBundleError("bounded_pass is only valid for BMC mode")
            if self.status == ir_formal.FormalStatus.PROVEN.value and self.mode != ir_formal.ProofMode.PROVE.value:
                raise codec_support.VerificationBundleError("proven is only valid for prove mode")
            if self.witness is not None:
                raise codec_support.VerificationBundleError("safety results cannot carry a cover witness")
            if (self.counterexample is not None) != (
                self.status == ir_formal.FormalStatus.FAILED.value
            ):
                raise codec_support.VerificationBundleError(
                    "failed safety results require exactly one counterexample"
                )
        elif self.kind == "cover":
            if self.status not in cover_statuses:
                raise codec_support.VerificationBundleError(
                    f"unsupported cover verification status '{self.status}'"
                )
            if self.mode != "cover":
                raise codec_support.VerificationBundleError("cover results require mode 'cover'")
            if self.counterexample is not None:
                raise codec_support.VerificationBundleError("cover results cannot carry a counterexample")
            if (self.witness is not None) != (
                self.status == ir_formal.CoverStatus.WITNESSED.value
            ):
                raise codec_support.VerificationBundleError(
                    "witnessed cover results require exactly one witness"
                )
        else:
            if self.status != ir_formal.FormalStatus.SKIPPED.value:
                raise codec_support.VerificationBundleError(
                    f"unsupported verification job kind '{self.kind}' must be skipped"
                )
            if self.mode not in {ir_formal.ProofMode.BMC.value, ir_formal.ProofMode.PROVE.value}:
                raise codec_support.VerificationBundleError(
                    f"unsupported verification result has invalid mode '{self.mode}'"
                )
            if self.counterexample is not None or self.witness is not None:
                raise codec_support.VerificationBundleError(
                    "unsupported verification results cannot carry trace metadata"
                )
        if self.counterexample is not None and self.counterexample.property_id != self.property_id:
            raise codec_support.VerificationBundleError(
                "verification counterexample property does not match its result"
            )
        if self.counterexample_metadata is not None:
            if self.counterexample is None:
                raise codec_support.VerificationBundleError(
                    "verification counterexample metadata requires a counterexample"
                )
            if not isinstance(
                self.counterexample_metadata, VerificationCounterexampleMetadata
            ):
                raise codec_support.VerificationBundleError(
                    "verification counterexample metadata has invalid type"
                )
        if self.witness is not None and self.witness.property_id != self.property_id:
            raise codec_support.VerificationBundleError(
                "verification witness property does not match its result"
            )
        names: set[str] = set()
        for name, value in self.tool_versions:
            codec_support.require_string(name, "verification job tool name")
            codec_support.require_string(value, "verification job tool version")
            if name in names:
                raise codec_support.VerificationBundleError(
                    f"duplicate verification job tool version '{name}'"
                )
            names.add(name)
        if self.work_directory is not None and not self.work_directory:
            raise codec_support.VerificationBundleError(
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
                codec_support.require_string(value, f"verification result {description}")
        for value, description in (
            (self.artifact_hash, "artifact hash"),
            (self.binding_identity, "binding identity"),
            (self.selected_ir_identity, "selected IR identity"),
        ):
            if value is not None:
                codec_support.validate_identity(value, f"verification result {description}")
        if len(set(self.assumption_ids)) != len(self.assumption_ids):
            raise codec_support.VerificationBundleError(
                "verification result assumption IDs must be unique"
            )
        for item in self.assumption_ids:
            codec_support.validate_property_id(item)
        if any(not item for item in self.physical_instance_path):
            raise codec_support.VerificationBundleError(
                "verification result physical instance path entries must not be empty"
            )
        if self.clock_domain_contract is not None:
            if not isinstance(self.clock_domain_contract, ir_cdc.ClockDomain):
                raise codec_support.VerificationBundleError(
                    "verification result clock-domain contract has invalid type"
                )
            try:
                self.clock_domain_contract.validate()
            except ValueError as error:
                raise codec_support.VerificationBundleError(
                    f"verification result clock-domain contract is invalid: {error}"
                ) from error
            if (
                self.clock_domain != self.clock_domain_contract.clock
                or self.reset_domain != self.clock_domain_contract.reset
            ):
                raise codec_support.VerificationBundleError(
                    "verification result logical and physical domains disagree"
                )
        if self.physical_domain_identity is not None:
            codec_support.validate_identity(
                self.physical_domain_identity,
                "verification result physical domain identity",
            )
            if self.clock_domain_contract is None:
                raise codec_support.VerificationBundleError(
                    "verification result physical domain identity requires its "
                    "exact contract"
                )
            if self.physical_domain_identity != ir_cdc.clock_domain_contract_identity(
                self.clock_domain_contract
            ):
                raise codec_support.VerificationBundleError(
                    "verification result physical domain identity does not match "
                    "its exact contract"
                )

    @classmethod
    def from_formal_result(
        cls, job: bundle_codec.VerificationJob, result: ir_formal.FormalResult,
        *, work_directory: Path | None = None,
        trace_snapshot: formal_trace.FormalTraceSnapshot | None = None,
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
        cls, job: bundle_codec.VerificationJob, result: ir_formal.CoverResult,
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
            "source_origin": source.source_origin_to_data(self.source_origin),
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
            "clock_domain_contract": ir_cdc.clock_domain_data(
                self.clock_domain_contract
            ),
            "physical_domain_identity": self.physical_domain_identity,
        }

    def identity_data(self) -> dict[str, object]:
        """Return execution evidence fields which define run identity."""

        return {
            "property_id": self.property_id,
            "kind": self.kind,
            "status": self.status,
            "reason": self.reason,
            "mode": self.mode,
            "depth": self.depth,
            "counterexample_cycle": (
                None if self.counterexample is None else self.counterexample.cycle
            ),
            "counterexample_sample_cycle": (
                None
                if self.counterexample_metadata is None
                else self.counterexample_metadata.sample_cycle
            ),
            "counterexample_reset_state": (
                None
                if self.counterexample_metadata is None
                else self.counterexample_metadata.reset_state
            ),
            "counterexample_comparison_valid_state": (
                None
                if self.counterexample_metadata is None
                else self.counterexample_metadata.comparison_valid_state
            ),
            "counterexample_values": (
                () if self.counterexample is None else self.counterexample.values
            ),
            "witness_cycle": (
                None if self.witness is None else self.witness.cycle
            ),
            "witness_values": (
                () if self.witness is None else self.witness.values
            ),
            "route": self.route,
            "backend": self.backend,
            "artifact_hash": self.artifact_hash,
            "binding_identity": self.binding_identity,
            "selected_ir_identity": self.selected_ir_identity,
            "scope_id": self.scope_id,
            "assumption_ids": self.assumption_ids,
            "clock_domain": self.clock_domain,
            "reset_domain": self.reset_domain,
            "clock_domain_contract": ir_cdc.clock_domain_data(
                self.clock_domain_contract
            ),
            "physical_domain_identity": self.physical_domain_identity,
            "physical_instance_path": self.physical_instance_path,
            "source_origin": source.source_origin_to_data(self.source_origin),
        }

    @classmethod
    def from_data(cls, data: object) -> "VerificationJobResult":
        if not isinstance(data, Mapping):
            raise codec_support.VerificationBundleError("verification job result must be an object")
        keys = (
            "property_id", "kind", "status", "mode", "engine", "solver",
            "depth", "reason", "counterexample", "witness", "source_origin",
            "tool_versions", "work_directory", "route", "backend",
            "artifact_hash", "binding_identity", "selected_ir_identity",
            "scope_id", "assumption_ids", "clock_domain", "reset_domain",
            "physical_instance_path",
            "clock_domain_contract",
            "physical_domain_identity",
        )
        codec_support.require_exact_keys(
            data, required=keys, description="verification job result"
        )

        tool_values = data["tool_versions"]
        if not isinstance(tool_values, list):
            raise codec_support.VerificationBundleError(
                "verification result tool_versions must be an array"
            )
        versions: list[tuple[str, str]] = []
        for value in tool_values:
            if (
                not isinstance(value, list)
                or len(value) != 2
                or any(not isinstance(item, str) for item in value)
            ):
                raise codec_support.VerificationBundleError(
                    "verification result tool version must be a string pair"
                )
            versions.append((value[0], value[1]))

        def trace(
            value: object, *, witness: bool
        ) -> tuple[
            ir_formal.Counterexample | ir_formal.CoverWitness | None,
            VerificationCounterexampleMetadata | None,
        ]:
            if value is None:
                return None, None
            if not isinstance(value, Mapping):
                raise codec_support.VerificationBundleError("verification trace must be an object")
            witness_keys = {"cycle", "property_id", "raw_trace", "values"}
            counterexample_keys = {
                *witness_keys,
                "sample_cycle",
                "reset_state",
                "comparison_valid_state",
            }
            codec_support.require_exact_keys(
                value,
                required=witness_keys if witness else counterexample_keys,
                description="verification trace",
            )
            cycle = value["cycle"]
            if cycle is not None:
                cycle = codec_support.require_integer(cycle, "verification trace cycle")
            raw_trace = value["raw_trace"]
            if raw_trace is not None and not isinstance(raw_trace, str):
                raise codec_support.VerificationBundleError(
                    "verification raw trace must be a string"
                )
            raw_values = value["values"]
            if not isinstance(raw_values, list):
                raise codec_support.VerificationBundleError(
                    "verification trace values must be an array"
                )
            values: list[tuple[str, str]] = []
            for item in raw_values:
                if (
                    not isinstance(item, list)
                    or len(item) != 2
                    or any(not isinstance(part, str) for part in item)
                ):
                    raise codec_support.VerificationBundleError(
                        "verification trace value must be a string pair"
                    )
                values.append((item[0], item[1]))
            property_id = codec_support.validate_property_id(value["property_id"])
            if witness:
                return ir_formal.CoverWitness(property_id, cycle, tuple(values), raw_trace), None
            sample_cycle = value["sample_cycle"]
            if sample_cycle is not None:
                sample_cycle = codec_support.require_integer(
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
                    raise codec_support.VerificationBundleError(
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
            return ir_formal.Counterexample(property_id, cycle, tuple(values), raw_trace), metadata

        counterexample, counterexample_metadata = trace(
            data["counterexample"], witness=False
        )
        witness, witness_metadata = trace(data["witness"], witness=True)
        if witness_metadata is not None:
            raise codec_support.VerificationBundleError(
                "verification cover witness cannot carry counterexample metadata"
            )

        return cls(
            property_id=codec_support.validate_property_id(data["property_id"]),
            kind=codec_support.require_string(data["kind"], "verification result kind"),
            status=codec_support.require_string(data["status"], "verification result status"),
            mode=codec_support.require_string(data["mode"], "verification result mode"),
            engine=codec_support.require_string(data["engine"], "verification result engine"),
            solver=codec_support.require_string(data["solver"], "verification result solver"),
            depth=codec_support.require_integer(data["depth"], "verification result depth", minimum=1),
            reason=codec_support.optional_string_field(data, "reason", "verification result"),
            counterexample=counterexample,  # type: ignore[arg-type]
            witness=witness,  # type: ignore[arg-type]
            source_origin=codec_support.origin_from_data(data["source_origin"]),
            tool_versions=tuple(versions),
            work_directory=codec_support.optional_string_field(
                data, "work_directory", "verification result"
            ),
            route=codec_support.optional_string_field(data, "route", "verification result"),
            backend=codec_support.optional_string_field(data, "backend", "verification result"),
            artifact_hash=codec_support.optional_string_field(
                data, "artifact_hash", "verification result"
            ),
            binding_identity=codec_support.optional_string_field(
                data, "binding_identity", "verification result"
            ),
            selected_ir_identity=codec_support.optional_string_field(
                data, "selected_ir_identity", "verification result"
            ),
            scope_id=codec_support.optional_string_field(data, "scope_id", "verification result"),
            assumption_ids=codec_support.string_tuple_field(
                data, "assumption_ids", "verification result"
            ),
            clock_domain=codec_support.optional_string_field(
                data, "clock_domain", "verification result"
            ),
            reset_domain=codec_support.optional_string_field(
                data, "reset_domain", "verification result"
            ),
            physical_instance_path=codec_support.string_tuple_field(
                data, "physical_instance_path", "verification result"
            ),
            counterexample_metadata=counterexample_metadata,
            clock_domain_contract=codec_support.clock_domain_from_job_data(
                data.get("clock_domain_contract")
            ),
            physical_domain_identity=codec_support.optional_string_field(
                data, "physical_domain_identity", "verification result"
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
    schema: str = field(default=bundle_codec.VERIFICATION_RUN_REPORT_SCHEMA, init=False)
    schema_version: int = field(default=bundle_codec.VERIFICATION_RUN_REPORT_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        codec_support.validate_identity(self.bundle_identity, "verification bundle identity")
        if codec_support.TOKEN_PATTERN.fullmatch(self.top) is None:
            raise codec_support.VerificationBundleError(
                f"verification report top '{self.top}' is not a legal identifier"
            )
        if not self.results:
            raise codec_support.VerificationBundleError("verification report requires at least one result")
        property_ids = tuple(item.property_id for item in self.results)
        if len(set(property_ids)) != len(property_ids):
            raise codec_support.VerificationBundleError("verification report property IDs must be unique")
        for item in self.results:
            expected_mode = "cover" if item.kind == "cover" else self.config.mode.value
            if item.mode != expected_mode:
                raise codec_support.VerificationBundleError(
                    f"verification result '{item.property_id}' mode does not match run config"
                )
            if item.engine != self.config.engine or item.solver != self.config.solver:
                raise codec_support.VerificationBundleError(
                    f"verification result '{item.property_id}' tool does not match run config"
                )
            if item.depth != self.config.depth:
                raise codec_support.VerificationBundleError(
                    f"verification result '{item.property_id}' depth does not match run config"
                )
        names: set[str] = set()
        for name, value in self.tool_versions:
            codec_support.require_string(name, "verification tool name")
            codec_support.require_string(value, "verification tool version")
            if name in names:
                raise codec_support.VerificationBundleError(
                    f"duplicate verification tool version '{name}'"
                )
            names.add(name)
        for item in self.results:
            if item.tool_versions != self.tool_versions:
                raise codec_support.VerificationBundleError(
                    f"verification result '{item.property_id}' tool versions do not "
                    "match the run report"
                )
        if self.bounded_results:
            if self.config.mode is not ir_formal.ProofMode.PROVE:
                raise codec_support.VerificationBundleError(
                    "bounded prerequisite evidence is valid only for a prove run"
                )
            bounded_ids = tuple(item.property_id for item in self.bounded_results)
            if len(set(bounded_ids)) != len(bounded_ids):
                raise codec_support.VerificationBundleError(
                    "bounded prerequisite property IDs must be unique"
                )
            if set(bounded_ids) != set(property_ids):
                raise codec_support.VerificationBundleError(
                    "bounded prerequisite evidence must cover every verification job"
                )
            final_by_property = {
                item.property_id: item for item in self.results
            }
            for item in self.bounded_results:
                expected_mode = "cover" if item.kind == "cover" else ir_formal.ProofMode.BMC.value
                if item.mode != expected_mode:
                    raise codec_support.VerificationBundleError(
                        f"bounded prerequisite '{item.property_id}' has invalid mode"
                    )
                if (
                    item.engine != self.config.engine
                    or item.solver != self.config.solver
                    or item.depth != self.config.depth
                    or item.tool_versions != self.tool_versions
                ):
                    raise codec_support.VerificationBundleError(
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
                    raise codec_support.VerificationBundleError(
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
            "results": [item.identity_data() for item in self.results],
            "bounded_results": [
                item.identity_data() for item in self.bounded_results
            ],
        })

    @property
    def outcome(self) -> str:
        statuses = {item.status for item in self.results}
        if ir_formal.FormalStatus.FAILED.value in statuses:
            return "failed"
        if statuses & {ir_formal.FormalStatus.UNKNOWN.value, ir_formal.FormalStatus.SKIPPED.value}:
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
        return stable_pretty_json(self.to_data())

    @classmethod
    def from_data(cls, data: object) -> "VerificationRunReport":
        if not isinstance(data, Mapping):
            raise codec_support.VerificationBundleError("verification run report must be an object")
        current_keys = (
            "bundle_identity", "config", "outcome", "results", "run_identity",
            "schema", "schema_version", "tool_versions", "top", "bounded_results",
        )
        codec_support.require_exact_keys(
            data, required=current_keys, description="verification run report"
        )
        schema = data["schema"]
        version = data["schema_version"]
        if (
            schema != bundle_codec.VERIFICATION_RUN_REPORT_SCHEMA
            or version != bundle_codec.VERIFICATION_RUN_REPORT_SCHEMA_VERSION
        ):
            raise codec_support.VerificationBundleError("unsupported verification run report schema")
        result_values = data["results"]
        bounded_values = data["bounded_results"]
        tool_values = data["tool_versions"]
        if not isinstance(result_values, list) or not isinstance(bounded_values, list):
            raise codec_support.VerificationBundleError("verification report results must be arrays")
        if not isinstance(tool_values, list):
            raise codec_support.VerificationBundleError("verification report tool_versions must be an array")
        versions: list[tuple[str, str]] = []
        for item in tool_values:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or any(not isinstance(value, str) for value in item)
            ):
                raise codec_support.VerificationBundleError(
                    "verification report tool version must be a string pair"
                )
            versions.append((item[0], item[1]))
        report = cls(
            bundle_identity=codec_support.validate_identity(
                data["bundle_identity"], "verification bundle identity"
            ),
            top=codec_support.require_string(data["top"], "verification report top"),
            config=VerificationRunConfig.from_data(data["config"]),
            results=tuple(VerificationJobResult.from_data(item) for item in result_values),
            tool_versions=tuple(versions),
            bounded_results=tuple(
                VerificationJobResult.from_data(item) for item in bounded_values
            ),
        )
        if data["outcome"] != report.outcome:
            raise codec_support.VerificationBundleError("verification report outcome is inconsistent")
        if data["run_identity"] != report.run_identity:
            raise codec_support.VerificationBundleError("verification run identity is inconsistent")
        return report

    @classmethod
    def from_json(cls, text: str) -> "VerificationRunReport":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise codec_support.VerificationBundleError(
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
