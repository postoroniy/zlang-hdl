from __future__ import annotations

import pytest

from tools.documentation_manifest import DocumentationError
from tools.documentation_pdf import validate_actions, validate_text_layout


NAMES = {"ZLang-HDL-Language-Reference.pdf": {"language", "typed-values"},
         "ZLang-HDL-User-Guide.pdf": {"user", "debugger"}}
CURRENT = "ZLang-HDL-Language-Reference.pdf"


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
