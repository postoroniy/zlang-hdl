from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from zlang.workspace import update_project_lock


ROOT = Path(__file__).resolve().parents[2]


def _run(source: Path, diagnostic_format: str = "text", *extra: str):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "zlang.cli",
            "--diagnostic-format",
            diagnostic_format,
            *extra,
            str(source),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_json_parse_diagnostic_is_one_stable_object(tmp_path: Path) -> None:
    source = tmp_path / "bad.zhl"
    source.write_text("module Bad { out y:u8 y= }")
    result = _run(source, "json")

    assert result.returncode == 1
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload == {
        "schema": "zlang-diagnostic-v1",
        "severity": "error",
        "code": "ZL-PARSE-001",
        "message": payload["message"],
        "primary": {
            "construct": "syntax error",
            "digest": None,
            "source_unit": None,
            "span": {
                "start_line": 1,
                "start_column": 26,
                "end_line": 1,
                "end_column": 27,
            },
        },
        "notes": [],
        "fixes": [],
    }
    assert payload["message"].startswith("syntax error at line 1, column")


def test_json_semantic_diagnostic_carries_code_origin_and_fix(tmp_path: Path) -> None:
    source = tmp_path / "bad_width.zhl"
    source.write_text("module Bad { in a:u8 out y:u8 y=a+a }")
    result = _run(source, "json")

    assert result.returncode == 1
    payload = json.loads(result.stderr)
    assert payload["code"] == "ZL-WIDTH-ASSIGNMENT"
    assert payload["message"] == "cannot assign u9 expression to u8 output 'y'"
    assert payload["primary"]["construct"] == "operator +"
    assert payload["fixes"] == ["use an explicit exact-width conversion"]


def test_text_format_default_and_explicit_are_byte_compatible(tmp_path: Path) -> None:
    source = tmp_path / "bad_width.zhl"
    source.write_text("module Bad { in a:u8 out y:u8 y=a+a }")
    default = subprocess.run(
        [sys.executable, "-m", "zlang.cli", str(source)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    explicit = _run(source, "text")

    assert explicit.returncode == default.returncode == 1
    assert explicit.stdout == default.stdout == ""
    assert explicit.stderr == default.stderr
    assert explicit.stderr == (
        f"zlang: {source}:1:33: error[ZL-WIDTH-ASSIGNMENT]: "
        "cannot assign u9 expression to u8 output 'y'\n"
    )


def test_text_parse_diagnostic_reports_physical_file_line_and_column(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bad_parse.zhl"
    source.write_text("module Bad {\n  out y:u8\n  y =\n}\n")

    result = _run(source)

    assert result.returncode == 1
    assert result.stderr.startswith(
        f"zlang: {source}:4:1: error[ZL-PARSE-001]: syntax error at line 4, column 1:"
    )


def test_transitive_project_diagnostic_reports_imported_file_and_exact_span(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    sources = project / "src"
    sources.mkdir(parents=True)
    (project / "zlang.toml").write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n'
    )
    leaf = sources / "leaf.zhl"
    leaf.write_text(
        "module Leaf {\n"
        "  in a:u8\n"
        "  out y:u8\n"
        "  y = missing\n"
        "}\n"
    )
    helper = sources / "helper.zhl"
    helper.write_text(
        "import demo.leaf\n"
        "module Helper {\n"
        "  in a:u8\n"
        "  out y:u8\n"
        "  child : Leaf\n"
        "  a -> child.a\n"
        "  child.y -> y\n"
        "}\n"
    )
    top = sources / "top.zhl"
    top.write_text(
        "import demo.helper\n"
        "module Top {\n"
        "  in a:u8\n"
        "  out y:u8\n"
        "  child : Helper\n"
        "  a -> child.a\n"
        "  child.y -> y\n"
        "}\n"
    )
    update_project_lock(project / "zlang.toml")

    text = _run(top)
    structured = _run(top, "json")

    assert text.returncode == structured.returncode == 1
    assert text.stderr == (
        f"zlang: {leaf}:4:7: error[ZL-SEMANTIC-001]: "
        "unknown input 'missing'\n"
    )
    payload = json.loads(structured.stderr)
    assert payload["code"] == "ZL-SEMANTIC-001"
    assert payload["primary"]["source_unit"] == "demo.leaf"
    assert payload["primary"]["span"] == {
        "start_line": 4,
        "start_column": 7,
        "end_line": 4,
        "end_column": 14,
    }


def test_json_top_selection_and_io_errors_have_stable_codes(tmp_path: Path) -> None:
    source = tmp_path / "good.zhl"
    source.write_text("module Good { out y:u8 y=0 }")
    top = _run(source, "json", "--top", "Missing")
    assert top.returncode == 1
    assert json.loads(top.stderr)["code"] == "ZL-TOP-001"

    missing = _run(tmp_path / "missing.zhl", "json")
    assert missing.returncode == 1
    payload = json.loads(missing.stderr)
    assert payload["code"] == "ZL-IO-001"
    assert payload["message"].startswith("cannot read source file 'missing.zhl':")


def test_legacy_source_suffixes_are_rejected_before_file_io(tmp_path: Path) -> None:
    for suffix in (".zl", ".zlang"):
        source = tmp_path / f"legacy{suffix}"
        source.write_text("module Legacy { out y:u8 y=0 }")

        result = _run(source, "json")

        assert result.returncode == 1
        assert result.stdout == ""
        payload = json.loads(result.stderr)
        assert payload["code"] == "ZL-SOURCE-EXTENSION"
        assert "rename the file to 'legacy.zhl'" in payload["message"]
        assert payload["fixes"] == ["rename the source file to use '.zhl'"]

        missing = _run(tmp_path / f"also-legacy{suffix}", "json")
        assert json.loads(missing.stderr)["code"] == "ZL-SOURCE-EXTENSION"
