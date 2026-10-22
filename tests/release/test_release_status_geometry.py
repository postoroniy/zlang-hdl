from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
RELEASE_STATUS = ROOT / "tools" / "release_status.py"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("cover_width", 1055, "cover_width"),
        ("cover_height", 1491, "cover_height"),
        ("page_width_points", 600.0, "page geometry mismatch"),
        ("page_height_points", 800.0, "page geometry mismatch"),
    ),
)
def test_release_status_rejects_stale_pdf_geometry(
    tmp_path: Path, field: str, value: int | float, message: str
) -> None:
    status = json.loads((ROOT / "release/status.json").read_text(encoding="utf-8"))
    # Synthetic metadata for this geometry-only CLI mutation. The real
    # checked-in source/PDF freshness gate remains unmodified and fail-closed.
    for key, relative in (
        ("builder_sha256", "tools/build_community_pdf.py"),
        ("syntax_grammar_sha256", "editors/vscode/zlang-hdl/syntaxes/zlang.tmLanguage.json"),
    ):
        path = ROOT / relative
        if key == "builder_sha256" and not path.exists():
            # The release-host-only builder is absent from public exports,
            # matching release_status's existing source-availability policy.
            continue
        status["documentation"][key] = hashlib.sha256(path.read_bytes()).hexdigest()
    status["documentation"][field] = value
    stale = tmp_path / "status.json"
    stale.write_text(json.dumps(status), encoding="utf-8")

    completed = subprocess.run(
        (
            sys.executable,
            str(RELEASE_STATUS),
            "check",
            "--root",
            str(ROOT),
            "--status",
            str(stale),
        ),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert message in completed.stderr
