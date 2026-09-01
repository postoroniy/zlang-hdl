"""Replay an immutable ZLang verification bundle."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile
from typing import Sequence

from zlang._version import __version__
from zlang.ir.formal import FormalError, ProofMode
from zlang.candidate_equivalence import execute_frozen_candidate_equivalence
from zlang.compiler_verification_report import (
    CompilerVerificationReport,
    CompilerVerificationReportError,
)
from zlang.formal_exploration import FormalExplorationConfig
from zlang.formal_orchestration import (
    CompilerFormalExecutionPlan,
    FormalOrchestrationError,
)
from zlang.verification_bundle import (
    VerificationBundleError,
    VerificationRunConfig,
    load_candidate_equivalence_replay,
    load_verification_bundle,
    run_verification_bundle_staged,
)


def _write_atomically(path: Path, content: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zlang-verify",
        description="Validate and replay an immutable ZLang verification bundle",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument("bundle", type=Path, help="verification bundle directory")
    parser.add_argument("--mode", choices=("bmc", "prove"), default="bmc")
    parser.add_argument("--engine", default="sby")
    parser.add_argument("--solver", default="z3")
    parser.add_argument("--depth", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=120, dest="timeout_seconds")
    parser.add_argument(
        "--jobs", type=int, default=1,
        help="maximum independent verification jobs to run concurrently",
    )
    parser.add_argument(
        "--format", choices=("text", "json"), default="text", dest="report_format"
    )
    parser.add_argument("--report", type=Path, help="also write the run report")
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="retain solver configurations, logs, and traces outside the bundle",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        help="reuse and atomically publish decisive verification job results",
    )
    arguments = parser.parse_args(argv)
    try:
        bundle = load_verification_bundle(arguments.bundle)
        config = VerificationRunConfig(
            mode=ProofMode(arguments.mode),
            engine=arguments.engine,
            solver=arguments.solver,
            depth=arguments.depth,
            timeout_seconds=arguments.timeout_seconds,
            jobs=arguments.jobs,
        )
        work_directory = arguments.work_dir or arguments.bundle.with_name(
            arguments.bundle.name + ".work"
        )
        run_keywords: dict[str, object] = {
            "config": config,
            "work_directory": work_directory,
        }
        if arguments.cache is not None:
            run_keywords["cache_directory"] = arguments.cache
        report = run_verification_bundle_staged(bundle, **run_keywords)
        frozen_sites = load_candidate_equivalence_replay(bundle)
        if frozen_sites:
            payload = bundle.verification_ir.get("payload")
            if not isinstance(payload, dict):
                raise VerificationBundleError(
                    "verification bundle has no compiler formal plan payload"
                )
            compiler_plan = CompilerFormalExecutionPlan.from_data(
                payload.get("compiler_execution_plan")
            )
            candidate_config = FormalExplorationConfig(
                policy=compiler_plan.formal_policy,
                bmc_depth=arguments.depth,
                timeout_seconds=arguments.timeout_seconds,
                engine=arguments.engine,
                solver=arguments.solver,
                cache_directory=arguments.cache,
                work_directory=work_directory,
            )
            candidate_reports = execute_frozen_candidate_equivalence(
                compiler_plan,
                tuple(frozen_sites),
                candidate_config,
                jobs=arguments.jobs,
            )
            report = CompilerVerificationReport(
                report, compiler_plan, candidate_reports
            )
        rendered = report.to_json() if arguments.report_format == "json" else report.to_text()
        if arguments.report is not None:
            try:
                report_path = arguments.report.resolve()
                bundle_path = arguments.bundle.resolve()
                report_path.relative_to(bundle_path)
            except ValueError:
                pass
            else:
                raise VerificationBundleError(
                    "run reports must be written outside the immutable bundle"
                )
            _write_atomically(arguments.report, rendered)
    except (
        CompilerVerificationReportError,
        FormalError,
        FormalOrchestrationError,
        VerificationBundleError,
        OSError,
    ) as error:
        print(f"zlang-verify: error: {error}", file=sys.stderr)
        return 2
    print(rendered, end="")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
