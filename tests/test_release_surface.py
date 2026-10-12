from __future__ import annotations

import hashlib
from pathlib import Path
import re
import tomllib
from zipfile import ZipFile

import pytest

import zlang
from zlang._version import __version__
from zlang import cli


ROOT = Path(__file__).resolve().parents[1]
APACHE_2_0_CANONICAL_SHA256 = (
    "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"
)
MARKDOWN_LINK = re.compile(r"\[[^]]+\]\(([^)]+)\)")


def test_root_license_is_unmodified_apache_2_0() -> None:
    assert hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest() == (
        APACHE_2_0_CANONICAL_SHA256
    )


def test_public_cli_reports_the_distribution_version(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["--version"])

    assert raised.value.code == 0
    assert capsys.readouterr().out == f"zlang {__version__}\n"


def test_package_and_build_metadata_share_one_version_source() -> None:
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert zlang.__version__ == __version__ == "0.1.0a23"
    assert configuration["project"]["dynamic"] == ["version"]
    assert configuration["project"]["license"] == "Apache-2.0"
    assert configuration["project"]["requires-python"] == ">=3.12,<3.13"
    assert configuration["project"]["name"] == "zlang-hdl"
    assert configuration["project"]["scripts"]["zlang"] == "zlang.cli:main"
    assert "zlangc" not in configuration["project"]["scripts"]
    assert configuration["build-system"] == {
        "requires": ["setuptools==84.0.0"],
        "build-backend": "setuptools.build_meta",
    }
    assert configuration["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "zlang._version.__version__"
    }
    assert "tool" not in configuration or "maturin" not in configuration["tool"]
    native = configuration["project"]["optional-dependencies"]["native"]
    assert native == [
        "zlang-native-sim==0.1.0a23; "
        "platform_system == 'Linux' and platform_machine == 'x86_64'",
    ]


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
    assert sources


def test_native_wheel_metadata_matches_the_current_release() -> None:
    wheels = tuple((ROOT / "release" / "native-wheels").glob("*.whl"))
    assert len(wheels) == 1
    with ZipFile(wheels[0]) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = archive.read(metadata_name).decode("utf-8")

    assert "Version: 0.1.0a23\n" in metadata
    assert set(re.findall(r"0\.1\.0a\d+", metadata)) == {"0.1.0a23"}


def test_user_facing_release_files_do_not_embed_host_paths() -> None:
    paths = (
        ROOT / "README.md",
        ROOT / "AI_GUIDE.md",
        ROOT / "CHANGELOG.md",
        ROOT / "Makefile",
        *(ROOT / "docs").rglob("*.md"),
        *(ROOT / "examples").rglob("*.md"),
        *(ROOT / ".github" / "workflows").glob("*.y*ml"),
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "/home/" not in text, path
        assert "/Users/" not in text, path


@pytest.mark.parametrize(
    "relative",
    ("README.md", "AI_GUIDE.md", "docs/release-process.md"),
)
def test_primary_documentation_has_no_broken_relative_links(relative: str) -> None:
    document = ROOT / relative
    for raw_target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
        target = raw_target.strip().split("#", 1)[0]
        if not target or "://" in target or target.startswith(("#", "mailto:")):
            continue
        assert (document.parent / target).resolve().exists(), (
            f"{relative} links to missing path {target!r}"
        )
