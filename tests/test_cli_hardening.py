"""Failure-boundary regressions shared by the installed CLI utilities."""

from __future__ import annotations

from pathlib import Path

import pytest

from zlang import cli_command
from zlang.cli import main
from zlang.verification_cli import main as verification_main


def _source(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "design.zhl"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("option", "message"),
    (
        ("--formal-depth", "--formal-depth must be positive"),
        ("--formal-max-candidates", "--formal-max-candidates must be positive"),
        ("--formal-timeout", "--formal-timeout must be positive"),
    ),
)
def test_compile_rejects_invalid_formal_bounds_before_compilation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    option: str,
    message: str,
) -> None:
    source = _source(tmp_path, "module Good { out y:u8 y=0 }")
    with pytest.raises(SystemExit) as raised:
        main([str(source), "--check", option, "0"])
    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert message in error
    assert "Traceback" not in error


def test_failed_verilator_launch_does_not_publish_final_rtl(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _source(tmp_path, "module Good { out y:u8 y=0 }")
    output = tmp_path / "Good.sv"
    missing = tmp_path / "missing-verilator"

    with pytest.raises(SystemExit) as raised:
        main([
            str(source),
            "--systemverilog",
            str(output),
            "--verilator-lint",
            "--verilator",
            str(missing),
        ])
    assert raised.value.code == 2
    assert "cannot execute Verilator" in capsys.readouterr().err
    assert not output.exists()


def test_publication_io_error_is_concise(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path, "module Good { out y:u8 y=0 }")
    output = tmp_path / "Good.sv"

    def fail_publication(path: Path, content: str) -> None:
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(
        "zlang.cli_publication.publish_text_atomically", fail_publication
    )
    assert main([str(source), "--systemverilog", str(output)]) == 2
    error = capsys.readouterr().err
    assert "error[ZL-IO-001]" in error
    assert "cannot publish requested artifacts" in error
    assert "Traceback" not in error


@pytest.mark.parametrize(
    ("text", "location", "message"),
    (
        (
            "module Bad { out y:u8 y= }",
            "1:26",
            "error[ZL-PARSE-001]",
        ),
        (
            "module Bad { out y:u8 y=missing }",
            "1:25",
            "error[ZL-SEMANTIC-001]",
        ),
    ),
)
def test_sim_source_diagnostics_retain_file_and_span(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    text: str,
    location: str,
    message: str,
) -> None:
    source = _source(tmp_path, text)
    assert main(["sim", str(source), "--json"]) == 1
    error = capsys.readouterr().err
    assert error.startswith(f"zlang sim: {source}:{location}: {message}")
    assert "Traceback" not in error


def test_implicit_verification_work_directory_is_cleaned_on_source_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path, "module Bad { out y:u8 y= }")

    class TemporaryOwner:
        name = str(tmp_path / "verification-work")
        cleaned = False

        def __enter__(self) -> str:
            return self.name

        def __exit__(self, *_error: object) -> None:
            self.cleanup()

        def cleanup(self) -> None:
            self.cleaned = True

    owner = TemporaryOwner()
    monkeypatch.setattr(
        cli_command.tempfile,
        "TemporaryDirectory",
        lambda **_kwargs: owner,
    )
    assert main([str(source), "--verify"]) == 1
    assert owner.cleaned


def test_compiler_rejects_overlapping_verification_directories(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _source(tmp_path, "module Good { out y:u8 y=0 }")
    bundle = tmp_path / "bundle"
    with pytest.raises(SystemExit) as raised:
        main([
            str(source),
            "--verify",
            "--verification-bundle",
            str(bundle),
            "--verification-work-dir",
            str(bundle),
        ])
    assert raised.value.code == 2
    assert "overlap" in capsys.readouterr().err
    assert not bundle.exists()


def test_standalone_verifier_validates_bounds_before_loading_bundle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "missing-bundle"
    with pytest.raises(SystemExit) as raised:
        verification_main([str(missing), "--jobs", "0"])
    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert "--jobs must be positive" in error
    assert "unavailable" not in error


def test_standalone_verifier_rejects_report_inside_bundle_before_loading(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "bundle"
    with pytest.raises(SystemExit) as raised:
        verification_main([
            str(missing),
            "--report",
            str(missing / "report.json"),
        ])
    assert raised.value.code == 2
    assert "outside the immutable bundle" in capsys.readouterr().err


def test_logic_assignment_error_names_both_enabling_modes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _source(tmp_path, "module Good { in x:u8 out y:u8 y=x }")
    with pytest.raises(SystemExit) as raised:
        main(["sim", str(source), "--set-logic", "x=00000000"])
    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert "--logic-state or --strict-uninitialized" in error
