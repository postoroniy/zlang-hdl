from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def run_cli(tmp_path: Path, source: str, *extra: str) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "source_error.zhl"
    path.write_text(source)
    return subprocess.run(
        [sys.executable, "-m", "zlang.cli", *extra, str(path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_parse_error_is_concise_and_does_not_publish_artifact(tmp_path: Path) -> None:
    output = tmp_path / "bad.sv"
    result = run_cli(
        tmp_path,
        "module Broken { out y:u8 y= }",
        "--systemverilog",
        str(output),
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "zlang: error:" in result.stderr
    assert "Traceback" not in result.stderr
    assert str(ROOT) not in result.stderr
    assert not output.exists()


def test_semantic_and_top_errors_are_concise(tmp_path: Path) -> None:
    semantic = run_cli(tmp_path, "module Broken { out y:u8 y=999 }")
    assert semantic.returncode == 1
    assert "zlang: error:" in semantic.stderr
    assert "Traceback" not in semantic.stderr
    result = run_cli(tmp_path, "module Good { out y:u8 y=0 }", "--top", "Missing")
    assert result.returncode == 1
    assert "zlang: error:" in result.stderr
    assert "Traceback" not in result.stderr
