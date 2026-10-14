#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Audit the reviewed Linux-x64 debugger and waveform executables."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys


EXPECTED_CONTRACTS = {
    "debug_adapter": "zlang-debug-adapter-v1",
    "debug_bundle": "zlang-native-debug-bundle-v1",
    "waveform_query": "zlang-waveform-query-v2",
    "waveform_trace": "zlang-waveform-trace-v1",
}
EXPECTED_EXECUTABLES = (
    "zlang-debug-adapter",
    "zlang-waveform-indexer",
)


class NativeEditorAuditError(ValueError):
    """The staged native editor payload is incomplete or unreviewed."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_file(root: Path, value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise NativeEditorAuditError(f"{label} must be a repository-relative path")
    logical = PurePosixPath(value)
    if logical.is_absolute() or logical.as_posix() != value or ".." in logical.parts:
        raise NativeEditorAuditError(f"{label} must be a normalized relative path")
    path = root.joinpath(*logical.parts)
    if path.is_symlink() or not path.is_file():
        raise NativeEditorAuditError(f"{label} is not a regular file: {value}")
    return path


def _audit_elf(path: Path) -> None:
    payload = path.read_bytes()
    if len(payload) < 64 or payload[:7] != b"\x7fELF\x02\x01\x01":
        raise NativeEditorAuditError(f"{path.name} is not a 64-bit little-endian ELF")
    if int.from_bytes(payload[16:18], "little") != 3:
        raise NativeEditorAuditError(f"{path.name} is not a position-independent executable")
    if int.from_bytes(payload[18:20], "little") != 62:
        raise NativeEditorAuditError(f"{path.name} is not an x86-64 executable")
    if not path.stat().st_mode & stat.S_IXUSR:
        raise NativeEditorAuditError(f"{path.name} is not executable")
    for marker in (b"/home/", b"/Users/", b"native-runtime/"):
        if marker in payload:
            raise NativeEditorAuditError(
                f"{path.name} contains a private build/source path marker"
            )
    try:
        completed = subprocess.run(
            ("readelf", "-d", str(path)),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeEditorAuditError(f"cannot inspect {path.name}: {exc}") from exc
    if completed.returncode != 0:
        raise NativeEditorAuditError(
            f"readelf rejected {path.name}: {completed.stderr.strip()}"
        )
    if re.search(r"\((?:RPATH|RUNPATH)\)", completed.stdout):
        raise NativeEditorAuditError(f"{path.name} contains RPATH/RUNPATH")


def audit_native_editor(
    root: Path,
    status_path: Path = Path("release/status.json"),
) -> dict[str, object]:
    """Validate exact bytes and the bounded public native-editor inventory."""

    root = root.resolve()
    path = status_path if status_path.is_absolute() else root / status_path
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NativeEditorAuditError(f"cannot read {path}: {exc}") from exc
    native = status.get("native_editor")
    if not isinstance(native, dict) or set(native) != {
        "contracts",
        "executables",
        "platform",
        "source_distribution",
        "third_party_licenses",
    }:
        raise NativeEditorAuditError("native_editor release metadata is malformed")
    if native["platform"] != "linux-x64" or native["source_distribution"] is not False:
        raise NativeEditorAuditError("native editor platform/source contract changed")
    if native["contracts"] != EXPECTED_CONTRACTS:
        raise NativeEditorAuditError("native editor protocol contracts changed")
    records = native["executables"]
    if not isinstance(records, list) or [record.get("name") for record in records] != list(
        EXPECTED_EXECUTABLES
    ):
        raise NativeEditorAuditError("native editor executable inventory is not exact")
    accepted: list[dict[str, object]] = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "name", "path", "sha256", "size",
        }:
            raise NativeEditorAuditError("native editor executable record is malformed")
        executable = _relative_file(root, record["path"], label=record["name"])
        if executable.name != record["name"]:
            raise NativeEditorAuditError("native editor executable name/path mismatch")
        size = executable.stat().st_size
        digest = _sha256(executable)
        if record["size"] != size or record["sha256"] != digest:
            raise NativeEditorAuditError(
                f"native editor identity is stale: {record['name']}"
            )
        _audit_elf(executable)
        accepted.append({"name": record["name"], "sha256": digest, "size": size})
    notice = native["third_party_licenses"]
    if not isinstance(notice, dict) or set(notice) != {"path", "sha256"}:
        raise NativeEditorAuditError("native editor license record is malformed")
    notice_path = _relative_file(root, notice["path"], label="third_party_licenses")
    if notice["sha256"] != _sha256(notice_path):
        raise NativeEditorAuditError("native editor third-party inventory is stale")
    directory = root / "release/native-editor/linux-x64"
    if sorted(item.name for item in directory.iterdir()) != list(EXPECTED_EXECUTABLES):
        raise NativeEditorAuditError("native editor release directory has unexpected files")
    return {
        "contracts": native["contracts"],
        "executables": accepted,
        "platform": native["platform"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--status", type=Path, default=Path("release/status.json"))
    arguments = parser.parse_args(argv)
    try:
        report = audit_native_editor(arguments.root, arguments.status)
    except NativeEditorAuditError as exc:
        print(f"native-editor-audit: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
