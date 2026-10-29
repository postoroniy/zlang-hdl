# SPDX-License-Identifier: Apache-2.0
"""Check the Community publication payload, artifacts and reachable ancestry.

This is a distribution validator, not a compiler pass or a general IP detector.
Known private source fingerprints supplement exact namespace/import checks.
"""

from __future__ import annotations

import argparse
import ast
from hashlib import sha256
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import zipfile


POLICY = Path("release/community-boundary.json")
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024


class BoundaryError(ValueError):
    """A candidate contains an unavailable owner or unreviewed publication input."""


def body_fingerprints(text: str) -> set[str]:
    tree = ast.parse(text)
    return {
        sha256(
            ast.dump(
                ast.Module(body=n.body, type_ignores=[]), include_attributes=False
            ).encode()
        ).hexdigest()
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and sum(1 for _ in ast.walk(n)) >= 35
    }


def load_policy(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise BoundaryError(
            f"missing or malformed Community boundary policy: {path}"
        ) from error
    if data.get("schema") != 1:
        raise BoundaryError("unsupported Community boundary policy schema")
    for key in (
        "module_prefixes",
        "path_prefixes",
        "source_sha256",
        "python_body_sha256",
        "document_markers",
        "denied_commits",
        "reviewed_binary_sha256",
    ):
        if not isinstance(data.get(key), list) or not all(
            isinstance(v, str) for v in data[key]
        ):
            raise BoundaryError(f"malformed Community policy field: {key}")
    for key in (
        "module_prefixes",
        "path_prefixes",
        "source_sha256",
        "python_body_sha256",
        "document_markers",
        "reviewed_binary_sha256",
    ):
        if not data[key]:
            raise BoundaryError(f"empty required Community policy field: {key}")
    for key in ("source_sha256", "python_body_sha256", "reviewed_binary_sha256"):
        if any(
            len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
            for value in data[key]
        ):
            raise BoundaryError(f"invalid fingerprint field: {key}")
    if not isinstance(data.get("public_base"), str) or len(data["public_base"]) != 40:
        raise BoundaryError("Community policy needs an exact public baseline")
    return data


def _module_private(name: str, policy: dict) -> bool:
    return any(name.startswith(prefix) for prefix in policy["module_prefixes"])


def _constant_string(node, budget=32):
    if budget <= 0:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = (
            _constant_string(node.left, budget - 1),
            _constant_string(node.right, budget - 1),
        )
        if left is not None and right is not None:
            return left + right
    return None


def check_member(name: str, content: bytes, policy: dict) -> None:
    parts = PurePosixPath(name).parts
    if (
        not parts
        or name.startswith("/")
        or ".." in parts
        or "\\" in name
        or PurePosixPath(name).as_posix() != name
        or ":" in parts[0]
    ):
        raise BoundaryError(f"unsafe payload path: {name}")
    # Wheels place stdlib under <distribution>.data/data/stdlib.
    logical = name.split(".data/data/", 1)[-1]
    if logical.startswith("extension/"):
        logical = logical[len("extension/") :]
    if any(logical.startswith(p) for p in policy["path_prefixes"]):
        raise BoundaryError(f"private publication owner: {name}")
    digest = sha256(content).hexdigest()
    if digest in policy["source_sha256"]:
        raise BoundaryError(f"known private source content: {name}")
    if logical.endswith((".pyc", ".pyo")):
        raise BoundaryError(f"compiled Python is not a reviewed source payload: {name}")
    if content.startswith((b"\x7fELF", b"MZ", b"\x00asm")):
        if digest not in policy["reviewed_binary_sha256"]:
            raise BoundaryError(f"unreviewed executable bytes: {name}")
        return
    if logical.endswith(".py"):
        try:
            text = content.decode("utf-8")
            tree = ast.parse(text)
        except (UnicodeError, SyntaxError) as error:
            raise BoundaryError(f"cannot inspect Python payload: {name}") from error
        if body_fingerprints(text).intersection(policy["python_body_sha256"]):
            raise BoundaryError(f"private implementation fingerprint: {name}")
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parent = list(PurePosixPath(logical).with_suffix("").parts)[
                        : -node.level
                    ]
                    base = ".".join([*parent, *([base] if base else [])])
                names = [base, *(base + "." + a.name for a in node.names)]
            elif isinstance(node, ast.Call):
                function = node.func
                if (isinstance(function, ast.Name) and function.id == "__import__") or (
                    isinstance(function, ast.Attribute)
                    and function.attr == "import_module"
                ):
                    value = _constant_string(node.args[0]) if node.args else None
                    names = [value] if value is not None else []
            if any(_module_private(n, policy) for n in names):
                raise BoundaryError(f"private dependency in Community: {name}")
            if logical == "zlang/cli.py" and isinstance(node, ast.Compare):
                if any(
                    isinstance(n, ast.Constant) and n.value == "trace"
                    for n in ast.walk(node)
                ):
                    raise BoundaryError(
                        "private hardware trace CLI dispatch is exposed"
                    )
            if (
                logical == "zlang/formal.py"
                and isinstance(node, ast.FunctionDef)
                and node.name == "run_verilog_formal"
                and any(
                    arg.arg == "multiclock"
                    for arg in [*node.args.args, *node.args.kwonlyargs]
                )
            ):
                raise BoundaryError("private multiclock preparation adapter is exposed")
            if logical == "zlang/implementation_request.py" and isinstance(
                node, (ast.Set, ast.Dict)
            ):
                if any(
                    isinstance(n, ast.Constant) and n.value in ("dft", "hardware-debug")
                    for n in ast.walk(node)
                ):
                    raise BoundaryError("private profile handler is exposed")
    if logical.endswith((".md", ".rst")):
        text = content.decode("utf-8")
        if any(marker in text for marker in policy["document_markers"]):
            raise BoundaryError(f"private documentation in Community: {name}")
    if logical.endswith(".zhl"):
        text = content.decode("utf-8")
        if "import std.debug" in text:
            raise BoundaryError(f"private stdlib dependency: {name}")


def validate_tree(root: Path, *, policy_path: Path | None = None, paths=None) -> int:
    policy = load_policy(policy_path or root / POLICY)
    if paths is None:
        # Git supplies the prospective source payload, excluding tool caches.
        # Production roots are also walked directly: an ignored Python module
        # must not become an accidental packaging or import escape hatch.
        excluded = {
            ".git",
            ".venv",
            "build",
            "dist",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            ".worktrees",
            ".vscode-test",
        }
        selected = set()
        if (root / ".git").exists():
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "ls-files",
                    "-c",
                    "-o",
                    "--exclude-standard",
                    "-z",
                ],
                capture_output=True,
                timeout=30,
            )
            if result.returncode:
                raise BoundaryError("cannot inventory Community source")
            selected.update(root / n for n in result.stdout.decode().split("\0") if n)
        else:
            selected.update(
                p
                for p in root.rglob("*")
                if not any(part in excluded for part in p.relative_to(root).parts)
            )
        for directory in (root / "zlang", root / "stdlib"):
            selected.update(
                p
                for p in directory.rglob("*")
                if "__pycache__" not in p.relative_to(root).parts
            )
        for prefix in policy["path_prefixes"]:
            if prefix.endswith("/") and (root / prefix).exists():
                raise BoundaryError(f"private publication directory: {prefix}")
        for path in selected:
            if path.is_symlink():
                raise BoundaryError(
                    f"publication source symlink: {path.relative_to(root)}"
                )
        paths = sorted(p for p in selected if p.is_file())
    count = 0
    for path in paths:
        check_member(path.relative_to(root).as_posix(), path.read_bytes(), policy)
        count += 1
    return count


def validate_artifact(path: Path, policy: dict, depth=0) -> int:
    return _artifact_bytes(path.name, path.read_bytes(), policy, depth)


def _artifact_bytes(name, content, policy, depth):
    if depth > 3 or len(content) > MAX_ARTIFACT_BYTES:
        raise BoundaryError("artifact nesting/size is outside the inspected domain")
    members = []
    stream = io.BytesIO(content)
    if zipfile.is_zipfile(stream):
        with zipfile.ZipFile(stream) as archive:
            total = 0
            for member in archive.infolist():
                total += member.file_size
                if total > MAX_ARTIFACT_BYTES:
                    raise BoundaryError("expanded artifact is too large")
                if not member.is_dir():
                    members.append((member.filename, archive.read(member)))
    else:
        stream.seek(0)
        try:
            with tarfile.open(fileobj=stream, mode="r:*") as archive:
                total = 0
                for member in archive:
                    if member.isdir():
                        continue
                    if not member.isfile():
                        raise BoundaryError(
                            "artifact links/special entries are not inspected"
                        )
                    total += member.size
                    if total > MAX_ARTIFACT_BYTES:
                        raise BoundaryError("expanded artifact is too large")
                    members.append((member.name, archive.extractfile(member).read()))
        except tarfile.ReadError as error:
            raise BoundaryError(f"unsupported artifact: {name}") from error
    seen = set()
    for member, data in members:
        if member in seen:
            raise BoundaryError("duplicate artifact member")
        seen.add(member)
        logical = member
        if name.endswith((".tar.gz", ".tgz")) and "/" in member:
            logical = member.split("/", 1)[1]
        check_member(member, data, policy)
        if logical != member:
            check_member(logical, data, policy)
        if member.endswith((".whl", ".vsix", ".zip", ".tar.gz", ".tgz")):
            _artifact_bytes(member, data, policy, depth + 1)
    return len(members)


def _git(root, *args, binary=False):
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=30)
    if r.returncode:
        raise BoundaryError(
            "cannot validate public ancestry: "
            + r.stderr.decode(errors="replace").strip()
        )
    return r.stdout if binary else r.stdout.decode().strip()


def validate_history(root: Path, policy: dict) -> int:
    base = policy["public_base"]
    _git(root, "merge-base", "--is-ancestor", base, "HEAD")
    reachable = set(_git(root, "rev-list", "HEAD").splitlines())
    if reachable.intersection(policy["denied_commits"]):
        raise BoundaryError("private-only commit is reachable from the candidate")
    commits = _git(root, "rev-list", "--reverse", base + "..HEAD").splitlines()
    for commit in commits:
        if len(_git(root, "rev-list", "--parents", "-n", "1", commit).split()) != 2:
            raise BoundaryError("unreviewed merge ancestry after the public baseline")
        names = (
            _git(
                root,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-z",
                commit,
                binary=True,
            )
            .decode("utf-8")
            .rstrip("\0")
            .split("\0")
        )
        for name in names:
            if not name:
                continue
            exists = subprocess.run(
                ["git", "-C", str(root), "cat-file", "-e", commit + ":" + name],
                capture_output=True,
            )
            if exists.returncode == 0:
                check_member(
                    name, _git(root, "show", commit + ":" + name, binary=True), policy
                )
    return len(commits)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy or args.root / POLICY)
        count = validate_tree(args.root, policy_path=args.policy)
        commits = validate_history(args.root, policy) if args.history else None
        artifacts = [validate_artifact(p, policy) for p in args.artifact]
    except (BoundaryError, OSError, ValueError) as error:
        parser.exit(1, f"Community boundary: {error}\n")
    print(
        json.dumps(
            dict(source_files=count, new_commits=commits, artifact_members=artifacts),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
