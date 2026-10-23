"""Bounded validation of standard, portable navigation in a manual PDF set."""
from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import urlsplit

try:
    from tools.documentation_manifest import DocumentationError
except ModuleNotFoundError:
    from documentation_manifest import DocumentationError


def _objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def validate_text_layout(text: str) -> None:
    """Reject detached punctuation and split operators in extracted PDF lines.

    A leading member access (.transfer), relative path or ellipsis is legitimate;
    an isolated sentence/list punctuation mark is not. This complements geometry
    checks: detached punctuation can be entirely inside the page margins.
    """
    checks = (
        (r"(?m)^\s*[.,;:](?:\s|$)", "detached punctuation"),
        (r"-\n[ \t]*-[A-Za-z]", "split CLI option prefix"),
        (r"[0-9]\.\n[ \t]*\.[A-Za-z0-9]", "split range operator"),
    )
    for number, page in enumerate(text.split("\f"), 1):
        for pattern, description in checks:
            if re.search(pattern, page):
                raise DocumentationError(f"manual PDF page {number}: {description}")


def validate_actions(data: dict, current: str, names: dict[str, set[str]]) -> None:
    for node in _objects(data):
        kind = node.get("/S")
        if "/JavaScript" in node or kind in {
            "/JavaScript", "/Launch", "/GoToE", "/SubmitForm", "/ImportData", "/Rendition"
        }:
            raise DocumentationError("manual PDF contains a forbidden active action")
        if kind == "/URI":
            uri = node.get("/URI")
            if not isinstance(uri, str) or not uri.startswith("u:") or urlsplit(uri[2:]).scheme not in {"http", "https", "mailto"}:
                raise DocumentationError("manual PDF contains an unsafe URI")
        if kind in {"/GoTo", "/GoToR"}:
            target = current
            if kind == "/GoToR":
                target = node.get("/F", "")
                if not isinstance(target, str) or not target.startswith("u:"):
                    raise DocumentationError("manual PDF has a nonportable remote file specification")
                target = target[2:]
                if target not in names:
                    raise DocumentationError("manual PDF links outside the three-book folder")
            destination = node.get("/D")
            if isinstance(destination, str) and destination.startswith("u:"):
                if destination[2:] not in names[target]:
                    raise DocumentationError("manual PDF has an unresolved named destination")
            elif kind == "/GoToR":
                raise DocumentationError("inter-book PDF links must use stable named destinations")


def verify_pdf_links(paths: dict[str, Path], run) -> None:
    """Inspect bytes, not recorded success flags; run enforces tool time bounds."""
    names = {
        name: set(re.findall(r'"([^"]+)"$', run(("pdfinfo", "-dests", str(path))), re.M))
        for name, path in paths.items()
    }
    for name, path in paths.items():
        try:
            data = json.loads(run(("qpdf", "--json", "--json-stream-data=none", str(path), "-")))
        except ValueError as exc:
            raise DocumentationError("qpdf returned malformed PDF structure") from exc
        validate_actions(data, name, names)
