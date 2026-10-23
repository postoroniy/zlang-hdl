"""Manifest-owned chapter assembly, anchor routing and publication-boundary checks.

This is a Markdown publication pass, not a language analyzer. It never edits
the canonical chapters and never parses ZLang examples.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

try:
    from tools.documentation_manifest import DocumentationError, Manual, load_manuals
except ModuleNotFoundError:
    from documentation_manifest import DocumentationError, Manual, load_manuals


PRIVATE_PATTERNS = (
    (r"/(?:home|Users|mnt)/", "local filesystem path"),
    (r"[A-Za-z]:\\\\", "Windows filesystem path"),
    (r"\bAGENTS\.md\b|\bcurrent-task\.md\b", "private handoff"),
    (r"(?:^|/)\.worktrees(?:/|$)|\bcodex/[-\w]+", "private workspace"),
    (r"\b(?:private checkout|internal only|do not publish|TODO|FIXME)\b", "private note"),
    (r"\btest[23]\.zhl\b|docs/internal/", "private source"),
    (r"\b(?:ghp_|github_pat_|AKIA)[A-Za-z0-9_\-]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----", "credential"),
    (r"\b(?:temporal_schedule|temporal_inventory|shared_mcm|T3[abcdefghi]|T4[abc])\b", "unprojected implementation"),
)


def source_paths(root: Path, manual: Manual | None = None) -> tuple[Path, ...]:
    return manual.sources if manual is not None else tuple(p for m in load_manuals(root) for p in m.sources)


def assert_public(text: str, source: Path) -> None:
    for pattern, label in PRIVATE_PATTERNS:
        match = re.search(pattern, text, re.I | re.M)
        if match:
            line = text[:match.start()].count("\n") + 1
            raise DocumentationError(f"{source}:{line}: public manual contains {label}: {match[0]!r}")


def slug(text: str) -> str:
    # GitHub-style fragment names for ordinary prose headings. Explicit HTML
    # aliases remain authoritative for historical reference-* names.
    text = re.sub(r"`+([^`]+)`+", lambda m: m[1].replace("<", "").replace(">", ""), text)
    text = re.sub(r"<[^>]*>", "", text).lower().replace(" ", "-")
    return re.sub(r"[^\w\-]", "", text)


def _headings(text: str):
    fenced = False
    aliases: list[str] = []
    seen: dict[str, int] = {}
    for index, line in enumerate(text.splitlines()):
        if line.startswith("```"):
            fenced = not fenced
        if fenced:
            continue
        match = re.fullmatch(r'<a id="([^"]+)"></a>', line)
        if match:
            aliases.append(match[1])
            continue
        match = re.match(r"^(#{1,6}) (.+)$", line)
        if match:
            base = slug(match[2])
            occurrence = seen.get(base, 0)
            seen[base] = occurrence + 1
            name = base + (f"-{occurrence}" if occurrence else "")
            yield index, match[1], match[2], (name, *aliases)
            aliases = []


def destinations(root: Path):
    """Stable destinations are path+heading scoped; allocation order is irrelevant."""
    result = {}
    legacy = {}
    for manual in load_manuals(root):
        result[(manual.entrypoint.as_posix(), "")] = (manual, manual.identity)
        for source in manual.sources:
            text = (root / source).read_text(encoding="utf-8")
            headings = tuple(_headings(text))
            if not headings:
                raise DocumentationError(f"chapter has no headings: {source}")
            for _, _, _, names in headings:
                identity = "d-" + hashlib.sha256((source.as_posix()+"#"+names[0]).encode()).hexdigest()[:24]
                for name in names:
                    result[(source.as_posix(), name)] = (manual, identity)
                    if name.startswith("reference-"):
                        if name in legacy:
                            raise DocumentationError(f"duplicate legacy destination: {name}")
                        legacy[name] = (manual, identity)
                result.setdefault((source.as_posix(), ""), (manual, identity))
    for name, target in legacy.items():
        result[("docs/language-reference.md", name)] = target
    return result


def _resolve(source: Path, target: str) -> tuple[str, str] | None:
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"https", "http", "mailto"}:
            raise DocumentationError(f"unsafe manual link scheme: {source}: {target}")
        return None
    path = unquote(parsed.path)
    resolved = posixpath.normpath(posixpath.join(source.parent.as_posix(), path)) if path else source.as_posix()
    if resolved.startswith("../") or resolved.startswith("/"):
        raise DocumentationError(f"link escapes repository: {source}: {target}")
    return resolved, unquote(parsed.fragment)


def assemble(root: Path, manual: Manual) -> str:
    targets = destinations(root)
    chapter_paths = {p.as_posix() for p in source_paths(root)}
    chunks = [f'<a id="{manual.identity}"></a>\n',
              manual.description + ".\n"]
    for source in manual.sources:
        text = (root / source).read_text(encoding="utf-8")
        assert_public(text, source)
        lines = text.splitlines()
        for index, level, title, names in _headings(text):
            _, identity = targets[(source.as_posix(), names[0])]
            # The existing renderer promotes H2 to chapter level below the cover.
            lines[index] = f'\n<a id="{identity}"></a>\n{level}# {title}'
        text = "\n".join(line for line in lines if not re.fullmatch(r'<a id="[^"]+"></a>', line))

        def link(match):
            target = match[1]
            resolved = _resolve(source, target)
            if resolved is None:
                return match[0]
            path, fragment = resolved
            destination = targets.get((path, fragment))
            if destination:
                owner, identity = destination
                return "](" + ("" if owner.identity == manual.identity else owner.pdf.name) + "#" + identity + ")"
            if path.endswith(".md") and path in chapter_paths:
                raise DocumentationError(f"missing chapter anchor: {source}: {target}")
            if not (root / path).exists():
                raise DocumentationError(f"broken manual link: {source}: {target}")
            # Non-book source links use the reviewed public baseline, never a
            # private integration commit or a moving main branch.
            return "](https://github.com/postoroniy/zlang-hdl/blob/3b083ca/" + path + ("#"+fragment if fragment else "") + ")"

        text = re.sub(r"\]\(([^)\s]+)\)", link, text)
        chunks.append(text.strip())
    return "\n\n".join(chunks) + "\n"


def entrypoint_markdown(root: Path, manual: Manual) -> str:
    version = json.loads((root / "release/status.json").read_text(encoding="utf-8"))["release"]["version"]
    lines = [f"# ZLang HDL {manual.title}", "", manual.description + ".", "",
             f"Candidate: **{version}**. Direct SystemVerilog is the production RTL backend.", "",
             "[Language Reference](language-reference.md) · [Standard Library Guide](stdlib.md) · [User Guide](user-guide.md)", "",
             f"[Download the PDF]({manual.pdf.name})", "", "## Chapters", ""]
    for source in manual.sources:
        heading = next(_headings((root / source).read_text(encoding="utf-8")))[2]
        lines.append(f"- [{heading}]({source.name})")
    if manual.identity == "language-reference":
        lines += ["", "[Native `--logic-state` simulation and diagnostics](native-jit-simulation.md#four-state-initialization-and-diagnostics)",
                  "", "## Previous reference links", "",
                  "Existing `reference-*` links remain as navigation aliases. The linked chapter owns the text.", ""]
        for book in load_manuals(root):
            for source in book.sources:
                for _, _, _, names in _headings((root / source).read_text(encoding="utf-8")):
                    for name in names:
                        if name.startswith("reference-"):
                            lines += [f'<a id="{name}"></a>',
                                      f'- [Continue in the canonical chapter]({source.name}#{name})', ""]
    return "\n".join(lines).rstrip() + "\n"


def validate_markdown_links(root: Path, *, allow_missing_pdfs: bool = False) -> int:
    """Check book/index/legacy Markdown fragments before any PDF rendering."""
    manuals = load_manuals(root)
    pdfs = {manual.pdf.as_posix() for manual in manuals}
    sources = tuple(p for manual in manuals for p in (manual.entrypoint, *manual.sources))
    anchors: dict[str, set[str]] = {}
    count = 0
    for source in sources:
        text = (root / source).read_text(encoding="utf-8")
        text = re.sub(r"(?m)^```[^\n]*\n.*?^```\s*$", "", text, flags=re.DOTALL)
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            resolved = _resolve(source, target)
            if resolved is None:
                continue
            path, fragment = resolved
            if not (root / path).exists() and not (allow_missing_pdfs and path in pdfs):
                raise DocumentationError(f"broken manual link: {source}: {target}")
            if path.endswith(".md") and fragment:
                if path not in anchors:
                    other = (root / path).read_text(encoding="utf-8")
                    names = {name for _, _, _, values in _headings(other) for name in values}
                    names.update(re.findall(r'<a id="([^"]+)"></a>', other))
                    anchors[path] = names
                if fragment not in anchors[path]:
                    raise DocumentationError(f"broken Markdown anchor: {source}: {target}")
            count += 1
    return count


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Check or regenerate manifest-owned manual indexes")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--write-indexes", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    for manual in load_manuals(root):
        expected = entrypoint_markdown(root, manual)
        path = root / manual.entrypoint
        if args.write_indexes:
            path.write_text(expected, encoding="utf-8")
        elif path.read_text(encoding="utf-8") != expected:
            raise DocumentationError(f"manual index is stale: {manual.entrypoint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
