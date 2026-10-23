from __future__ import annotations

from pathlib import Path
import re

import pytest

from tools.documentation_manifest import DocumentationError, MANIFEST, load_manuals
from tools.documentation_sources import _headings, _resolve, assemble, destinations, validate_markdown_links


ROOT = Path(__file__).resolve().parents[2]


def inventory(tmp_path: Path) -> Path:
    books = load_manuals(ROOT)
    for relative in (MANIFEST, *(p for m in books for p in (m.entrypoint, *m.sources))):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / relative).read_bytes())
    return tmp_path


def test_exact_three_books_and_single_chapter_ownership() -> None:
    books = load_manuals(ROOT)
    assert tuple(m.identity for m in books) == ("language-reference", "standard-library", "user-guide")
    sources = [p for m in books for p in m.sources]
    assert len(sources) == len(set(sources))
    assert all(m.entrypoint not in sources for m in books)
    assert validate_markdown_links(ROOT) > 500


@pytest.mark.parametrize(("before", "after"), [
    ('schema = 1', 'schema = 2'),
    ('schema = 1', 'schema = true'),
    ('id = "user-guide"', 'id = "fourth-book"'),
    ('docs/ZLang-HDL-User-Guide.pdf', 'docs/ZLang-HDL-Language-Reference.pdf'),
    ('docs/tutorial.md', 'docs/types-and-numerics.md'),
    ('docs/tutorial.md', 'docs/internal/secret.md'),
    ('docs/tutorial.md', 'docs/../../secret.md'),
    ('docs/tutorial.md', '/absolute/secret.md'),
    ('docs/tutorial.md', 'docs/missing.md'),
])
def test_manifest_rejects_wrong_book_mapping_schema_duplicate_and_unsafe_sources(tmp_path, before, after) -> None:
    inventory(tmp_path)
    manifest = tmp_path / MANIFEST
    manifest.write_text(manifest.read_text().replace(before, after))
    with pytest.raises(DocumentationError):
        load_manuals(tmp_path)


def test_manifest_rejects_symlink_escape(tmp_path) -> None:
    inventory(tmp_path)
    path = tmp_path / "docs/tutorial.md"
    path.unlink()
    path.symlink_to(ROOT / "docs/tutorial.md")
    with pytest.raises(DocumentationError, match="escapes"):
        load_manuals(tmp_path)


def test_missing_anchor_and_missing_file_fail_closed(tmp_path) -> None:
    inventory(tmp_path)
    chapter = tmp_path / "docs/tutorial.md"
    for bad in ("types-and-numerics.md#nonexistent", "missing-file.md"):
        chapter.write_text(f"# Example\n\n[Broken]({bad})\n")
        with pytest.raises(DocumentationError, match="anchor|broken"):
            assemble(tmp_path, load_manuals(tmp_path)[2])


@pytest.mark.parametrize("target", ["javascript:alert", "file:///local/path", "run:program"])
def test_unsafe_link_schemes_are_rejected(tmp_path, target) -> None:
    inventory(tmp_path)
    (tmp_path / "docs/tutorial.md").write_text(f"# Example\n\n[Unsafe]({target})\n")
    with pytest.raises(DocumentationError, match="unsafe manual link"):
        assemble(tmp_path, load_manuals(tmp_path)[2])


def test_only_builder_can_defer_its_manifest_owned_pdf_outputs(tmp_path) -> None:
    inventory(tmp_path)
    for book in load_manuals(tmp_path):
        (tmp_path / book.entrypoint).write_text(f"# Book\n\n[PDF]({book.pdf.name})\n")
        for path in book.sources:
            (tmp_path / path).write_text("# Chapter\n")
    with pytest.raises(DocumentationError, match="broken manual link"):
        validate_markdown_links(tmp_path)
    assert validate_markdown_links(tmp_path, allow_missing_pdfs=True) == 3


def test_old_reference_anchors_route_to_canonical_owners() -> None:
    entrypoint = (ROOT / "docs/language-reference.md").read_text()
    aliases = re.findall(r'<a id="(reference-[^"]+)"></a>', entrypoint)
    routes = destinations(ROOT)
    assert len(aliases) == 185  # 179 monolith aliases plus six previous PDF-only roots.
    assert len(aliases) == len(set(aliases))
    for alias in aliases:
        assert ("docs/language-reference.md", alias) in routes


def test_legacy_navigation_and_authoring_entry_links_still_resolve() -> None:
    paths = [ROOT / path for path in ("README.md", "AI_GUIDE.md", "docs/README.md", "docs/language-guide.md")]
    paths.extend((ROOT / "docs/reference").glob("*.md"))
    paths.append(ROOT / ".qwen/skills/zlang-hdl/SKILL.md")
    for source in paths:
        text = re.sub(r"(?m)^```[^\n]*\n.*?^```\s*$", "", source.read_text(), flags=re.DOTALL)
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            resolved = _resolve(source.relative_to(ROOT), target)
            if resolved is None:
                continue
            name, fragment = resolved
            destination = ROOT / name
            assert destination.exists(), (source, target)
            if fragment and destination.suffix == ".md":
                other = destination.read_text()
                anchors = {name for _, _, _, names in _headings(other) for name in names}
                anchors.update(re.findall(r'<a id="([^"]+)"></a>', other))
                assert fragment in anchors, (source, target)


def test_book_links_are_offline_siblings_not_moving_main() -> None:
    for book in load_manuals(ROOT):
        text = assemble(ROOT, book)
        assert "/blob/main/" not in text
        assert "javascript:" not in text.lower()
        cross = re.findall(r"\]\((ZLang-HDL-[^)]+\.pdf)#[^)]+\)", text)
        assert cross


def test_public_guides_keep_contracts_not_internal_review_procedures() -> None:
    release = (ROOT / "docs/release-process.md").read_text()
    for excluded in (
        "release-subsystem-map.md", "RELEASING.md", "private integration",
        "private handoff", "codex/community-", "## Repository and worktree roles",
    ):
        assert excluded not in release
    for gate in (
        "make review-commits", "make release-review", "make release-candidate",
        "release/regressions.json", "release/status.json", "origin/main",
        "two independent zero-unexpected-skip suites", "signed annotated tag",
        "community-pdf-check", "JavaScript and Launch actions are forbidden",
    ):
        assert gate in release
    tooling = (ROOT / "docs/tooling-integration-api.md").read_text()
    assert "Compiler-owned diagnostic edit projection." not in tooling
    assert "The generated-RTL audit" not in tooling
    assert "load_generated_navigation_bundle()" in tooling
    assert "exposes no generated-location query yet" in tooling
    optimization = (ROOT / "docs/optimization-formal.md").read_text().split(
        '<a id="reference-egraph-optimization-infrastructure"></a>', 1
    )[1]
    for internal in ("ScheduledValueGraph", "OperationCostModel", "ResourceMatcher", "ExpressionOp"):
        assert internal not in optimization
    for contract in ("Widths", "signedness", "rounding", "required_proven", "bounded_pass"):
        assert contract in optimization
