from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import textwrap
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _stdlib_members(root: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            source.relative_to(root).as_posix()
            for source in root.glob("stdlib/**/*.zhl")
        )
    )


def _build_python() -> str | None:
    candidates = (
        os.environ.get("ZLANG_BUILD_PYTHON"),
        sys.executable,
        "/usr/bin/python3",
        shutil.which("python3"),
    )
    for candidate in candidates:
        if not candidate:
            continue
        probe = subprocess.run(
            [
                candidate,
                "-c",
                (
                    "import pip, setuptools, setuptools.build_meta; "
                    "major=int(setuptools.__version__.split('.', 1)[0]); "
                    "raise SystemExit(0 if major >= 77 else 1)"
                ),
            ],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            return candidate
    return None


def test_wheel_contains_and_resolves_every_shipped_stdlib_module(tmp_path: Path) -> None:
    build_python = _build_python()
    if build_python is None:
        pytest.skip("a local Python with setuptools.build_meta is unavailable")

    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(ROOT / "pyproject.toml", source / "pyproject.toml")
    shutil.copy2(ROOT / "setup.py", source / "setup.py")
    shutil.copy2(ROOT / "README.md", source / "README.md")
    shutil.copy2(ROOT / "LICENSE", source / "LICENSE")
    shutil.copy2(ROOT / "MANIFEST.in", source / "MANIFEST.in")
    shutil.copy2(ROOT / "NOTICE", source / "NOTICE")
    shutil.copytree(ROOT / "zlang", source / "zlang")
    shutil.copytree(ROOT / "stdlib", source / "stdlib")
    # This directory is deliberately absent from repository packaging metadata.
    # Its presence in the wheel proves recursive discovery rather than a manually
    # maintained list of known stdlib families.
    nested = source / "stdlib" / "autodiscovery" / "deep" / "nested.zhl"
    nested.parent.mkdir(parents=True)
    nested.write_text("module PackagingNested { in x:u8 out y:u8 y=x }\n")
    (source / "tests").mkdir()
    (source / "tests" / "test_must_not_ship.py").write_text(
        "raise AssertionError('runtime sdist must not ship a partial test suite')\n"
    )
    wheel_dir = source / "dist"
    wheel_dir.mkdir()

    build = subprocess.run(
        [
            build_python,
            "-c",
            textwrap.dedent(
                """
                import os
                from setuptools.build_meta import build_wheel
                os.chdir(os.environ["ZLANG_WHEEL_SOURCE"])
                print(build_wheel(os.environ["ZLANG_WHEEL_DIST"]))
                """
            ),
        ],
        env={
            **os.environ,
            "ZLANG_WHEEL_SOURCE": str(source),
            "ZLANG_WHEEL_DIST": str(wheel_dir),
        },
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    wheels = tuple(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1

    expected = _stdlib_members(source)
    with zipfile.ZipFile(wheels[0]) as archive:
        metadata_name = next(
            name for name in archive.namelist()
            if name.endswith(".dist-info/METADATA")
        )
        metadata = archive.read(metadata_name).decode("utf-8")
        packaged = tuple(
            sorted(
                name.split(".data/data/", 1)[1]
                for name in archive.namelist()
                if ".data/data/stdlib/" in name and name.endswith(".zhl")
            )
        )
    assert packaged == expected
    assert "stdlib/math/complex.zhl" in packaged
    assert "stdlib/stream/core.zhl" in packaged
    assert "stdlib/stream/serialization.zhl" in packaged
    assert "stdlib/dsp/fft.zhl" in packaged
    assert "stdlib/storage/core.zhl" in packaged
    assert "stdlib/storage.zhl" in packaged
    assert "stdlib/coding/core.zhl" in packaged
    assert "stdlib/coding.zhl" in packaged
    assert "stdlib/bus/ahb_lite.zhl" in packaged
    assert "stdlib/autodiscovery/deep/nested.zhl" in packaged
    assert "Requires-Python: <3.13,>=3.12\n" in metadata
    assert "Version: 0.1.0a6\n" in metadata
    assert "License-Expression: Apache-2.0\n" in metadata
    assert "License-File: LICENSE\n" in metadata
    assert "License-File: NOTICE\n" in metadata

    sdist_dir = source / "sdist"
    sdist_dir.mkdir()
    sdist_build = subprocess.run(
        [
            build_python,
            "-c",
            textwrap.dedent(
                """
                import os
                from setuptools.build_meta import build_sdist
                os.chdir(os.environ["ZLANG_WHEEL_SOURCE"])
                print(build_sdist(os.environ["ZLANG_SDIST_DIST"]))
                """
            ),
        ],
        env={
            **os.environ,
            "ZLANG_WHEEL_SOURCE": str(source),
            "ZLANG_SDIST_DIST": str(sdist_dir),
        },
        capture_output=True,
        text=True,
    )
    assert sdist_build.returncode == 0, sdist_build.stdout + sdist_build.stderr
    sdists = tuple(sdist_dir.glob("*.tar.gz"))
    assert len(sdists) == 1
    with tarfile.open(sdists[0], "r:gz") as archive:
        sdist_members = tuple(archive.getnames())
    assert any(name.endswith("/LICENSE") for name in sdist_members)
    assert any(name.endswith("/NOTICE") for name in sdist_members)
    assert any(name.endswith("/stdlib/math/complex.zhl") for name in sdist_members)
    assert not any("/tests/" in name for name in sdist_members)

    installed = tmp_path / "installed"
    install = subprocess.run(
        [
            build_python,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            "--no-index",
            "--target",
            str(installed),
            str(wheels[0]),
        ],
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    logical = tuple(
        "std." + Path(path[7:]).with_suffix("").as_posix().replace("/", ".")
        for path in expected
    )
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import json
                import importlib.util
                import os
                from pathlib import Path

                import zlang
                from zlang.compiler import compile_source
                from zlang.stdlib import available_stdlib_modules, resolve_stdlib

                installed = Path(os.environ["ZLANG_INSTALLED_ROOT"]).resolve()
                assert Path(zlang.__file__).resolve().is_relative_to(installed)
                assert importlib.util.find_spec("zlang.backend.clash") is None
                compile_source("module InstalledDirect { out y:u1 y=0 }")
                expected = tuple(json.loads(os.environ["ZLANG_EXPECTED_MODULES"]))
                available = available_stdlib_modules()
                assert available == expected, (available, expected)
                for module in expected:
                    resolved = resolve_stdlib((module,))
                    assert resolved[-1].path == module
                    assert resolved[-1].source_path.is_relative_to(installed / "stdlib")
                print(json.dumps(available))
                """
            ),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(installed),
            "ZLANG_EXPECTED_MODULES": json.dumps(logical),
            "ZLANG_INSTALLED_ROOT": str(installed),
        },
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr

    # Project/lock tooling must work from the wheel as well as the checkout.
    dependency = tmp_path / "wheel-dependency"
    project = tmp_path / "wheel-project"
    (dependency / "src").mkdir(parents=True)
    (project / "src").mkdir(parents=True)
    (dependency / "zlang.toml").write_text(
        'schema=1\n[project]\nname="wheeldep"\nversion="1"\nsource-root="src"\n'
    )
    (dependency / "src" / "id.zhl").write_text(
        "module WheelIdentity { in x:u8 out y:u8 y=x }"
    )
    (project / "zlang.toml").write_text(
        'schema=1\n[project]\nname="wheelapp"\nversion="1"\nsource-root="src"\n'
        '[dependencies]\nwheeldep={path="../wheel-dependency"}\n'
    )
    top = project / "src" / "top.zhl"
    top.write_text(
        "import wheeldep.id module WheelTop { in x:u8 out y:u8 "
        "inst child:WheelIdentity child.x=x y=child.y }"
    )
    installed_environment = {
        **os.environ,
        "PYTHONPATH": str(installed),
    }
    for program in (
        "zlang",
        "zlang-lock",
        "zlang-verify",
    ):
        version_result = subprocess.run(
            [sys.executable, str(installed / "bin" / program), "--version"],
            cwd=tmp_path,
            env=installed_environment,
            capture_output=True,
            text=True,
        )
        assert version_result.returncode == 0, (
            version_result.stdout + version_result.stderr
        )
        assert version_result.stdout == f"{program} 0.1.0a6\n"
    lock_result = subprocess.run(
        [
            sys.executable, "-m", "zlang.project_cli", "update",
            "--project", str(project),
        ],
        cwd=tmp_path,
        env=installed_environment,
        capture_output=True,
        text=True,
    )
    assert lock_result.returncode == 0, lock_result.stdout + lock_result.stderr
    compile_result = subprocess.run(
        [
            sys.executable, "-m", "zlang.cli", str(top),
            "--project", str(project), "--check",
        ],
        cwd=tmp_path,
        env=installed_environment,
        capture_output=True,
        text=True,
    )
    assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr

    # The installed wheel must also publish and replay a self-contained
    # first-class verification bundle.  This exercises the two public module
    # entry points from the installed package rather than the source checkout.
    from zlang.common.tool_inventory import discover_tool_inventory

    tools = discover_tool_inventory(
        ("yosys", "sby", "yosys-smtbmc", "z3")
    )
    if tools.missing:
        pytest.skip("Yosys/SBY/yosys-smtbmc/SMT solver unavailable")
    verification_source = project / "src" / "verify.zhl"
    verification_source.write_text(
        "module WheelVerify { clock clk reset rst in x:u8 out y:u8 y=x "
        "assert passthrough @ clk { y == x } "
        "cover sees_one @ clk { x == 1 } }\n"
    )
    bundle = tmp_path / "wheel-verification-bundle"
    publish = subprocess.run(
        [
            sys.executable,
            "-m",
            "zlang.cli",
            str(verification_source),
            "--verification-bundle",
            str(bundle),
        ],
        cwd=tmp_path,
        env=installed_environment,
        capture_output=True,
        text=True,
    )
    assert publish.returncode == 0, publish.stdout + publish.stderr
    replay = subprocess.run(
        [
            sys.executable,
            "-m",
            "zlang.verification_cli",
            str(bundle),
            "--mode",
            "bmc",
            "--depth",
            "4",
            "--work-dir",
            str(tmp_path / "wheel-verification-work"),
        ],
        cwd=tmp_path,
        env=installed_environment,
        capture_output=True,
        text=True,
    )
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert "bounded_pass" in replay.stdout
    assert "witnessed" in replay.stdout
