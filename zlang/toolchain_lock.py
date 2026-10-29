# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Immutable exact SDK requirements and uncached, path-independent inventories.

These are project input identities, not installer instructions or proof claims.
No Git state, timestamps, absolute paths or process-global caches participate.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib import metadata, util
from pathlib import Path, PurePosixPath
import sys
from typing import Mapping

from zlang.common.serialization import stable_digest
from zlang.dependencies import validate_digest, validate_logical_path


TOOLCHAIN_SCHEMA = "zlang-toolchain-pin-v1"
TOOLCHAIN_PROJECT_SCHEMA = 2
TOOLCHAIN_LOCK_SCHEMA = 3


def _compiler_files():
    root = Path(__file__).resolve().parent
    return tuple(
        sorted(
            (p.relative_to(root).as_posix(), sha256(p.read_bytes()).hexdigest())
            for p in root.rglob("*")
            if p.is_file() and p.suffix in {".py", ".lark", ".json"}
        )
    )


# Immutable process-start observation, not a result cache. An interpreter may
# not bless replaced compiler files using already imported old Python code.
_BOOT_COMPILER_FILES = _compiler_files()


@dataclass(frozen=True)
class ToolchainRequirement:
    compiler_version: str
    compiler_identity: str
    stdlib_identity: str

    def __post_init__(self):
        if not isinstance(self.compiler_version, str) or not self.compiler_version:
            raise ValueError("toolchain compiler-version must be nonempty")
        for digest in (self.compiler_identity, self.stdlib_identity):
            validate_digest(digest, description="toolchain identity")

    def to_data(self):
        return dict(
            zip(
                ("compiler-version", "compiler-identity", "stdlib-identity"),
                (self.compiler_version, self.compiler_identity, self.stdlib_identity),
            )
        )

    @classmethod
    def from_data(cls, data):
        if not isinstance(data, Mapping) or set(data) != {
            "compiler-version",
            "compiler-identity",
            "stdlib-identity",
        }:
            raise ValueError(
                "toolchain requires exact compiler-version/compiler-identity/stdlib-identity"
            )
        return cls(
            data["compiler-version"], data["compiler-identity"], data["stdlib-identity"]
        )


def _inventory(items, *, kind):
    rows = tuple(items)
    if any(
        not isinstance(row, tuple)
        or len(row) != 2
        or not all(isinstance(v, str) for v in row)
        for row in rows
    ):
        raise ValueError(f"invalid {kind} inventory rows")
    rows = tuple(sorted(rows))
    if not rows or len(rows) > 10_000 or len({name for name, _ in rows}) != len(rows):
        raise ValueError(f"invalid {kind} inventory")
    for name, digest in rows:
        if kind == "stdlib":
            validate_logical_path(name)
            if not name.startswith("std."):
                raise ValueError("stdlib inventory must use std namespaces")
        else:
            path = PurePosixPath(name)
            if (
                not name
                or path.is_absolute()
                or path.as_posix() != name
                or ".." in path.parts
                or "\\" in name
            ):
                raise ValueError("compiler inventory requires relative logical paths")
        validate_digest(digest, description=f"{kind} content digest")
    return rows


def _runtime_inventory(items):
    rows = tuple(items)
    if (
        not rows
        or len(rows) > 100
        or any(
            not isinstance(row, tuple)
            or len(row) != 2
            or not all(isinstance(v, str) and v for v in row)
            for row in rows
        )
    ):
        raise ValueError("invalid compiler runtime inventory")
    if len({k for k, _ in rows}) != len(rows):
        raise ValueError("duplicate compiler runtime identity")
    return tuple(sorted(rows))


@dataclass(frozen=True)
class LockedToolchain:
    requirement: ToolchainRequirement
    compiler_files: tuple[tuple[str, str], ...]
    runtime: tuple[tuple[str, str], ...]
    stdlib_modules: tuple[tuple[str, str], ...]

    def __post_init__(self):
        if not isinstance(self.requirement, ToolchainRequirement):
            raise ValueError("locked toolchain requires a typed requirement")
        object.__setattr__(
            self, "compiler_files", _inventory(self.compiler_files, kind="compiler")
        )
        object.__setattr__(
            self, "stdlib_modules", _inventory(self.stdlib_modules, kind="stdlib")
        )
        runtime = _runtime_inventory(self.runtime)
        object.__setattr__(self, "runtime", runtime)
        if self.requirement != self.identify(
            self.requirement.compiler_version,
            self.compiler_files,
            runtime,
            self.stdlib_modules,
        ):
            raise ValueError(
                "toolchain identities do not match their complete inventories"
            )

    @staticmethod
    def identify(version, files, runtime, modules):
        files = _inventory(files, kind="compiler")
        runtime = _runtime_inventory(runtime)
        modules = _inventory(modules, kind="stdlib")
        return ToolchainRequirement(
            version,
            stable_digest(
                {
                    "schema": TOOLCHAIN_SCHEMA,
                    "version": version,
                    "files": list(files),
                    "runtime": list(runtime),
                }
            ),
            stable_digest({"schema": TOOLCHAIN_SCHEMA, "modules": list(modules)}),
        )

    def to_data(self):
        return {
            "schema": TOOLCHAIN_SCHEMA,
            **self.requirement.to_data(),
            "compiler-files": dict(self.compiler_files),
            "runtime": dict(self.runtime),
            "stdlib-modules": dict(self.stdlib_modules),
        }

    @classmethod
    def from_data(cls, data):
        keys = {
            "schema",
            "compiler-version",
            "compiler-identity",
            "stdlib-identity",
            "compiler-files",
            "runtime",
            "stdlib-modules",
        }
        if (
            not isinstance(data, Mapping)
            or set(data) != keys
            or data["schema"] != TOOLCHAIN_SCHEMA
        ):
            raise ValueError("invalid locked toolchain schema or fields")
        if any(
            not isinstance(data[k], Mapping)
            for k in ("compiler-files", "runtime", "stdlib-modules")
        ):
            raise ValueError("toolchain inventories must be tables")
        return cls(
            ToolchainRequirement.from_data(
                {
                    k: data[k]
                    for k in keys
                    if k.endswith("identity") or k == "compiler-version"
                }
            ),
            tuple(data["compiler-files"].items()),
            tuple(data["runtime"].items()),
            tuple(data["stdlib-modules"].items()),
        )


def current_toolchain() -> LockedToolchain:
    """Fingerprint actual compiler bytes and the resolver's entire stdlib.

    Re-read content on each request, even when a workspace/parse cache is warm.
    Relocation and pycache creation do not change this logical inventory.
    """
    from zlang._version import __version__
    from zlang.schema_registry import compiler_compatibility_identity
    from zlang.simulation_plan_policy import SIMULATION_RUNTIME_ABI
    from zlang.stdlib import stdlib_source_inventory

    files = _compiler_files()
    if files != _BOOT_COMPILER_FILES:
        raise ValueError(
            "compiler files changed in this process; restart the compiler/LSP before relocking"
        )
    runtime = {
        "python-abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
        "compatibility": compiler_compatibility_identity(),
        "native-abi": str(SIMULATION_RUNTIME_ABI),
    }
    for dependency in ("lark", "egglog"):
        runtime[dependency] = metadata.version(dependency)
    native = util.find_spec("_zlang_native_sim")
    if native is None:
        runtime["native"] = "absent"
    else:
        from zlang.sim import _native_runtime

        _native_runtime()  # Existing ABI admission, never a simulation fallback.
        base = Path(native.origin).parent
        native_files = tuple(
            sorted(
                (p.relative_to(base).as_posix(), sha256(p.read_bytes()).hexdigest())
                for p in base.rglob("*")
                if p.is_file() and p.suffix in {".py", ".so", ".pyd"}
            )
        )
        runtime["native"] = stable_digest(native_files)
        runtime["native-version"] = metadata.version("zlang-native-sim")
    modules = stdlib_source_inventory()
    rows = tuple(sorted(runtime.items()))
    return LockedToolchain(
        LockedToolchain.identify(__version__, files, rows, modules),
        files,
        rows,
        modules,
    )


def require_toolchain(requirements, locked=None):
    """Check requirements and an optional resolved lock against fresh SDK bytes."""
    snapshot = current_toolchain()
    for requirement in requirements:
        if requirement != snapshot.requirement:
            raise ValueError(
                "toolchain mismatch: install the pinned compiler/stdlib; compiler updates require LSP restart"
            )
    if locked is not None and snapshot != locked:
        raise ValueError(
            "stale locked toolchain inventory; explicit qualification and lock update required"
        )
    return snapshot
