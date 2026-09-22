"""Consolidated compiler/LSP parity for supported concise source forms."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from live_edit_catalog import LIVE_EDIT_CASES, LanguageSurfaceCase
from zlang.compiler import compile_source
from zlang.lsp.server import LspServer, path_to_uri
from zlang.opt import lower
from zlang.opt.identity import canonical_ir_identity
from zlang.public_capabilities import CAPABILITY_REGISTRY


CAPABILITY_WITNESSES = tuple(sorted({
    (item.name, item.witness.source_path, item.witness.top)
    for item in CAPABILITY_REGISTRY.capabilities
}))


@pytest.mark.parametrize("case", LIVE_EDIT_CASES, ids=lambda case: case.surface_id)
def test_live_edit_explicit_and_concise_forms_share_canonical_semantics(
    case: LanguageSurfaceCase,
) -> None:
    explicit = compile_source(case.explicit).ir
    concise = compile_source(case.concise).ir
    assert canonical_ir_identity(lower(explicit)) == canonical_ir_identity(
        lower(concise)
    )


@pytest.mark.parametrize("case", LIVE_EDIT_CASES, ids=lambda case: case.surface_id)
def test_live_edit_sequence_never_creates_a_tooling_placeholder(
    tmp_path: Path,
    case: LanguageSurfaceCase,
) -> None:
    source = tmp_path / f"{case.surface_id}.zhl"
    source.write_text(case.explicit, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()

    opened = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {
            "uri": uri,
            "version": 1,
            "text": case.explicit,
        }},
    })
    assert opened[0]["params"]["diagnostics"] == []

    incomplete = case.explicit[:-1]
    invalid = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": incomplete}],
        },
    })
    diagnostics = invalid[0]["params"]["diagnostics"]
    assert diagnostics
    assert all(item["code"] != "ZL-TOOLING-001" for item in diagnostics)
    assert any(
        item["range"]["start"] != {"line": 0, "character": 0}
        for item in diagnostics
    )

    for version, text in ((3, case.concise), (4, case.explicit)):
        changed = server.dispatch({
            "jsonrpc": "2.0",
            "method": "textDocument/didChange",
            "params": {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            },
        })
        assert changed[0]["params"]["diagnostics"] == []
        completion = server.dispatch({
            "jsonrpc": "2.0",
            "id": version,
            "method": "textDocument/completion",
            "params": {
                "textDocument": {"uri": uri},
                "position": {"line": 0, "character": 0},
            },
        })
        assert completion[-1].get("error") is None


@pytest.mark.parametrize(
    ("capability", "relative_path", "top"),
    CAPABILITY_WITNESSES,
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_every_public_capability_witness_passes_the_lsp_diagnostic_path(
    capability: str,
    relative_path: str,
    top: str,
) -> None:
    del capability
    source = Path(relative_path).resolve()
    text = source.read_text(encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {
            "uri": uri,
            "version": 1,
            "text": text,
        }},
    })
    server.documents[uri] = replace(
        server.documents[uri], navigation_top=top
    )
    messages = server._publish_document(server.documents[uri])
    published = [
        item for item in messages
        if item.get("method") == "textDocument/publishDiagnostics"
    ]
    assert published == [{
        "jsonrpc": "2.0",
        "method": "textDocument/publishDiagnostics",
        "params": {"uri": uri, "diagnostics": []},
    }]

    incomplete = text + "\nmodule"
    invalid = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": incomplete}],
        },
    })
    invalid_diagnostics = invalid[0]["params"]["diagnostics"]
    assert invalid_diagnostics
    assert all(
        item["code"] != "ZL-TOOLING-001" for item in invalid_diagnostics
    )

    restored = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": {"uri": uri, "version": 3},
            "contentChanges": [{"text": text}],
        },
    })
    assert restored[0]["params"]["diagnostics"] == []
