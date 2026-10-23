from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from tools.documentation_manifest import MANIFEST, PDF_NAMES, load_manuals
from tools.stage_release_pdf import ReleasePdfError, stage_release_pdfs

ROOT = Path(__file__).resolve().parents[2]


def fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "source"
    status = json.loads((ROOT / "release/status.json").read_text())
    docs = status["documentation"]
    paths = {MANIFEST, Path("editors/vscode/zlang-hdl/syntaxes/zlang.tmLanguage.json"),
             Path("release/status.json"), Path(".vscode/launch.json"),
             Path("docs/stdlib-formal-coverage.md"), Path("docs/reference/04-simulation-verification.md"),
             *(Path(p) for p in docs["build_sources"])}
    for m in load_manuals(ROOT):
        paths.update((m.pdf, m.entrypoint, *m.sources))
    for path in paths:
        if path == Path("tools/build_community_pdf.py") and not (ROOT / path).is_file():
            continue  # Reviewed prebuilt manuals ship without the host-only renderer.
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, destination)
    shutil.copytree(ROOT / "stdlib", root / "stdlib")
    shutil.copytree(ROOT / "examples", root / "examples")
    return root, root / "release/status.json"


def test_stages_exact_complete_set_under_stable_names(tmp_path: Path) -> None:
    root, status = fixture(tmp_path)
    destinations = stage_release_pdfs(root=root, status_path=status, tag="v0.1.0a24", output=tmp_path / "dist")
    assert tuple(p.name for p in destinations) == PDF_NAMES
    for destination in destinations:
        assert destination.read_bytes() == (root / "docs" / destination.name).read_bytes()


def test_prebuilt_set_stages_without_host_renderer(tmp_path: Path) -> None:
    root, status = fixture(tmp_path)
    (root / "tools/build_community_pdf.py").unlink(missing_ok=True)
    destinations = stage_release_pdfs(root=root, status_path=status, tag="v0.1.0a24", output=tmp_path / "dist")
    assert tuple(p.name for p in destinations) == PDF_NAMES


@pytest.mark.parametrize("name", ("tools/documentation_sources.py", "tools/documentation_pdf.py"))
def test_missing_public_recipe_owner_fails_closed(tmp_path: Path, name: str) -> None:
    root, status = fixture(tmp_path)
    (root / name).unlink()
    with pytest.raises(ReleasePdfError):
        stage_release_pdfs(root=root, status_path=status, tag="v0.1.0a24", output=tmp_path / "dist")
    assert not (tmp_path / "dist").exists()


@pytest.mark.parametrize("book", ("language-reference", "standard-library", "user-guide"))
@pytest.mark.parametrize("mutation", ("missing", "digest", "signature", "source", "mapping", "record"))
def test_staging_fails_closed_without_partial_output(tmp_path, book, mutation) -> None:
    root, status_path = fixture(tmp_path)
    data = json.loads(status_path.read_text())
    record = data["documentation"]["manuals"][book]
    pdf = root / record["pdf"]
    if mutation == "missing":
        pdf.unlink()
    elif mutation == "digest":
        pdf.write_bytes(pdf.read_bytes() + b"changed")
    elif mutation == "signature":
        pdf.write_bytes(b"not a PDF")
    elif mutation == "source":
        path = root / next(iter(record["sources"]))
        path.write_text(path.read_text() + "\nChanged source\n")
    elif mutation == "mapping":
        record["pdf"] = "docs/ZLang-HDL-Other.pdf"
    else:
        del data["documentation"]["manuals"][book]
    status_path.write_text(json.dumps(data))
    with pytest.raises(ReleasePdfError):
        stage_release_pdfs(root=root, status_path=status_path, tag="v0.1.0a24", output=tmp_path / "dist")
    assert not (tmp_path / "dist").exists()


@pytest.mark.parametrize("mutation", ("manifest", "schema", "builder", "tag"))
def test_staging_rejects_changed_recipe_and_wrong_release(tmp_path, mutation) -> None:
    root, status_path = fixture(tmp_path)
    data = json.loads(status_path.read_text())
    tag = "v0.1.0a24"
    if mutation == "manifest":
        path = root / MANIFEST
        path.write_text(path.read_text() + "\n# changed manifest\n")
    elif mutation == "schema":
        data["schema"] = 2
    elif mutation == "builder":
        (root / "tools/build_community_pdf.py").write_text("# changed builder")
    else:
        tag = "v0.1.0a23"
    status_path.write_text(json.dumps(data))
    with pytest.raises(ReleasePdfError):
        stage_release_pdfs(root=root, status_path=status_path, tag=tag, output=tmp_path / "dist")


@pytest.mark.parametrize("name", ["old-reference.pdf", "fourth-book.PDF"])
def test_staging_rejects_extra_pdf_assets(tmp_path, name) -> None:
    root, status = fixture(tmp_path)
    output = tmp_path / "dist"
    output.mkdir()
    (output / name).write_bytes(b"old asset")
    with pytest.raises(ReleasePdfError, match="unexpected PDF"):
        stage_release_pdfs(root=root, status_path=status, tag="v0.1.0a24", output=output)
    assert tuple(path.name for path in output.iterdir()) == (name,)
