from __future__ import annotations

import hashlib
from pathlib import Path
import tomllib

import pytest

import zlang
from zlang._version import __version__
from zlang import cli, project_cli, verification_cli
from zlang import toolchain


ROOT = Path(__file__).resolve().parents[1]
APACHE_2_0_CANONICAL_SHA256 = (
    "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"
)


def test_root_license_is_unmodified_apache_2_0() -> None:
    assert hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest() == (
        APACHE_2_0_CANONICAL_SHA256
    )


@pytest.mark.parametrize(
    ("entrypoint", "program"),
    (
        (cli.main, "zlang"),
        (project_cli.main, "zlang-lock"),
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

    assert zlang.__version__ == __version__ == "0.1.0a6"
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
    assert len(sources) == 134
