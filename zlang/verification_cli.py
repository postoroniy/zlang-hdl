"""Replay an immutable ZLang verification bundle."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

from zlang._version import __version__
from zlang.common.atomic_io import publish_text_atomically
from zlang.ir.formal import FormalError, ProofMode
from zlang.formal_routes import public_formal_route_identities
from zlang.candidate_equivalence import execute_frozen_candidate_equivalence
from zlang import compiler_verification_report as compiler_verification_report
from zlang.formal_exploration import FormalExplorationConfig
from zlang import formal_orchestration as formal_orchestration
from zlang import verification_bundle as verification_bundle


def _write_atomically(path: Path, content: str) -> None:
    publish_text_atomically(path, content)


def _resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise verification_bundle.VerificationBundleError(
            f"cannot resolve verification path '{path}': {error}"
        ) from error


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _preflight_arguments(
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
) -> None:
    if arguments.depth < 1:
        parser.error("--depth must be positive")
    if arguments.timeout_seconds < 1:
        parser.error("--timeout must be positive")
    if arguments.jobs < 1:
        parser.error("--jobs must be positive")

    bundle = _resolved(arguments.bundle)
    if arguments.report is not None:
        report = _resolved(arguments.report)
        if report == bundle or report.is_relative_to(bundle):
            parser.error("--report must be outside the immutable bundle")
    owned = tuple(
        (option, _resolved(path))
        for option, path in (
            ("--work-dir", arguments.work_dir),
            ("--cache", arguments.cache),
        )
        if path is not None
    )
    for option, path in owned:
        if _paths_overlap(path, bundle):
            parser.error(f"{option} must not overlap the immutable bundle")
    if len(owned) == 2 and _paths_overlap(owned[0][1], owned[1][1]):
        parser.error("--work-dir and --cache must not overlap")


def main(
    argv: Sequence[str] | None = None,
    *,
    prog: str = "zlang verify",
) -> int:
    parser = argparse.ArgumentParser(
        prog=prog,
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
    parser.add_argument(
        "--route", choices=public_formal_route_identities(), default="smtbmc"
    )
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
    _preflight_arguments(parser, arguments)
    try:
        bundle = verification_bundle.load_verification_bundle(arguments.bundle)
        config = verification_bundle.VerificationRunConfig(
            mode=ProofMode(arguments.mode),
            engine=arguments.engine,
            route=arguments.route,
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
        report = verification_bundle.run_verification_bundle_staged(bundle, **run_keywords)
        frozen_sites = verification_bundle.load_candidate_equivalence_replay(bundle)
        if frozen_sites:
            payload = bundle.verification_ir.get("payload")
            if not isinstance(payload, dict):
                raise verification_bundle.VerificationBundleError(
                    "verification bundle has no compiler formal plan payload"
                )
            compiler_plan = formal_orchestration.CompilerFormalExecutionPlan.from_data(
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
            report = compiler_verification_report.CompilerVerificationReport(
                report, compiler_plan, candidate_reports
            )
        rendered = report.to_json() if arguments.report_format == "json" else report.to_text()
        if arguments.report is not None:
            _write_atomically(arguments.report, rendered)
    except (
        compiler_verification_report.CompilerVerificationReportError,
        FormalError,
        formal_orchestration.FormalOrchestrationError,
        verification_bundle.VerificationBundleError,
        OSError,
    ) as error:
        print(f"{prog}: error: {error}", file=sys.stderr)
        return 2
    print(rendered, end="")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
