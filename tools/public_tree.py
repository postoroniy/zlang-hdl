#!/usr/bin/env python3
"""Create and validate the compact, history-free public source projection."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tomllib
from urllib.parse import unquote, urlsplit


DEFAULT_CONFIG = Path("release/public-tree.toml")
MANIFEST_SCHEMA = 1
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
ZLANG_IMPORT = re.compile(
    r"(?m)^\s*import\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
)
ACTION_USE = re.compile(
    r"(?m)^\s*-?\s*uses:\s*([^\s#]+)\s*(?:#.*)?$"
)
IMMUTABLE_ACTION = re.compile(
    r"^[^/@\s]+/[^/@\s]+(?:/[^/@\s]+)*@[0-9a-f]{40}$"
)
LOCAL_PYTHON_ROOTS = frozenset({"tests", "tools", "zlang"})


class ProjectionError(RuntimeError):
    """A public snapshot is incomplete, unsafe, or non-deterministic."""


@dataclass(frozen=True)
class ProjectionConfig:
    path: Path
    manifest: str
    repository: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    required: tuple[str, ...]
    closure_roots: tuple[str, ...]
    scan_exempt: tuple[str, ...]
    forbidden_substrings: tuple[str, ...]
    forbidden_regex: tuple[re.Pattern[str], ...]
    text_extensions: frozenset[str]


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _load_config(source: Path, config_path: Path) -> ProjectionConfig:
    path = config_path if config_path.is_absolute() else source / config_path
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProjectionError(f"cannot load projection config {path}: {exc}") from exc
    if raw.get("schema") != 1:
        raise ProjectionError("public-tree.toml schema must be 1")
    projection = raw.get("projection")
    content = raw.get("content")
    if not isinstance(projection, dict) or not isinstance(content, dict):
        raise ProjectionError("public-tree.toml requires [projection] and [content]")

    def strings(
        table: dict[str, object],
        key: str,
        *,
        allow_empty_items: bool = False,
        optional: bool = False,
    ) -> tuple[str, ...]:
        value = table.get(key)
        if value is None and optional:
            return ()
        if not isinstance(value, list) or not all(
            isinstance(item, str) and (allow_empty_items or bool(item))
            for item in value
        ):
            raise ProjectionError(f"{key} must be a non-empty string array")
        return tuple(value)

    regexes: list[re.Pattern[str]] = []
    for expression in strings(content, "forbidden_regex"):
        try:
            regexes.append(re.compile(expression))
        except re.error as exc:
            raise ProjectionError(f"invalid forbidden regex {expression!r}: {exc}") from exc
    manifest = projection.get("manifest")
    repository = projection.get("repository")
    if not isinstance(manifest, str) or not manifest:
        raise ProjectionError("projection.manifest must be a path")
    manifest_path = PurePosixPath(manifest)
    if (
        manifest_path.is_absolute()
        or manifest_path.as_posix() != manifest
        or any(part in ("", ".", "..") for part in manifest_path.parts)
        or "\\" in manifest
    ):
        raise ProjectionError(
            "projection.manifest must be a normalized repository-relative path"
        )
    if not isinstance(repository, str) or not repository.startswith("https://github.com/"):
        raise ProjectionError("projection.repository must be a GitHub HTTPS URL")
    return ProjectionConfig(
        path=path,
        manifest=manifest,
        repository=repository,
        include=strings(projection, "include"),
        exclude=strings(projection, "exclude"),
        required=strings(projection, "required"),
        closure_roots=strings(projection, "closure_roots"),
        scan_exempt=strings(content, "scan_exempt", optional=True),
        forbidden_substrings=strings(content, "forbidden_substrings"),
        forbidden_regex=tuple(regexes),
        text_extensions=frozenset(
            strings(content, "text_extensions", allow_empty_items=True)
        ),
    )


def _directory_is_excluded(relative: str, patterns: tuple[str, ...]) -> bool:
    return _matches(relative, patterns) or _matches(f"{relative}/placeholder", patterns)


def _source_files(
    source: Path, manifest: str, exclude: tuple[str, ...] = ()
) -> tuple[Path, ...]:
    files: list[Path] = []
    for directory, names, basenames in os.walk(source, followlinks=False):
        current = Path(directory)
        kept_names = []
        for name in sorted(name for name in names if name != ".git"):
            candidate = current / name
            relative = _relative(candidate, source)
            if _directory_is_excluded(relative, exclude):
                continue
            if candidate.is_symlink():
                raise ProjectionError(
                    f"symlink directory is forbidden: {relative}"
                )
            kept_names.append(name)
        names[:] = kept_names
        for basename in sorted(basenames):
            candidate = current / basename
            relative = _relative(candidate, source)
            if relative == manifest:
                continue
            if candidate.is_symlink():
                raise ProjectionError(f"symlink is forbidden: {relative}")
            if candidate.is_file():
                files.append(candidate)
    return tuple(files)


def selected_files(source: Path, config: ProjectionConfig) -> tuple[Path, ...]:
    selected = []
    for path in _source_files(source, config.manifest, config.exclude):
        relative = _relative(path, source)
        if _matches(relative, config.include) and not _matches(relative, config.exclude):
            selected.append(path)
    return tuple(sorted(selected, key=lambda item: _relative(item, source)))


def _decode_text(path: Path, relative: str, config: ProjectionConfig) -> str:
    if path.suffix.lower() not in config.text_extensions:
        raise ProjectionError(
            f"unreviewed binary/file format in public tree: {relative} ({path.suffix!r})"
        )
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectionError(f"public file is not UTF-8 text: {relative}") from exc


def _check_required(selected: set[str], source: Path, config: ProjectionConfig) -> None:
    errors = []
    for required in config.required:
        if not (source / required).is_file():
            errors.append(f"required public file is absent: {required}")
        elif required not in selected:
            errors.append(f"required public file is excluded: {required}")
    if errors:
        raise ProjectionError("\n".join(errors))


def _check_closure_roots(
    all_files: tuple[Path, ...], selected: set[str], source: Path, config: ProjectionConfig
) -> None:
    errors = []
    for path in all_files:
        relative = _relative(path, source)
        in_closure = any(
            relative == root or relative.startswith(f"{root}/")
            for root in config.closure_roots
        )
        if (
            in_closure
            and relative not in selected
            and not _matches(relative, config.exclude)
        ):
            errors.append(f"file omitted from closure root: {relative}")
    if errors:
        raise ProjectionError("\n".join(errors))


def _check_content(
    text_by_path: dict[str, str], config: ProjectionConfig
) -> None:
    errors: list[str] = []
    for relative, text in text_by_path.items():
        if relative in config.scan_exempt:
            continue
        for forbidden in config.forbidden_substrings:
            if forbidden in text:
                errors.append(f"forbidden content {forbidden!r}: {relative}")
        for expression in config.forbidden_regex:
            if expression.search(text):
                errors.append(
                    f"secret/private-content pattern {expression.pattern!r}: {relative}"
                )
    if errors:
        raise ProjectionError("\n".join(errors))


def _local_link_target(raw: str) -> str | None:
    raw = raw.strip()
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1]
    # Drop an optional Markdown title after a whitespace-separated destination.
    raw = raw.split(maxsplit=1)[0] if raw else raw
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    return unquote(parsed.path)


def _check_markdown_links(
    text_by_path: dict[str, str], selected: set[str], source: Path
) -> None:
    errors: list[str] = []
    for relative, text in text_by_path.items():
        if not relative.endswith(".md"):
            continue
        parent = PurePosixPath(relative).parent
        for match in MARKDOWN_LINK.finditer(text):
            target = _local_link_target(match.group(1))
            if target is None:
                continue
            combined = parent / target
            normalized = PurePosixPath(os.path.normpath(combined.as_posix())).as_posix()
            if normalized == "." or normalized.startswith("../") or normalized.startswith("/"):
                errors.append(f"link escapes public root: {relative} -> {target}")
                continue
            disk = source / normalized
            if disk.is_file() and normalized not in selected:
                errors.append(f"link target is excluded: {relative} -> {normalized}")
            elif disk.is_dir() and not any(
                item.startswith(f"{normalized}/") for item in selected
            ):
                errors.append(f"linked directory is excluded: {relative} -> {normalized}")
            elif not disk.exists():
                errors.append(f"broken local link: {relative} -> {normalized}")
    if errors:
        raise ProjectionError("\n".join(errors))


def _python_module_candidates(module: str) -> tuple[str, str]:
    stem = module.replace(".", "/")
    return f"{stem}.py", f"{stem}/__init__.py"


def _check_python_imports(text_by_path: dict[str, str], selected: set[str]) -> None:
    errors: list[str] = []
    for relative, text in text_by_path.items():
        if not relative.endswith(".py"):
            continue
        try:
            tree = ast.parse(text, filename=relative)
        except SyntaxError as exc:
            raise ProjectionError(f"invalid public Python source {relative}: {exc}") from exc
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.append(node.module)
            for module in modules:
                if module.split(".", 1)[0] not in LOCAL_PYTHON_ROOTS:
                    continue
                candidates = _python_module_candidates(module)
                if not any(candidate in selected for candidate in candidates):
                    errors.append(
                        f"local Python import is not in public tree: {relative} -> {module}"
                    )
    if errors:
        raise ProjectionError("\n".join(errors))


def _project_import_roots(source: Path, selected: set[str]) -> dict[str, str]:
    roots: dict[str, str] = {}
    for relative in sorted(selected):
        if not relative.endswith("zlang.toml"):
            continue
        manifest = source / relative
        try:
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ProjectionError(f"invalid project manifest {relative}: {exc}") from exc
        project = data.get("project")
        if not isinstance(project, dict):
            continue
        name = project.get("name")
        source_root = project.get("source-root", "src")
        if isinstance(name, str) and isinstance(source_root, str):
            root = (PurePosixPath(relative).parent / source_root).as_posix()
            roots[name] = root
    return roots


def _check_zlang_imports(
    text_by_path: dict[str, str], selected: set[str], source: Path
) -> None:
    projects = _project_import_roots(source, selected)
    errors: list[str] = []
    for relative, text in text_by_path.items():
        if not relative.endswith(".zhl"):
            continue
        for module in ZLANG_IMPORT.findall(text):
            parts = module.split(".")
            target: str | None = None
            if parts[0] == "std":
                target = f"stdlib/{'/'.join(parts[1:])}.zhl"
            elif parts[0] in projects:
                suffix = "/".join(parts[1:])
                target = f"{projects[parts[0]]}/{suffix}.zhl"
            if target is not None and target not in selected:
                errors.append(f"ZLang import is not in public tree: {relative} -> {target}")
    if errors:
        raise ProjectionError("\n".join(errors))


def _check_action_pins(text_by_path: dict[str, str]) -> None:
    errors: list[str] = []
    for relative, text in text_by_path.items():
        if not relative.startswith(".github/workflows/"):
            continue
        for use in ACTION_USE.findall(text):
            if use.startswith("./"):
                continue
            if not IMMUTABLE_ACTION.fullmatch(use):
                errors.append(f"GitHub Action is not pinned by commit: {relative}: {use}")
    if errors:
        raise ProjectionError("\n".join(errors))


def validate_source(source: Path, config_path: Path = DEFAULT_CONFIG) -> tuple[Path, ...]:
    source = source.resolve()
    config = _load_config(source, config_path)
    all_files = _source_files(source, config.manifest, config.exclude)
    selected_paths = selected_files(source, config)
    selected = {_relative(path, source) for path in selected_paths}
    _check_required(selected, source, config)
    _check_closure_roots(all_files, selected, source, config)
    text_by_path = {
        _relative(path, source): _decode_text(path, _relative(path, source), config)
        for path in selected_paths
    }
    _check_content(text_by_path, config)
    _check_markdown_links(text_by_path, selected, source)
    _check_python_imports(text_by_path, selected)
    _check_zlang_imports(text_by_path, selected, source)
    _check_action_pins(text_by_path)
    return selected_paths


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _mode(path: Path) -> str:
    executable = bool(path.stat().st_mode & stat.S_IXUSR)
    return "0755" if executable else "0644"


def _manifest(source: Path, selected: tuple[Path, ...], config: ProjectionConfig) -> dict:
    config_digest = hashlib.sha256(config.path.read_bytes()).hexdigest()
    return {
        "schema": MANIFEST_SCHEMA,
        "repository": config.repository,
        "config_sha256": config_digest,
        "files": [
            {
                "path": _relative(path, source),
                "sha256": _sha256(path),
                "mode": _mode(path),
            }
            for path in selected
        ],
    }


def export_tree(source: Path, destination: Path, config_path: Path = DEFAULT_CONFIG) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if destination == source or destination.is_relative_to(source):
        raise ProjectionError("destination must be outside the source checkout")
    if destination.exists() and any(destination.iterdir()):
        raise ProjectionError(f"destination is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    selected = validate_source(source, config_path)
    config = _load_config(source, config_path)
    for path in selected:
        relative = Path(_relative(path, source))
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, output)
        output.chmod(0o755 if _mode(path) == "0755" else 0o644)
    manifest = _manifest(source, selected, config)
    manifest_path = destination.joinpath(*PurePosixPath(config.manifest).parts)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest_path.chmod(0o644)


def check_export(source: Path, config_path: Path = DEFAULT_CONFIG) -> None:
    source = source.resolve()
    config = _load_config(source, config_path)
    selected = validate_source(source, config_path)
    manifest_path = source.joinpath(*PurePosixPath(config.manifest).parts)
    try:
        recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectionError(f"cannot read public-tree manifest: {exc}") from exc
    expected = _manifest(source, selected, config)
    if recorded != expected:
        raise ProjectionError("public-tree manifest does not match exported contents")
    exported = {
        _relative(path, source)
        for path in _source_files(source, config.manifest)
    }
    selected_relatives = {_relative(path, source) for path in selected}
    unexpected = sorted(exported - selected_relatives)
    if unexpected:
        raise ProjectionError(
            "files outside public allow-list:\n" + "\n".join(unexpected)
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("check-source", "check-export"):
        child = subparsers.add_parser(command)
        child.add_argument("--source", type=Path, default=Path("."))
        child.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    export = subparsers.add_parser("export")
    export.add_argument("--source", type=Path, default=Path("."))
    export.add_argument("--destination", type=Path, required=True)
    export.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "check-source":
            selected = validate_source(args.source, args.config)
            print(f"public source projection valid: {len(selected)} files")
        elif args.command == "check-export":
            check_export(args.source, args.config)
            print("public export valid")
        else:
            export_tree(args.source, args.destination, args.config)
            print(f"public snapshot exported to {args.destination.resolve()}")
    except ProjectionError as exc:
        print(f"public-tree: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
