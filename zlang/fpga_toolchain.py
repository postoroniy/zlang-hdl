"""Pinned executable and device-database identity for bounded FPGA builds."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path

from zlang.common.files import file_digest


SCHEMA = "zlang-fpga-toolchain-v1"
OSS_VERSION = "2026-10-21"
OSS_ARCHIVE_SHA256 = "dde9f1700dece195a98a9b15845a4f4043b2aab1dacf9be498bbad0c3be1f247"
OSS_FILES_SHA256 = "2922db5dde276f1898f3e704fe109a53162005068112704f50e7b4dc99321d72"
OSS_TOOLS = (
    "yosys", "verilator", "nextpnr-ecp5", "nextpnr-himbaechel",
    "ecppack", "gowin_pack", "openFPGALoader",
)


class FpgaToolchainError(ValueError):
    """A pinned executable, dependency or device database is unavailable/stale."""


def inventory_digest(rows: list[dict[str, object]]) -> str:
    return hashlib.sha256(json.dumps(
        rows, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def inventory_tree(root: Path) -> list[dict[str, object]]:
    """Capture one installed distribution without path-dependent launcher hashes."""
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            if not path.resolve().is_relative_to(root.resolve()):
                raise FpgaToolchainError("tool dependency symlink escapes distribution")
            rows.append({"path": path.relative_to(root).as_posix(),
                         "symlink": str(path.readlink())})
        elif path.is_file():
            rows.append({"path": path.relative_to(root).as_posix(),
                         "sha256": file_digest(path), "bytes": path.stat().st_size,
                         "mode": path.stat().st_mode & 0o777})
    return rows


@dataclass(frozen=True)
class FpgaToolchain:
    """One immutable, fully checked tool/library/chip-database snapshot."""

    root: Path
    manifest: Path
    identity: str
    files_identity: str

    def executable(self, name: str) -> Path:
        if name not in OSS_TOOLS:
            raise FpgaToolchainError(f"unavailable FPGA tool '{name}'")
        path = self.root / "bin" / name
        if not path.is_file() or not os.access(path, os.X_OK):
            raise FpgaToolchainError(f"missing executable '{name}'")
        return path

    def environment(self, home: Path) -> dict[str, str]:
        # OSS launchers select their bundled loader/Python and support files.
        # Do not inherit ambient Python, preload, library or solver overrides.
        return {
            "PATH": str(self.root / "bin") + os.pathsep + os.defpath,
            "HOME": str(home), "LANG": "C", "LC_ALL": "C",
            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        }

    def verify(self) -> None:
        checked = load_toolchain(self.manifest)
        if checked.root != self.root or checked.identity != self.identity:
            raise FpgaToolchainError("FPGA toolchain identity changed during build")

    def tool_record(self, name: str) -> dict[str, str]:
        executable = self.executable(name)
        return {"name": name, "path": "bin/" + name,
                "executable_sha256": file_digest(executable),
                "distribution": "oss-cad-suite-" + OSS_VERSION,
                "dependency_inventory_sha256": self.files_identity}


def load_toolchain(path: Path | str) -> FpgaToolchain:
    """Reject changed executables, supporting libraries and device databases."""
    path = Path(path).resolve()
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or set(data) != {
            "schema", "root", "version", "archive_sha256", "files_sha256", "files",
        }:
            raise FpgaToolchainError("invalid FPGA toolchain manifest fields")
        if (data["schema"], data["version"], data["archive_sha256"], data["files_sha256"]) != (
            SCHEMA, OSS_VERSION, OSS_ARCHIVE_SHA256, OSS_FILES_SHA256,
        ):
            raise FpgaToolchainError("unsupported FPGA toolchain pin/policy")
        rows = data["files"]
        if not isinstance(rows, list) or inventory_digest(rows) != OSS_FILES_SHA256:
            raise FpgaToolchainError("stale FPGA executable/dependency inventory")
        root = Path(data["root"])
        if not root.is_absolute() or not root.is_dir() or root.is_symlink():
            raise FpgaToolchainError("FPGA toolchain root must be a real absolute directory")
        root = root.resolve()
        # The inventory hash authenticates every path, mode and symlink before
        # filesystem traversal. Exact membership also catches new Python caches.
        actual = inventory_tree(root)
        if actual != rows:
            raise FpgaToolchainError("FPGA executable/library/device-database bytes changed")
        identity = hashlib.sha256(json.dumps({
            k: v for k, v in data.items() if k not in {"root", "files"}
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        result = FpgaToolchain(root, path, identity, OSS_FILES_SHA256)
        for name in OSS_TOOLS:
            result.executable(name)
        return result
    except (OSError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, FpgaToolchainError):
            raise
        raise FpgaToolchainError(f"invalid FPGA toolchain manifest: {error}") from error


__all__ = ["FpgaToolchain", "FpgaToolchainError", "load_toolchain"]
