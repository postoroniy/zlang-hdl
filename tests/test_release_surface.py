from __future__ import annotations

from pathlib import Path
import tomllib

import pytest

import zlang
from zlang._version import __version__
from zlang import backend_comparison, cli, project_cli, verification_cli
from zlang import toolchain


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("entrypoint", "program"),
    (
        (cli.main, "zlang"),
        (project_cli.main, "zlang-lock"),
        (backend_comparison.main, "zlang-compare-backends"),
        (verification_cli.main, "zlang-verify"),
    ),
)
def test_every_public_cli_reports_the_distribution_version(
    entrypoint,
    program: str,
    capsys,
) -> None:
    with pytest.raises(SystemExit) as raised:
        entrypoint(["--version"])

    assert raised.value.code == 0
    assert capsys.readouterr().out == f"{program} {__version__}\n"


def test_package_and_build_metadata_share_one_version_source() -> None:
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert zlang.__version__ == __version__ == "0.1.0a1"
    assert configuration["project"]["dynamic"] == ["version"]
    assert configuration["project"]["license"] == "Apache-2.0"
    assert configuration["project"]["requires-python"] == ">=3.12,<3.13"
    assert configuration["project"]["name"] == "zlang-hdl"
    assert configuration["project"]["scripts"]["zlang"] == "zlang.cli:main"
    assert "zlangc" not in configuration["project"]["scripts"]
    assert configuration["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "zlang._version.__version__"
    }


def test_public_language_identity_is_unambiguous() -> None:
    assert zlang.PUBLIC_LANGUAGE_NAME == "ZLang HDL"
    assert zlang.DISTRIBUTION_NAME == "zlang-hdl"
    assert zlang.SOURCE_SUFFIX == ".zhl"
    assert zlang.CLI_NAME == "zlang"
    assert zlang.VSCODE_LANGUAGE_ID == "zlang-hdl"
    assert zlang.MIME_TYPE == "text/x-zlang-hdl"


def test_every_owned_hardware_source_uses_the_canonical_suffix() -> None:
    roots = (
        ROOT / "stdlib",
        ROOT / "examples",
        ROOT / "tests" / "fixtures",
        ROOT / "docs" / "reproducers",
        ROOT / "editors" / "vscode" / "zlang-hdl" / "examples",
    )
    legacy = tuple(path for root in roots for path in root.rglob("*.zl"))
    sources = tuple(path for root in roots for path in root.rglob("*.zhl"))

    assert legacy == ()
    assert len(sources) == 122


def test_clash_discovery_has_no_machine_specific_fallback(monkeypatch) -> None:
    monkeypatch.delenv("ZLANG_CLASH", raising=False)
    monkeypatch.delenv("ZLANG_CLASH_ROOT", raising=False)
    monkeypatch.setattr(toolchain.shutil, "which", lambda _name: None)

    assert toolchain.find_clash_executable() is None


def test_clash_checkout_environment_is_inferred_from_selected_executable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkout = tmp_path / "clash-compiler"
    executable = (
        checkout
        / "dist-newstyle/build/x86_64-linux/ghc-9.10.3/"
        "clash-ghc-1.11.0/x/clash/build/clash/clash"
    )
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    package_environment = checkout / ".ghc.environment.x86_64-linux-9.10.3"
    package_environment.write_text("clear-package-db\n")
    (checkout / "clash-lib").mkdir()
    monkeypatch.setenv("ZLANG_CLASH", str(executable))
    monkeypatch.delenv("ZLANG_CLASH_ROOT", raising=False)

    assert toolchain.find_clash_executable() == str(executable)
    environment = toolchain.clash_subprocess_environment(str(executable))
    assert environment["GHC_ENVIRONMENT"] == str(package_environment)
    assert environment["clash_lib_datadir"] == str(checkout / "clash-lib")


def test_clash_discovery_tracks_configuration_changes_without_process_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first = tmp_path / "first-clash"
    second = tmp_path / "second-clash"
    for executable in (first, second):
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)

    monkeypatch.setenv("ZLANG_CLASH", str(first))
    assert toolchain.find_clash_executable() == str(first)

    monkeypatch.setenv("ZLANG_CLASH", str(second))
    assert toolchain.find_clash_executable() == str(second)
