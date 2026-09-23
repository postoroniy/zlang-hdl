from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools.stage_release_pdf import ReleasePdfError, stage_release_pdf


def _fixture(tmp_path: Path, payload: bytes = b"%PDF-1.5\nfixture\n") -> tuple[Path, Path]:
    root = tmp_path / "source"
    pdf = root / "docs/reference.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(payload)
    status = root / "release/status.json"
    status.parent.mkdir()
    status.write_text(
        json.dumps(
            {
                "documentation": {
                    "pdf": "docs/reference.pdf",
                    "pdf_sha256": hashlib.sha256(payload).hexdigest(),
                }
            }
        ),
        encoding="utf-8",
    )
    return root, status


def test_stages_exact_reviewed_bytes_under_tagged_name(tmp_path: Path) -> None:
    root, status = _fixture(tmp_path)
    destination = stage_release_pdf(
        root=root,
        status_path=status,
        tag="v0.1.0a14",
        output=tmp_path / "dist",
    )
    assert destination.name == "zlang-hdl-v0.1.0a14-language-reference.pdf"
    assert destination.read_bytes() == (root / "docs/reference.pdf").read_bytes()


@pytest.mark.parametrize("mutation", ("missing", "digest", "signature"))
def test_staging_fails_closed_for_unreviewed_pdf(
    tmp_path: Path, mutation: str
) -> None:
    payload = b"not a PDF" if mutation == "signature" else b"%PDF-1.5\nfixture\n"
    root, status = _fixture(tmp_path, payload)
    pdf = root / "docs/reference.pdf"
    if mutation == "missing":
        pdf.unlink()
    elif mutation == "digest":
        pdf.write_bytes(pdf.read_bytes() + b"changed")
    with pytest.raises(ReleasePdfError):
        stage_release_pdf(
            root=root,
            status_path=status,
            tag="v0.1.0a14",
            output=tmp_path / "dist",
        )
