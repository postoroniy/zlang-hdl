"""Deterministic public report for one compiler-owned formal execution.

The existing :class:`VerificationRunReport` remains the M35/source-contract
execution product. This wrapper joins it to independently typed direct-SV M36
candidate reports without conflating their result/status models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Mapping

from zlang.common import stable_digest, stable_json
from zlang.formal_orchestration import (
    CandidateEquivalenceExecutionReport,
    CompilerFormalExecutionPlan,
    FormalOrchestrationError,
)
from zlang.verification_bundle import (
    VerificationBundleError,
    VerificationRunReport,
)


COMPILER_VERIFICATION_REPORT_SCHEMA = "zlang-compiler-verification-report-v2"
COMPILER_VERIFICATION_REPORT_SCHEMA_VERSION = 2


class CompilerVerificationReportError(ValueError):
    """A combined compiler verification report is malformed."""


@dataclass(frozen=True)
class CompilerVerificationReport:
    """M35 execution plus exact selected-candidate direct-SV M36 evidence."""

    verification: VerificationRunReport
    formal_execution_plan: CompilerFormalExecutionPlan
    candidate_equivalence: tuple[CandidateEquivalenceExecutionReport, ...]
    schema: str = field(default=COMPILER_VERIFICATION_REPORT_SCHEMA, init=False)
    schema_version: int = field(
        default=COMPILER_VERIFICATION_REPORT_SCHEMA_VERSION, init=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.verification, VerificationRunReport):
            raise CompilerVerificationReportError(
                "compiler verification requires a typed M35 run report"
            )
        if not isinstance(self.formal_execution_plan, CompilerFormalExecutionPlan):
            raise CompilerVerificationReportError(
                "compiler verification requires a typed formal execution plan"
            )
        reports = tuple(sorted(
            self.candidate_equivalence,
            key=lambda item: item.plan.site_identity,
        ))
        object.__setattr__(self, "candidate_equivalence", reports)
        if any(
            not isinstance(item, CandidateEquivalenceExecutionReport)
            for item in reports
        ):
            raise CompilerVerificationReportError(
                "candidate equivalence results must use typed execution reports"
            )
        expected = tuple(
            item.plan_identity
            for item in self.formal_execution_plan.candidate_equivalence_plans
        )
        actual = tuple(item.plan.plan_identity for item in reports)
        if actual != expected:
            detail = sorted(set(actual) ^ set(expected))
            suffix = "" if not detail else f" at '{detail[0]}'"
            raise CompilerVerificationReportError(
                "candidate equivalence reports differ from the compiler plan"
                + suffix
            )

    @property
    def outcome(self) -> str:
        if self.verification.outcome == "failed" or any(
            item.verification_failure for item in self.candidate_equivalence
        ):
            return "failed"
        if self.verification.outcome == "incomplete":
            return "incomplete"
        return "passed"

    @property
    def exit_code(self) -> int:
        return {"passed": 0, "failed": 1, "incomplete": 2}[self.outcome]

    @property
    def report_identity(self) -> str:
        return "compiler-verification-report:" + stable_digest({
            "schema": self.schema,
            "schema_version": self.schema_version,
            "verification_run": self.verification.run_identity,
            "formal_execution_plan": self.formal_execution_plan.plan_identity,
            "candidate_equivalence": [
                {
                    key: value
                    for key, value in item.to_data().items()
                    if key != "work_directories"
                }
                for item in self.candidate_equivalence
            ],
        })

    def to_data(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "report_identity": self.report_identity,
            "outcome": self.outcome,
            "verification": self.verification.to_data(),
            "formal_execution_plan": self.formal_execution_plan.to_data(),
            "candidate_equivalence": [
                item.to_data() for item in self.candidate_equivalence
            ],
        }

    def to_json(self) -> str:
        return stable_json(self.to_data(), indent=2) + "\n"

    @classmethod
    def from_data(cls, value: object) -> "CompilerVerificationReport":
        if not isinstance(value, Mapping) or any(
            not isinstance(key, str) for key in value
        ):
            raise CompilerVerificationReportError(
                "compiler verification report must be a JSON object"
            )
        expected = {
            "schema", "schema_version", "report_identity", "outcome",
            "verification", "formal_execution_plan", "candidate_equivalence",
        }
        if set(value) != expected:
            raise CompilerVerificationReportError(
                "compiler verification report fields differ from the current schema"
            )
        if (
            value["schema"] != COMPILER_VERIFICATION_REPORT_SCHEMA
            or value["schema_version"]
            != COMPILER_VERIFICATION_REPORT_SCHEMA_VERSION
        ):
            raise CompilerVerificationReportError(
                "unsupported compiler verification report schema"
            )
        reports = value["candidate_equivalence"]
        if not isinstance(reports, list):
            raise CompilerVerificationReportError(
                "candidate equivalence reports must be an array"
            )
        try:
            restored = cls(
                VerificationRunReport.from_data(value["verification"]),
                CompilerFormalExecutionPlan.from_data(
                    value["formal_execution_plan"]
                ),
                tuple(
                    CandidateEquivalenceExecutionReport.from_data(item)
                    for item in reports
                ),
            )
        except (
            VerificationBundleError,
            FormalOrchestrationError,
            ValueError,
        ) as error:
            raise CompilerVerificationReportError(str(error)) from error
        if value["outcome"] != restored.outcome:
            raise CompilerVerificationReportError(
                "compiler verification report outcome is inconsistent"
            )
        if value["report_identity"] != restored.report_identity:
            raise CompilerVerificationReportError(
                "compiler verification report identity is inconsistent"
            )
        return restored

    @classmethod
    def from_json(cls, text: str) -> "CompilerVerificationReport":
        try:
            value = json.loads(text)
        except (TypeError, json.JSONDecodeError) as error:
            raise CompilerVerificationReportError(
                "compiler verification report is not valid JSON"
            ) from error
        return cls.from_data(value)

    def to_text(self) -> str:
        lines = [self.verification.to_text().rstrip("\n")]
        applicability = (
            self.formal_execution_plan.verification_plan.applicability_summary()
        )
        lines.append(
            "formal applicability "
            f"executable={applicability['executable']} "
            f"skipped={applicability['skipped']} "
            f"total={applicability['total']}"
        )
        skip_reasons = applicability["skip_reasons"]
        assert isinstance(skip_reasons, dict)
        for reason, count in skip_reasons.items():
            lines.append(f"  skipped reason={reason} count={count}")
        for report in self.candidate_equivalence:
            lines.append(
                "candidate equivalence "
                f"site={report.plan.site_identity} "
                f"candidate={report.plan.candidate_identity} "
                f"status={report.direct_systemverilog_m36.status.value} "
                "backend=direct_systemverilog"
            )
            if report.tool_versions:
                lines.append(
                    "  tools "
                    + " ".join(
                        f"{name}={version}"
                        for name, version in report.tool_versions
                    )
                )
            for route, path in report.work_directories:
                lines.append(f"  work route={route} path={path}")
            for evidence in sorted(
                report.evidence_records, key=lambda item: item.evidence_id
            ):
                detail = (
                    f"  {evidence.claim} status={evidence.status}"
                    f" mode={evidence.mode} depth={evidence.depth}"
                )
                if evidence.backend is not None:
                    detail += f" backend={evidence.backend}"
                if evidence.property_id is not None:
                    detail += f" property={evidence.property_id}"
                lines.append(detail)
        lines.append(
            "compiler verification summary "
            f"{self.outcome}: {len(self.candidate_equivalence)} "
            "candidate site(s)"
        )
        return "\n".join(lines) + "\n"


__all__ = [
    "COMPILER_VERIFICATION_REPORT_SCHEMA",
    "COMPILER_VERIFICATION_REPORT_SCHEMA_VERSION",
    "CompilerVerificationReport",
    "CompilerVerificationReportError",
]
