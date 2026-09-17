#!/usr/bin/env python3
"""Validate the public release-status manifest against the live source tree."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET



DEFAULT_STATUS = Path("release/status.json")
CORPUS_TEST = Path("tests/systemverilog/test_example_coverage.py")
COMMUNITY_PDF = Path("docs/ZLang-HDL-Language-Reference.pdf")
COMMUNITY_PDF_SOURCES = (
    Path("docs/language-reference.md"),
    Path("docs/language-quick-reference.md"),
)


class StatusError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise StatusError(f"cannot read {path}: {exc}") from exc


def _check_documentation(root: Path, documentation: object) -> None:
    if not isinstance(documentation, dict):
        raise StatusError("release status is missing documentation metadata")
    expected_keys = {"format", "pages", "pdf", "pdf_sha256", "sources"}
    if set(documentation) != expected_keys:
        raise StatusError("documentation metadata has unexpected or missing fields")
    if documentation.get("pdf") != COMMUNITY_PDF.as_posix():
        raise StatusError("documentation.pdf must name the reviewed Community PDF")
    pdf = root / COMMUNITY_PDF
    try:
        payload = pdf.read_bytes()
    except OSError as exc:
        raise StatusError(f"cannot read {pdf}: {exc}") from exc
    if not payload.startswith(b"%PDF-1.5") or not payload.rstrip().endswith(b"%%EOF"):
        raise StatusError("Community reference is not the reviewed PDF 1.5 envelope")
    if len(payload) > 20 * 1024 * 1024:
        raise StatusError("Community reference PDF exceeds the 20 MiB release bound")
    if documentation.get("pdf_sha256") != hashlib.sha256(payload).hexdigest():
        raise StatusError("Community reference PDF digest is stale")
    if documentation.get("format") != "PDF-1.5":
        raise StatusError("documentation format must be PDF-1.5")
    pages = documentation.get("pages")
    if not isinstance(pages, int) or pages <= 0:
        raise StatusError("documentation page count must be a positive integer")
    sources = documentation.get("sources")
    expected_sources = {path.as_posix() for path in COMMUNITY_PDF_SOURCES}
    if not isinstance(sources, dict) or set(sources) != expected_sources:
        raise StatusError("documentation sources must be the two public Markdown guides")
    for source in COMMUNITY_PDF_SOURCES:
        if sources[source.as_posix()] != _sha256(root / source):
            raise StatusError(f"documentation source digest is stale: {source}")


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StatusError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StatusError(f"{path} must contain a JSON object")
    return value


def _dict_assignment_size(path: Path, name: str) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        target = None
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if (
            isinstance(target, ast.Name)
            and target.id == name
            and isinstance(value, ast.Dict)
        ):
            return len(value.keys)
    raise StatusError(f"cannot find literal {name} registry in {path}")


def _example_counts(root: Path) -> dict[str, int]:
    try:
        from zlang.parser import parse
    except ImportError as exc:
        raise StatusError("ZLang must be importable to inspect the example corpus") from exc
    sources = tuple(sorted((root / "examples").rglob("*.zhl")))
    roots = 0
    for path in sources:
        syntax = parse(path.read_text(encoding="utf-8"))
        roots += len(syntax.submodules) + 1
    child = _dict_assignment_size(root / CORPUS_TEST, "CHILD_OR_TEMPLATE_ONLY")
    unsupported = _dict_assignment_size(root / CORPUS_TEST, "DIRECT_UNSUPPORTED")
    return {
        "source_files": len(sources),
        "module_roots": roots,
        "standalone_supported": roots - child - unsupported,
        "child_or_template_only": child,
        "direct_unsupported": unsupported,
    }


def _project_version(root: Path, pyproject: dict) -> str | None:
    project = pyproject.get("project", {})
    direct = project.get("version")
    if isinstance(direct, str):
        return direct
    dynamic = pyproject.get("tool", {}).get("setuptools", {}).get("dynamic", {})
    attribute = dynamic.get("version", {}).get("attr")
    if not isinstance(attribute, str) or "." not in attribute:
        return None
    module_name, symbol = attribute.rsplit(".", 1)
    module_path = root / f"{module_name.replace('.', '/')}.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == symbol for target in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    return None


def _junit_counts(path: Path) -> tuple[int, int, int, int]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise StatusError(f"cannot read JUnit report {path}: {exc}") from exc
    cases = root.findall(".//testcase")
    failures = sum(case.find("failure") is not None for case in cases)
    errors = sum(case.find("error") is not None for case in cases)
    skipped = sum(case.find("skipped") is not None for case in cases)
    passed = len(cases) - failures - errors - skipped
    return passed, skipped, failures, errors


def _command_output(command: tuple[str, ...]) -> str:
    executable = shutil.which(command[0])
    if executable is None:
        raise StatusError(f"required EDA tool is missing: {command[0]}")
    completed = subprocess.run(
        (executable, *command[1:]), capture_output=True, text=True, timeout=30
    )
    if completed.returncode != 0:
        raise StatusError(
            f"cannot query {command[0]}: {completed.stdout}{completed.stderr}"
        )
    return f"{completed.stdout}\n{completed.stderr}".strip()


def _check_tools(expected: dict[str, str]) -> None:
    commands = {
        "iverilog": ("iverilog", "-V"),
        "sby": ("sby", "--version"),
        "verilator": ("verilator", "--version"),
        "vvp": ("vvp", "-V"),
        "yosys": ("yosys", "-V"),
        "z3": ("z3", "--version"),
    }
    for name, command in commands.items():
        version = expected.get(name)
        if not isinstance(version, str) or not version:
            raise StatusError(f"eda_toolchain.{name} has no expected version")
        output = _command_output(command)
        if re.search(rf"(?<![0-9]){re.escape(version)}(?![0-9])", output) is None:
            raise StatusError(
                f"{name} version mismatch: expected {version!r}, got {output!r}"
            )
    if shutil.which("yosys-smtbmc") is None:
        raise StatusError("required EDA tool is missing: yosys-smtbmc")


def validate(
    root: Path,
    status_path: Path = DEFAULT_STATUS,
    *,
    junit: Path | None = None,
    check_tools: bool = False,
    tag: str | None = None,
) -> None:
    root = root.resolve()
    path = status_path if status_path.is_absolute() else root / status_path
    status = _load_json(path)
    if status.get("schema") != 2:
        raise StatusError("release status schema must be 2")
    release = status.get("release")
    validation = status.get("validation")
    platform = status.get("platform")
    tools = status.get("eda_toolchain")
    if not all(isinstance(item, dict) for item in (release, validation, platform, tools)):
        raise StatusError("release status is missing required object sections")
    _check_documentation(root, status.get("documentation"))
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = _project_version(root, pyproject)
    if release.get("version") != project_version:
        raise StatusError(
            f"release version {release.get('version')!r} does not match pyproject "
            f"version {project_version!r}"
        )
    if release.get("repository") != "https://github.com/postoroniy/zlang-hdl":
        raise StatusError("release repository must be postoroniy/zlang-hdl")
    if release.get("channel") != "alpha":
        raise StatusError("the first public release must remain alpha")
    if tag is not None and tag != f"v{project_version}":
        raise StatusError(
            f"release tag {tag!r} does not match package version v{project_version}"
        )
    if platform != {
        "operating_system": "Linux",
        "python": ">=3.12,<3.13",
        "architecture": "x86_64",
    }:
        raise StatusError("release platform contract has changed without review")
    actual_corpus = _example_counts(root)
    if validation.get("example_corpus") != actual_corpus:
        raise StatusError(
            "example corpus status is stale: "
            f"recorded={validation.get('example_corpus')!r}, actual={actual_corpus!r}"
        )
    minimum = validation.get("minimum_tests_passed")
    minimum_collected = validation.get("minimum_tests_collected")
    maximum_skipped = validation.get("maximum_tests_skipped")
    if not isinstance(minimum, int) or minimum <= 0:
        raise StatusError("minimum_tests_passed must be a positive integer")
    if not isinstance(minimum_collected, int) or minimum_collected < minimum:
        raise StatusError(
            "minimum_tests_collected must be an integer at least as large as "
            "minimum_tests_passed"
        )
    if not isinstance(maximum_skipped, int) or maximum_skipped < 0:
        raise StatusError("maximum_tests_skipped must be a non-negative integer")
    if junit is not None:
        passed, skipped, failures, errors = _junit_counts(junit)
        if failures or errors:
            raise StatusError(
                f"JUnit report contains {failures} failures and {errors} errors"
            )
        if skipped > maximum_skipped:
            raise StatusError(
                f"{skipped} tests skipped; release allows at most {maximum_skipped}"
            )
        if passed + skipped < minimum_collected:
            raise StatusError(
                f"only {passed + skipped} tests collected; release requires "
                f"{minimum_collected}"
            )
        if passed < minimum:
            raise StatusError(f"only {passed} tests passed; release requires {minimum}")
    if check_tools:
        _check_tools(tools)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check",))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--junit", type=Path)
    parser.add_argument("--check-tools", action="store_true")
    parser.add_argument("--tag")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        validate(
            args.root,
            args.status,
            junit=args.junit,
            check_tools=args.check_tools,
            tag=args.tag,
        )
    except StatusError as exc:
        print(f"release-status: error: {exc}", file=sys.stderr)
        return 1
    print("release status valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
