from __future__ import annotations

from collections.abc import Sequence

import pytest

from zlang import cli
from zlang._version import __version__


def _help(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> str:
    with pytest.raises(SystemExit) as raised:
        cli.main(argv)
    assert raised.value.code == 0
    return capsys.readouterr().out


def test_primary_help_discovers_every_installed_utility(
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = _help(["--help"], capsys)
    assert "zlang sim SOURCE" in output
    assert "zlang verify BUNDLE" in output
    assert "zlang lock update" in output
    assert "zlang lsp" in output


@pytest.mark.parametrize(
    ("command", "usage"),
    (
        ("verify", "usage: zlang verify"),
        ("lock", "usage: zlang lock"),
        ("lsp", "usage: zlang lsp"),
    ),
)
def test_utility_help_uses_the_umbrella_command_name(
    command: str,
    usage: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert usage in _help([command, "--help"], capsys)


@pytest.mark.parametrize("command", ("verify", "lock", "lsp"))
def test_utility_versions_use_the_umbrella_command_name(
    command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main([command, "--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out == f"zlang {command} {__version__}\n"


@pytest.mark.parametrize(
    ("command", "module_name"),
    (
        ("verify", "zlang.verification_cli"),
        ("lock", "zlang.project_cli"),
        ("lsp", "zlang.lsp.server"),
    ),
)
def test_umbrella_router_forwards_arguments_without_reparsing(
    command: str,
    module_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str] | None, str]] = []

    def fake_main(
        argv: Sequence[str] | None = None,
        *,
        prog: str,
    ) -> int:
        calls.append((None if argv is None else list(argv), prog))
        return 17

    monkeypatch.setattr(f"{module_name}.main", fake_main)
    assert cli.main([command, "payload", "--flag"]) == 17
    assert calls == [(["payload", "--flag"], f"zlang {command}")]


def test_simulation_router_remains_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str] | None] = []

    def fake_main(argv: Sequence[str] | None = None) -> int:
        calls.append(None if argv is None else list(argv))
        return 19

    monkeypatch.setattr("zlang.sim_cli.main", fake_main)
    assert cli.main(["sim", "design.zhl", "--json"]) == 19
    assert calls == [["design.zhl", "--json"]]
