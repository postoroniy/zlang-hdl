# SPDX-License-Identifier: Apache-2.0
"""Bind Z3 execution through relocatable OSS CAD Suite launchers.

An OSS launcher prepends its own bin directory, overriding the caller's PATH.
A run-local view keeps those launchers and libraries unchanged while binding
its solver slot to the executable discovered for this invocation. No installed
suite or process-global environment is modified.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import tempfile

from zlang.ir.formal import FormalError


EXECUTION_TOOL_POLICY = "zlang-formal-execution-tools-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1 << 20), b""):
            digest.update(data)
    return digest.hexdigest()


@dataclass(frozen=True)
class FormalToolBinding:
    """One frozen solver and launcher identity; paths are operational facts."""

    solver: str
    solver_path: Path
    suite: Path | None
    files: tuple[tuple[str, Path, str], ...]

    @classmethod
    def discover(cls, solver: str) -> FormalToolBinding | None:
        if solver != "z3":
            return None
        selected = shutil.which(solver)
        if selected is None or not Path(selected).is_file():
            return None
        selected_path = Path(selected).absolute()
        paths = {"solver": selected_path}
        suite = None
        smtbmc, sby = shutil.which("yosys-smtbmc"), shutil.which("sby")
        yosys = shutil.which("yosys")
        if smtbmc and sby:
            driver, engine = Path(smtbmc).resolve(), Path(sby).resolve()
            candidate = driver.parent.parent
            rewrites_path = any(path.is_file() and b'release_bindir_abs:$PATH' in path.read_bytes()
                                for path in (driver, engine))
            if (driver.parent.name == "bin" and engine.parent == driver.parent
                    and yosys is not None and Path(yosys).resolve().parent == driver.parent
                    and (candidate / "libexec/yosys-smtbmc").is_file()
                    and (candidate / "libexec/sby").is_file()
                    and (candidate / "bin/tabbypy3").is_file()
                    and b'release_bindir_abs:$PATH' in driver.read_bytes()
                    and b'release_bindir_abs:$PATH' in engine.read_bytes()):
                suite = candidate
                paths.update({"smtbmc_launcher": driver, "sby_launcher": engine,
                              "yosys_launcher": Path(yosys).resolve(),
                              "python_launcher": candidate / "bin/tabbypy3",
                              "sby_driver": candidate / "libexec/sby",
                              "smtbmc_driver": candidate / "libexec/yosys-smtbmc"})
            elif rewrites_path:
                raise FormalError("cannot bind solver through a mixed or incomplete OSS toolchain")
        return cls(solver, selected_path, suite,
                   tuple((name, path, _sha256(path)) for name, path in sorted(paths.items())))

    def validate(self) -> None:
        try:
            changed = any(_sha256(path) != digest for _, path, digest in self.files)
        except OSError as error:
            raise FormalError("formal execution tool disappeared after discovery") from error
        if changed:
            raise FormalError("formal execution tool changed after discovery")

    @property
    def identity(self) -> dict:
        self.validate()
        return {"policy": EXECUTION_TOOL_POLICY, "solver": self.solver,
                "files": {name: digest for name, _, digest in self.files}}

    @contextmanager
    def environment(self):
        self.validate()
        environment = dict(os.environ)
        if self.suite is None:
            environment["PATH"] = str(self.solver_path.parent) + os.pathsep + environment.get("PATH", "")
            yield environment
            return
        # OSS launchers contain unquoted executable paths. Keep this derived
        # tools view outside possibly space-containing user work directories.
        with tempfile.TemporaryDirectory(prefix="zlang-formal-tools-", dir="/tmp") as temporary:
            root = Path(temporary)
            for path in self.suite.iterdir():
                if path.name != "bin":
                    (root / path.name).symlink_to(path, target_is_directory=path.is_dir())
            bin_directory = root / "bin"
            bin_directory.mkdir()
            for path in (self.suite / "bin").iterdir():
                if path.name != self.solver:
                    (bin_directory / path.name).symlink_to(path, target_is_directory=path.is_dir())
            (bin_directory / self.solver).symlink_to(self.solver_path)
            environment["PATH"] = str(bin_directory) + os.pathsep + environment.get("PATH", "")
            # SBY's documented overrides must agree with discovery, rather than
            # allowing inherited overrides to silently change the execution.
            environment["SMTBMC"] = shlex.quote(str(bin_directory / "yosys-smtbmc"))
            environment["YOSYS"] = shlex.quote(str(bin_directory / "yosys"))
            yield environment


def binding_identity(binding: FormalToolBinding | None) -> dict:
    return {"policy": EXECUTION_TOOL_POLICY} if binding is None else binding.identity


def execution_environment(binding: FormalToolBinding | None):
    return nullcontext(None) if binding is None else binding.environment()
