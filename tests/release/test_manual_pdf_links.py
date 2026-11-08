from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

import pytest

from tools.documentation_manifest import DocumentationError, load_manuals
from tools.documentation_pdf import validate_actions, validate_text_layout


NAMES = {"ZLang-HDL-Language-Reference.pdf": {"language", "typed-values"},
         "ZLang-HDL-User-Guide.pdf": {"user", "debugger"}}
CURRENT = "ZLang-HDL-Language-Reference.pdf"
ROOT = Path(__file__).resolve().parents[2]


def test_standard_local_and_remote_named_actions_are_allowed() -> None:
    validate_actions({"objects": [
        {"/S": "/GoTo", "/D": "u:typed-values"},
        {"/S": "/GoToR", "/F": "u:ZLang-HDL-User-Guide.pdf", "/D": "u:debugger"},
        {"/S": "/URI", "/URI": "u:https://example.org/guide"},
    ]}, CURRENT, NAMES)


@pytest.mark.parametrize("action", [
    {"/S": "/Launch", "/F": "u:program"},
    {"/S": "/JavaScript", "/JS": "u:alert(1)"},
    {"/Names": {"/JavaScript": []}},
    {"/S": "/URI", "/URI": "u:javascript:alert(1)"},
    {"/S": "/URI", "/URI": "u:file:///local/file"},
    {"/S": "/URI", "/URI": "u:missing.md"},
    {"/S": "/GoToR", "/F": "u:../ZLang-HDL-User-Guide.pdf", "/D": "u:debugger"},
    {"/S": "/GoToR", "/F": "u:ZLang-HDL-Fourth-Book.pdf", "/D": "u:debugger"},
    {"/S": "/GoToR", "/F": "u:ZLang-HDL-User-Guide.pdf", "/D": "u:missing"},
    {"/S": "/GoToR", "/F": "u:ZLang-HDL-User-Guide.pdf", "/D": [0]},
    {"/S": "/GoTo", "/D": "u:missing"},
])
def test_active_nonportable_and_unresolved_actions_fail_closed(action) -> None:
    with pytest.raises(DocumentationError):
        validate_actions({"value": action}, CURRENT, NAMES)


@pytest.mark.parametrize("text", [
    "Previous page.\f\n. Their declarations enforce the bound.\n",
    "Scalar architecture(auto)\n, pipeline(auto) is retired.\n",
    "Pass -\n-verification-work-dir DIR\n",
    "where -\n-formal-policy executes the check.\n",
    "The result is in 0.\n.D-1.\n",
])
def test_rendered_manual_rejects_detached_punctuation_and_split_tokens(text) -> None:
    with pytest.raises(DocumentationError, match="manual PDF page"):
        validate_text_layout(text)


def test_rendered_manual_allows_member_access_paths_and_ellipsis() -> None:
    validate_text_layout(
        ".transfer events are read-only.\n.underflow is an observation.\n"
        "../examples/counter.zhl\n.venv/bin/python\n... remains a syntax placeholder.\n"
        "Use --formal-policy required_proven.\nThe bound is 0 <= r < D.\n"
    )


SOURCE = "https://github.com/postoroniy/zlang-hdl/blob/v0.1.0a24/examples/counter.zhl"


def test_pdf_source_actions_match_exact_release_bound_inventory() -> None:
    from tools.documentation_pdf import validate_source_actions
    data = {"objects": [{"/S": "/URI", "/URI": "u:" + SOURCE}]}
    validate_source_actions(data, {SOURCE})


@pytest.mark.parametrize("uri", [
    SOURCE.replace("v0.1.0a24", "3b083ca"),
    SOURCE.replace("v0.1.0a24", "main"),
    SOURCE.replace("counter.zhl", "other.zhl"),
    SOURCE.replace("postoroniy/zlang-hdl", "other/repo"),
    None,
])
def test_pdf_stale_altered_or_missing_source_url_is_rejected(uri) -> None:
    from tools.documentation_pdf import validate_source_actions
    data = {"objects": [] if uri is None else [{"/S": "/URI", "/URI": "u:" + uri}]}
    with pytest.raises(DocumentationError, match="source links differ"):
        validate_source_actions(data, {SOURCE})


@pytest.mark.parametrize("manual", load_manuals(ROOT), ids=lambda manual: manual.identity)
def test_major_chapters_start_new_pages_without_forcing_subchapter_breaks(manual) -> None:
    pdf = ROOT / manual.pdf
    outlines = json.loads(subprocess.run(
        ["qpdf", "--json", "--json-key=outlines", str(pdf)],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout)["outlines"]
    assert outlines[-1]["title"] == "Font license notice"
    chapters = outlines[:-1]
    assert chapters
    chapter_pages = [chapter["destpageposfrom1"] for chapter in chapters]
    assert chapter_pages == sorted(set(chapter_pages))
    destinations = subprocess.run(
        ["pdfinfo", "-dests", str(pdf)],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout
    book_destination = re.search(
        rf'(?m)^\s*(\d+)\s+\[.*\]\s+"{re.escape(manual.identity)}"$', destinations,
    )
    assert book_destination and int(book_destination.group(1)) == chapter_pages[0]
    document = ET.fromstring(subprocess.run(
        ["pdftotext", "-bbox-layout", str(pdf), "-"],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout)
    namespace = {"x": "http://www.w3.org/1999/xhtml"}
    pages = document.findall(".//x:page", namespace)
    for number, chapter in enumerate(chapters, 1):
        page = pages[chapter["destpageposfrom1"] - 1]
        # Exclude the shared running header and page-number footer.
        lines = [line for line in page.findall(".//x:line", namespace)
                 if 50 <= float(line.attrib["yMin"]) < float(page.attrib["height"]) - 35]
        first = lines[0]
        words = first.findall("x:word", namespace)
        assert float(first.attrib["yMin"]) < 100, chapter["title"]
        assert words[0].text == str(number), chapter["title"]
        assert float(page.attrib["height"]) - chapter["dest"][3] < 100, chapter["title"]
    assert any(child["destpageposfrom1"] == chapter["destpageposfrom1"]
               for chapter in chapters for child in chapter["kids"])

    # The notice is a real final-page heading, not a bookmark at the previous
    # chapter's last hyperref anchor. Check rendered text independently of the
    # renderer's destination metadata.
    notice = outlines[-1]
    assert notice["destpageposfrom1"] == len(pages)
    assert notice["destpageposfrom1"] > chapter_pages[-1]
    notice_page = pages[-1]
    notice_lines = [line for line in notice_page.findall(".//x:line", namespace)
                    if 50 <= float(line.attrib["yMin"]) < float(notice_page.attrib["height"]) - 35]
    assert " ".join(word.text for word in notice_lines[0].findall("x:word", namespace)) == "Font license notice"
    assert float(notice_lines[0].attrib["yMin"]) < 100
    assert float(notice_page.attrib["height"]) - notice["dest"][3] < 100
