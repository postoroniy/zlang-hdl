from __future__ import annotations

import json
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
