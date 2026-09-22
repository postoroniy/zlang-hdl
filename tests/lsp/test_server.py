from __future__ import annotations

from io import BytesIO
import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

from zlang.analysis_needs import AnalysisNeeds
from zlang.lsp.server import (
    DocumentState,
    LspServer,
    SEMANTIC_TOKEN_MODIFIERS,
    SEMANTIC_TOKEN_TYPES,
    DIAGNOSTIC_DEBOUNCE_SECONDS,
    _DiagnosticScheduler,
    _fix_to_code_action,
    origin_to_range,
    path_to_uri,
    read_message,
    semantic_tokens_to_lsp,
    uri_to_path,
    write_message,
)
from zlang.tooling import (
    ToolingDiagnosticEdit,
    ToolingDiagnosticFix,
    ToolingOrigin,
    ToolingSemanticToken,
    check_snapshot,
)
from zlang.workspace_parse_cache import load_parse_index


VALID = "module Top { out y:u8 y=1 }\n"
SEMANTICALLY_INVALID = "module Top { in a:u8 out y:u7 y=a+1 }\n"
SYNTAX_INVALID = "module Top {\n"


def _messages(payload: bytes) -> list[object]:
    stream = BytesIO(payload)
    result: list[object] = []
    while True:
        item = read_message(stream)
        if item is None:
            return result
        result.append(item)


def _request(stream: BytesIO, message: object) -> None:
    write_message(stream, message)


def test_json_rpc_reader_assembles_short_unbuffered_reads() -> None:
    class ShortReadStream(BytesIO):
        def read(self, size: int = -1) -> bytes:
            return super().read(min(size, 3) if size >= 0 else 3)

    payload = BytesIO()
    _request(payload, {"jsonrpc": "2.0", "id": 7, "method": "shutdown"})
    assert read_message(ShortReadStream(payload.getvalue())) == {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "shutdown",
    }


def test_stdio_transport_argument_starts_a_real_json_rpc_process() -> None:
    """Match the argv that vscode-languageclient uses in production."""

    payload = BytesIO()
    _request(payload, {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {},
    })
    _request(payload, {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "shutdown",
        "params": None,
    })
    _request(payload, {
        "jsonrpc": "2.0",
        "method": "exit",
        "params": None,
    })
    completed = subprocess.run(
        (
            sys.executable,
            "-c",
            (
                "from zlang.lsp.server import main; "
                "raise SystemExit(main(['--stdio']))"
            ),
        ),
        input=payload.getvalue(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8")
    assert completed.stderr == b""
    messages = _messages(completed.stdout)
    assert [message["id"] for message in messages] == [1, 2]
    assert messages[0]["result"]["serverInfo"]["name"] == "zlang-lsp"
    assert messages[1]["result"] is None


def test_diagnostic_scheduler_replaces_deadlines_with_an_injected_clock() -> None:
    now = 10.0
    scheduler = _DiagnosticScheduler(lambda: now)
    scheduler.replace(("first",))
    assert scheduler.due() == ()
    assert scheduler.timeout() == pytest.approx(DIAGNOSTIC_DEBOUNCE_SECONDS)

    now += 0.100
    scheduler.replace(("first", "second"))
    now += 0.249
    assert scheduler.due() == ()
    now += 0.001
    assert scheduler.due() == ("first", "second")
    assert scheduler.timeout() is None


def test_uri_round_trip_handles_escaping(tmp_path: Path) -> None:
    path = tmp_path / "space name.zhl"
    uri = path_to_uri(path)
    assert "%20" in uri
    assert uri_to_path(uri) == path
    assert uri_to_path(uri.replace("file:///", "file://localhost/", 1)) == path


def test_uri_rejects_remote_and_virtual_documents() -> None:
    import pytest

    with pytest.raises(ValueError, match="local file"):
        uri_to_path("untitled:buffer.zhl")
    with pytest.raises(ValueError, match="remote"):
        uri_to_path("file://server/share/design.zhl")


def test_origin_to_lsp_range_is_zero_based() -> None:
    first = ToolingOrigin("top.zhl", "name a", 1, 1, 1, 2)
    assert origin_to_range(first) == {
        "start": {"line": 0, "character": 0},
        "end": {"line": 0, "character": 1},
    }
    origin = ToolingOrigin("top.zhl", "operator +", 3, 4, 4, 9)
    assert origin_to_range(origin) == {
        "start": {"line": 2, "character": 3},
        "end": {"line": 3, "character": 8},
    }
    assert origin_to_range(None) == {
        "start": {"line": 0, "character": 0},
        "end": {"line": 0, "character": 0},
    }


def test_dispatch_advertises_only_supported_capabilities_and_lifecycle() -> None:
    server = LspServer()
    response = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    capabilities = response[0]["result"]["capabilities"]
    assert capabilities == {
        "textDocumentSync": {"openClose": True, "change": 1},
        "documentSymbolProvider": True,
        "hoverProvider": True,
        "definitionProvider": True,
        "referencesProvider": True,
        "renameProvider": True,
        "completionProvider": {"resolveProvider": False},
        "signatureHelpProvider": {},
        "semanticTokensProvider": {
            "legend": {
                "tokenTypes": ["parameter", "variable", "property", "function"],
                "tokenModifiers": ["declaration"],
            },
            "full": True,
            "range": False,
        },
        "codeActionProvider": {"codeActionKinds": ["quickfix"]},
    }
    assert "prepareRenameProvider" not in capabilities
    assert "resolveProvider" not in capabilities["codeActionProvider"]
    assert server.dispatch({"jsonrpc": "2.0", "method": "initialized"}) == []
    assert server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "shutdown"}) == [
        {"jsonrpc": "2.0", "id": 2, "result": None}
    ]
    assert server.shutdown_requested is True
    assert server.dispatch({"jsonrpc": "2.0", "method": "exit"}) == []
    assert server.exit_requested is True


def test_document_symbol_request_uses_current_open_text(tmp_path: Path) -> None:
    source = tmp_path / "Top.zhl"
    source.write_text(VALID, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": VALID}},
    })
    response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 9,
        "method": "textDocument/documentSymbol",
        "params": {"textDocument": {"uri": uri}},
    })
    assert response[0]["id"] == 9
    assert response[0]["result"][0]["name"] == "Top"
    assert response[0]["result"][0]["kind"] == 2
    assert [item["name"] for item in response[0]["result"][0]["children"]] == [
        "y"
    ]
    changed = "module Edited { out z:u8 z=1 }\n"
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": changed}],
        },
    })
    changed_symbols = server.dispatch({
        "jsonrpc": "2.0",
        "id": 10,
        "method": "textDocument/documentSymbol",
        "params": {"textDocument": {"uri": uri}},
    })
    assert changed_symbols[0]["result"][0]["name"] == "Edited"
    assert changed_symbols[0]["result"][0]["children"][0]["name"] == "z"


def test_document_symbol_nested_and_malformed_current_text(tmp_path: Path) -> None:
    source = tmp_path / "Declarations.zhl"
    text = "struct Pair { left:u8 right:u8 }\nfn add(a:u8) { a }\n"
    source.write_text(text, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    result = server.dispatch({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "textDocument/documentSymbol",
        "params": {"textDocument": {"uri": uri}},
    })
    assert [item["name"] for item in result[0]["result"]] == ["Pair", "add"]
    assert [item["name"] for item in result[0]["result"][0]["children"]] == [
        "left", "right"
    ]
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": SYNTAX_INVALID}],
        },
    })
    malformed = server.dispatch({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "textDocument/documentSymbol",
        "params": {"textDocument": {"uri": uri}},
    })
    assert malformed == [{"jsonrpc": "2.0", "id": 2, "result": []}]


def test_document_symbol_unknown_or_closed_document_is_a_request_error(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    source.write_text(VALID, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    unknown = server.dispatch({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "textDocument/documentSymbol",
        "params": {"textDocument": {"uri": uri}},
    })
    assert unknown[0]["error"]["code"] == -32602
    invalid_uri = server.dispatch({
        "jsonrpc": "2.0",
        "id": 8,
        "method": "textDocument/documentSymbol",
        "params": {"textDocument": {"uri": "untitled:buffer.zhl"}},
    })
    assert invalid_uri[0]["error"]["code"] == -32602
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": VALID}},
    })
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didClose",
        "params": {"textDocument": {"uri": uri}},
    })
    closed = server.dispatch({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "textDocument/documentSymbol",
        "params": {"textDocument": {"uri": uri}},
    })
    assert closed[0]["error"]["code"] == -32602


def test_hover_uses_current_semantic_text_and_formats_type_facts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    text = "module Top { in a:u8 out y:u9 y=a+1 }\n"
    source.write_text(text, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 11,
        "method": "textDocument/hover",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 0, "character": text.index("a")},
        },
    })
    hover = response[0]["result"]
    assert hover["contents"] == {
        "kind": "markdown",
        "value": "a : u8\ninput, unsigned, 8 bit(s)",
    }
    assert hover["range"]["start"] == {"line": 0, "character": 13}

    changed = "module Top { in s:s8 out y:s9 y=s+1 }\n"
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": changed}],
        },
    })
    changed_hover = server.dispatch({
        "jsonrpc": "2.0",
        "id": 12,
        "method": "textDocument/hover",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 0, "character": changed.index("s")},
        },
    })
    assert "signed, 8 bit(s)" in changed_hover[0]["result"]["contents"]["value"]


def test_hover_supports_fixed_and_function_facts_and_safe_empty_results(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Declarations.zhl"
    text = (
        "fn add(x:u8, y:u8) -> u9 { x+y }\n"
        "module Top { in f:SF8.8 out y:SF9.8 y=f }\n"
    )
    source.write_text(text, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    function = server.dispatch({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "textDocument/hover",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 0, "character": text.index("add")},
        },
    })
    assert function[0]["result"]["contents"]["value"] == (
        "fn add(x : u8, y : u8) -> u9\nunsigned, 9 bit(s)"
    )
    fixed_line = text.splitlines()[1]
    fixed = server.dispatch({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "textDocument/hover",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 1, "character": fixed_line.index("f")},
        },
    })
    assert "fixed-point fixed<16,8>" in fixed[0]["result"]["contents"]["value"]
    assert server.dispatch({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "textDocument/hover",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 1, "character": 0},
        },
    })[0]["result"] is None
    assert server.dispatch({
        "jsonrpc": "2.0",
        "id": 4,
        "method": "textDocument/hover",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 20, "character": 0},
        },
    })[0]["result"] is None


def test_hover_malformed_and_closed_document_are_safe_request_results(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    source.write_text(SYNTAX_INVALID, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {"uri": uri, "version": 1, "text": SYNTAX_INVALID}
        },
    })
    malformed = server.dispatch({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "textDocument/hover",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 0, "character": 0},
        },
    })
    assert malformed == [{"jsonrpc": "2.0", "id": 1, "result": None}]
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didClose",
        "params": {"textDocument": {"uri": uri}},
    })
    closed = server.dispatch({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "textDocument/hover",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 0, "character": 0},
        },
    })
    assert closed[0]["error"]["code"] == -32602


def test_real_compiler_open_change_and_close(tmp_path: Path) -> None:
    source = tmp_path / "Top.zhl"
    source.write_text(VALID, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()

    opened = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": VALID}},
    })
    assert opened[0]["method"] == "textDocument/publishDiagnostics"
    assert opened[0]["params"]["diagnostics"] == []
    assert server.documents[uri].text == VALID

    invalid = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": SEMANTICALLY_INVALID}],
        },
    })
    diagnostics = invalid[0]["params"]["diagnostics"]
    assert diagnostics
    assert diagnostics[0]["source"] == "zlang"
    assert diagnostics[0]["code"].startswith("ZL-")
    assert diagnostics[0]["severity"] == 1
    assert server.documents[uri].version == 2

    valid_again = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": {"uri": uri, "version": 3},
            "contentChanges": [{"text": VALID}],
        },
    })
    assert valid_again[0]["params"]["diagnostics"] == []

    closed = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didClose",
        "params": {"textDocument": {"uri": uri}},
    })
    assert closed[0]["params"] == {"uri": uri, "diagnostics": []}
    assert uri not in server.documents


def test_tooling_session_reuses_queries_and_invalidates_on_did_change(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The LSP session reuses semantic products until the document changes."""

    import zlang.tooling as tooling

    source = tmp_path / "Top.zhl"
    source.write_text(VALID, encoding="utf-8")
    uri = path_to_uri(source)
    original = tooling.check_file_snapshot
    calls = 0

    def observed(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": VALID}},
    })
    assert calls == 1

    definition_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "textDocument/definition",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 0, "character": VALID.index("y")},
        },
    }
    server.dispatch(definition_request)
    server.dispatch({**definition_request, "id": 2})
    # didOpen's diagnostics snapshot is upgraded once with definition records;
    # the second navigation request reuses that richer product.
    assert calls == 2

    changed = "module Top { out y:u8 y=2 }\n"
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": changed}],
        },
    })
    # didChange invalidates the old entry and publishes one fresh snapshot;
    # the first definition request upgrades it with definition records and the
    # second request hits that richer entry.
    assert calls == 3
    changed_request = {
        **definition_request,
        "id": 3,
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 0, "character": changed.index("y")},
        },
    }
    server.dispatch(changed_request)
    server.dispatch({**changed_request, "id": 4})
    assert calls == 4


def test_preview_close_reopen_reuses_content_validated_semantic_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VS Code preview churn must not recompile an unchanged definition file."""

    import zlang.tooling as tooling

    source = (tmp_path / "Preview.zhl").resolve()
    source.write_text(VALID, encoding="utf-8")
    uri = path_to_uri(source)
    original = tooling.check_file_snapshot
    calls = 0

    def observed(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    server = LspServer()

    def open_text(text: str, version: int) -> None:
        response = server.dispatch({
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": uri,
                    "version": version,
                    "text": text,
                },
            },
        })
        assert response[0]["params"]["diagnostics"] == []

    def close() -> None:
        server.dispatch({
            "jsonrpc": "2.0",
            "method": "textDocument/didClose",
            "params": {"textDocument": {"uri": uri}},
        })

    open_text(VALID, 1)
    close()
    open_text(VALID, 2)
    assert calls == 1

    close()
    changed = "module Top { out y:u8 y=2 }\n"
    source.write_text(changed, encoding="utf-8")
    open_text(changed, 3)
    assert calls == 2


def test_definition_target_reuses_parent_symbol_shard_for_open_and_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A navigated child needs no duplicate diagnostics/token compilation."""

    import zlang.tooling as tooling
    from zlang.workspace import update_project_lock

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    root = tmp_path / "demo"
    source_root = root / "src"
    source_root.mkdir(parents=True)
    (root / "zlang.toml").write_text(
        'schema = 1\n[project]\nname = "demo"\nversion = "0.1.0"\n'
        'source-root = "src"\n',
        encoding="utf-8",
    )
    child = source_root / "child.zhl"
    child_text = "module Leaf { out y:u8 y=1 }\n"
    child.write_text(child_text, encoding="utf-8")
    top = source_root / "top.zhl"
    top_text = (
        "import demo.child\n"
        "module Top { child:Leaf out y:u8 y=child.y }\n"
    )
    top.write_text(top_text, encoding="utf-8")
    update_project_lock(root / "zlang.toml")

    original = tooling.check_file_snapshot
    calls = 0

    def observed(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    server = LspServer()
    top_uri = path_to_uri(top)
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": top_uri,
                "version": 1,
                "text": top_text,
            },
        },
    })
    leaf_line = 1
    definition = server.dispatch({
        "jsonrpc": "2.0",
        "id": 60,
        "method": "textDocument/definition",
        "params": {
            "textDocument": {"uri": top_uri},
            "position": {
                "line": leaf_line,
                "character": top_text.splitlines()[leaf_line].index("Leaf"),
            },
        },
    })
    assert definition[0]["result"]["uri"] == path_to_uri(child)
    assert calls == 2

    # A restarted server uses the exact root shard for diagnostics/tokens and
    # the repeated F12 establishes the one-shot navigation target context.
    server = LspServer()
    reopened_top = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": top_uri,
                "version": 2,
                "text": top_text,
            },
        },
    })
    assert reopened_top[0]["params"]["diagnostics"] == []
    definition = server.dispatch({
        "jsonrpc": "2.0",
        "id": 62,
        "method": "textDocument/definition",
        "params": {
            "textDocument": {"uri": top_uri},
            "position": {
                "line": leaf_line,
                "character": top_text.splitlines()[leaf_line].index("Leaf"),
            },
        },
    })
    assert definition[0]["result"]["uri"] == path_to_uri(child)
    assert calls == 2

    child_uri = path_to_uri(child)
    opened = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": child_uri,
                "version": 1,
                "text": child_text,
            },
        },
    })
    assert opened[0]["params"]["diagnostics"] == []
    tokens = server.dispatch({
        "jsonrpc": "2.0",
        "id": 61,
        "method": "textDocument/semanticTokens/full",
        "params": {"textDocument": {"uri": child_uri}},
    })
    assert tokens[0]["result"]["data"]
    assert calls == 2

    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didClose",
        "params": {"textDocument": {"uri": child_uri}},
    })
    changed = "module Leaf { out y:u8 y=2 }\n"
    child.write_text(changed, encoding="utf-8")
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": child_uri,
                "version": 2,
                "text": changed,
            },
        },
    })
    assert calls == 3


def test_trivia_edit_reuses_clean_diagnostic_proof_but_invalid_edit_rechecks(
    tmp_path: Path, monkeypatch,
) -> None:
    import zlang.tooling as tooling

    source = tmp_path / "Top.zhl"
    source.write_text(VALID)
    uri = path_to_uri(source)
    original = tooling.check_file_snapshot
    calls = 0

    def observed(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0", "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": VALID}},
    })
    assert calls == 1
    trivia = "// note 😀\n" + VALID
    response = server.dispatch({
        "jsonrpc": "2.0", "method": "textDocument/didChange",
        "params": {"textDocument": {"uri": uri, "version": 2},
                   "contentChanges": [{"text": trivia}]},
    })
    assert response[0]["params"]["diagnostics"] == []
    assert server.documents[uri].version == 2
    assert calls == 1
    same_bytes = server.dispatch({
        "jsonrpc": "2.0", "method": "textDocument/didChange",
        "params": {"textDocument": {"uri": uri, "version": 3},
                   "contentChanges": [{"text": trivia}]},
    })
    assert same_bytes[0]["params"]["diagnostics"] == []
    assert server.documents[uri].version == 3 and calls == 1
    server.dispatch({
        "jsonrpc": "2.0", "method": "textDocument/didChange",
        "params": {"textDocument": {"uri": uri, "version": 4},
                   "contentChanges": [{"text": SYNTAX_INVALID}]},
    })
    assert calls == 2


def test_syntax_diagnostics_and_stale_versions(tmp_path: Path) -> None:
    source = tmp_path / "Top.zhl"
    source.write_text(SYNTAX_INVALID, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    opened = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 5, "text": SYNTAX_INVALID}},
    })
    assert opened[0]["params"]["diagnostics"][0]["code"].startswith("ZL-")
    stale = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": {"uri": uri, "version": 4},
            "contentChanges": [{"text": VALID}],
        },
    })
    assert stale == []
    assert server.documents[uri].version == 5


def test_syntax_diagnostic_marks_the_failing_line_instead_of_file_start(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    text = (
        "module Top {\n"
        "  in a : u8\n"
        "  out y : u8\n"
        "  y = a +\n"
        "}\n"
    )
    source.write_text(text, encoding="utf-8")
    response = LspServer().dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": path_to_uri(source),
                "version": 1,
                "text": text,
            },
        },
    })

    diagnostic = response[0]["params"]["diagnostics"][0]
    assert diagnostic["code"] == "ZL-PARSE-001"
    assert diagnostic["range"] == {
        "start": {"line": 4, "character": 0},
        "end": {"line": 4, "character": 1},
    }
    assert diagnostic["data"]["construct"] == "syntax error"


def test_full_text_protocol_rejects_range_changes(tmp_path: Path) -> None:
    source = tmp_path / "Top.zhl"
    source.write_text(VALID, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": VALID}},
    })
    result = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 1},
                },
                "text": "x",
            }],
        },
    })
    assert result[0]["params"]["diagnostics"][0]["code"] == "ZL-LSP-CHANGE-001"
    assert server.documents[uri].text == VALID


def test_framed_json_rpc_session_exits_cleanly(tmp_path: Path) -> None:
    source = tmp_path / "Top.zhl"
    source.write_text(VALID, encoding="utf-8")
    uri = path_to_uri(source)
    incoming = BytesIO()
    _request(incoming, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    _request(incoming, {"jsonrpc": "2.0", "method": "initialized"})
    _request(incoming, {
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": VALID}},
    })
    _request(incoming, {"jsonrpc": "2.0", "id": 2, "method": "shutdown"})
    _request(incoming, {"jsonrpc": "2.0", "method": "exit"})
    incoming.seek(0)
    outgoing = BytesIO()
    assert LspServer().run(incoming, outgoing) == 0
    messages = _messages(outgoing.getvalue())
    assert messages[0]["id"] == 1
    assert messages[1]["method"] == "textDocument/publishDiagnostics"
    assert messages[1]["params"]["diagnostics"] == []
    assert messages[2] == {"jsonrpc": "2.0", "id": 2, "result": None}


def test_framed_json_rpc_document_symbol_request(tmp_path: Path) -> None:
    source = tmp_path / "Top.zhl"
    source.write_text(VALID, encoding="utf-8")
    uri = path_to_uri(source)
    incoming = BytesIO()
    _request(incoming, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    _request(incoming, {
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": VALID}},
    })
    _request(incoming, {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "textDocument/documentSymbol",
        "params": {"textDocument": {"uri": uri}},
    })
    _request(incoming, {"jsonrpc": "2.0", "id": 3, "method": "shutdown"})
    _request(incoming, {"jsonrpc": "2.0", "method": "exit"})
    incoming.seek(0)
    outgoing = BytesIO()
    assert LspServer().run(incoming, outgoing) == 0
    messages = _messages(outgoing.getvalue())
    assert messages[0]["id"] == 1
    assert messages[1]["id"] == 2
    assert messages[1]["result"][0]["name"] == "Top"
    assert messages[2]["method"] == "textDocument/publishDiagnostics"
    assert messages[3] == {"jsonrpc": "2.0", "id": 3, "result": None}


def test_framed_json_rpc_hover_request(tmp_path: Path) -> None:
    source = tmp_path / "Top.zhl"
    text = "module Top { in a:u8 out y:u9 y=a+1 }\n"
    source.write_text(text, encoding="utf-8")
    uri = path_to_uri(source)
    incoming = BytesIO()
    _request(incoming, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    _request(incoming, {
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    _request(incoming, {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "textDocument/hover",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 0, "character": text.index("a")},
        },
    })
    _request(incoming, {"jsonrpc": "2.0", "id": 3, "method": "shutdown"})
    _request(incoming, {"jsonrpc": "2.0", "method": "exit"})
    incoming.seek(0)
    outgoing = BytesIO()
    assert LspServer().run(incoming, outgoing) == 0
    messages = _messages(outgoing.getvalue())
    assert messages[0]["id"] == 1
    assert messages[1]["id"] == 2
    assert messages[1]["result"]["contents"]["value"].startswith("a : u8")
    assert messages[2]["method"] == "textDocument/publishDiagnostics"
    assert messages[3] == {"jsonrpc": "2.0", "id": 3, "result": None}


def test_framed_live_change_burst_compiles_only_the_latest_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zlang.tooling as tooling

    source = tmp_path / "Top.zhl"
    source.write_text(VALID, encoding="utf-8")
    uri = path_to_uri(source)
    original = tooling.check_file_snapshot
    compilations = 0

    def observed(*args: object, **kwargs: object) -> object:
        nonlocal compilations
        compilations += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    incoming = BytesIO()
    _request(incoming, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    _request(incoming, {
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {
            "uri": uri,
            "version": 1,
            "text": VALID,
        }},
    })
    for version, text in (
        (2, "module Top {"),
        (3, "module Top { out y:u8"),
        (4, VALID),
    ):
        _request(incoming, {
            "jsonrpc": "2.0",
            "method": "textDocument/didChange",
            "params": {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            },
        })
    _request(incoming, {"jsonrpc": "2.0", "id": 2, "method": "shutdown"})
    _request(incoming, {"jsonrpc": "2.0", "method": "exit"})
    incoming.seek(0)
    outgoing = BytesIO()
    assert LspServer().run(incoming, outgoing) == 0
    messages = _messages(outgoing.getvalue())
    published = [
        item for item in messages
        if item.get("method") == "textDocument/publishDiagnostics"
    ]
    assert len(published) == 1
    assert published[0]["params"] == {"uri": uri, "diagnostics": []}
    assert compilations == 1


def test_definition_request_resolves_port_from_current_unsaved_text(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    text = (
        "module Top {\n"
        "    in a:u8\n"
        "    out y:u9\n"
        "    y = a + 1\n"
        "}\n"
    )
    source.write_text("module Top { out stale:u8 stale=1 }\n", encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 4,
        "method": "textDocument/definition",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 3, "character": text.splitlines()[3].index("a")},
        },
    })
    location = response[0]["result"]
    assert location["uri"] == uri
    assert location["range"]["start"] == {"line": 1, "character": 7}


def test_definition_request_resolves_function_and_rejects_unknown_or_closed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    text = (
        "fn add(x:u8) -> u9 { x + 1 }\n"
        "module Top { out y:u9 y=add(1) }\n"
    )
    source.write_text(text, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    call_start = text.splitlines()[1].index("add")
    response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 5,
        "method": "textDocument/definition",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 1, "character": call_start},
        },
    })
    assert response[0]["result"]["uri"] == uri
    assert response[0]["result"]["range"]["start"] == {"line": 0, "character": 3}
    whitespace = server.dispatch({
        "jsonrpc": "2.0",
        "id": 6,
        "method": "textDocument/definition",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 1, "character": 0},
        },
    })
    assert whitespace[0]["result"] is None
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didClose",
        "params": {"textDocument": {"uri": uri}},
    })
    closed = server.dispatch({
        "jsonrpc": "2.0",
        "id": 7,
        "method": "textDocument/definition",
        "params": {"textDocument": {"uri": uri}, "position": {"line": 1, "character": call_start}},
    })
    assert closed[0]["error"]["code"] == -32602


def test_definition_request_resolves_80211a_module_instance_targets() -> None:
    project = Path("examples/projects/80211a_transmitter").resolve()
    source = project / "src/transmitter.zhl"
    text = source.read_text(encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })

    expected = {
        "IeeePacketMapper64": (project / "src/mapper.zhl", 357, 7),
        "IeeeFramedIFFT64": (project / "src/ifft.zhl", 167, 7),
        "IeeeIFFTFramedOutputBoundary": (project / "src/ifft.zhl", 59, 7),
    }
    for name, (target_path, target_line, target_character) in expected.items():
        line = next(
            index for index, value in enumerate(text.splitlines()) if name in value
        )
        response = server.dispatch({
            "jsonrpc": "2.0",
            "id": name,
            "method": "textDocument/definition",
            "params": {
                "textDocument": {"uri": uri},
                "position": {
                    "line": line,
                    "character": text.splitlines()[line].index(name),
                },
            },
        })
        location = response[0]["result"]
        assert location["uri"] == path_to_uri(target_path)
        assert location["range"]["start"] == {
            "line": target_line,
            "character": target_character,
        }


def test_definition_request_resolves_80211a_named_type_in_unopened_dependency() -> None:
    project = Path("examples/projects/80211a_transmitter").resolve()
    source = project / "src/transmitter.zhl"
    text = source.read_text(encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })

    line = next(
        index for index, value in enumerate(text.splitlines())
        if "command : rv<WifiTxCommand>" in value
    )
    response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 21,
        "method": "textDocument/definition",
        "params": {
            "textDocument": {"uri": uri},
            "position": {
                "line": line,
                "character": text.splitlines()[line].index("WifiTxCommand"),
            },
        },
    })
    location = response[0]["result"]
    assert location["uri"] == path_to_uri(project / "src/data_types.zhl")
    assert location["range"]["start"] == {"line": 11, "character": 7}


def test_definition_request_converts_utf16_character_to_compiler_column(
    tmp_path: Path,
) -> None:
    source = (tmp_path / "utf16_definition.zhl").resolve()
    text = "module Top { in a:u8 out z:u8 = /* 😀 */ a }\n"
    source.write_text(text, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })

    use = text.rindex("a")
    lsp_character = len(text[:use].encode("utf-16-le")) // 2
    response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 22,
        "method": "textDocument/definition",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 0, "character": lsp_character},
        },
    })
    location = response[0]["result"]
    assert location["uri"] == uri
    assert location["range"]["start"] == {"line": 0, "character": 16}


def test_framed_definition_resolves_nested_project_from_parent_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror live F12 at a selected symbol's exclusive right edge."""

    import zlang.tooling as tooling

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("ZLANG_LSP_SYMBOL_CACHE", "persistent")
    original = tooling.check_file_snapshot
    definition_compilations = 0

    def observed(*args: object, **kwargs: object) -> object:
        nonlocal definition_compilations
        needs = AnalysisNeeds(kwargs.get("analysis_needs", AnalysisNeeds.NONE))
        if needs.wants(AnalysisNeeds.DEFINITIONS):
            definition_compilations += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)

    repository = Path.cwd().resolve()
    project = repository / "examples/projects/80211a_transmitter"
    source = project / "src/transmitter.zhl"
    text = source.read_text(encoding="utf-8")
    uri = path_to_uri(source)
    lines = text.splitlines()
    expected = {
        "IeeePacketMapper64": (project / "src/mapper.zhl", 357, 7),
        "WifiTxCommand": (project / "src/data_types.zhl", 11, 7),
    }

    for workspace in (repository, project):
        incoming = BytesIO()
        _request(incoming, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "rootUri": path_to_uri(workspace),
                "workspaceFolders": [{
                    "uri": path_to_uri(workspace),
                    "name": workspace.name,
                }],
            },
        })
        _request(incoming, {"jsonrpc": "2.0", "method": "initialized"})
        _request(incoming, {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": uri,
                    "version": 1,
                    "text": text,
                },
            },
        })
        for request_id, (name, _) in enumerate(expected.items(), start=2):
            line = next(index for index, value in enumerate(lines) if name in value)
            _request(incoming, {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "textDocument/definition",
                "params": {
                    "textDocument": {"uri": uri},
                    "position": {
                    "line": line,
                    "character": lines[line].index(name) + len(name),
                },
                },
            })
        connection_line = next(
            index for index, value in enumerate(lines)
            if "command -> packet_mapper.command" in value
        )
        _request(incoming, {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "textDocument/definition",
            "params": {
                "textDocument": {"uri": uri},
                "position": {
                    "line": connection_line,
                    "character": lines[connection_line].index("command")
                    + len("command"),
                },
            },
        })
        _request(incoming, {"jsonrpc": "2.0", "id": 5, "method": "shutdown"})
        _request(incoming, {"jsonrpc": "2.0", "method": "exit"})
        incoming.seek(0)
        outgoing = BytesIO()
        assert LspServer().run(incoming, outgoing) == 0
        responses = {
            message["id"]: message
            for message in _messages(outgoing.getvalue())
            if "id" in message
        }
        for request_id, (_, (target_path, line, character)) in enumerate(
            expected.items(), start=2
        ):
            location = responses[request_id]["result"]
            assert location["uri"] == path_to_uri(target_path)
            assert location["range"]["start"] == {
                "line": line,
                "character": character,
            }
        connection_location = responses[4]["result"]
        assert connection_location["uri"] == uri
        assert connection_location["range"]["start"] == {
            "line": 13,
            "character": 7,
        }
    assert definition_compilations == 1


def test_nested_project_unsaved_instance_spelling_uses_editor_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``inst`` and concise declarations are equivalent in live buffers."""

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    source = Path(
        "examples/projects/80211a_transmitter/src/transmitter.zhl"
    ).resolve()
    disk_text = source.read_text(encoding="utf-8")
    text = disk_text.replace(
        "    packet_mapper : IeeePacketMapper64",
        "    inst packet_mapper : IeeePacketMapper64",
        1,
    )
    assert text != disk_text
    uri = path_to_uri(source)
    server = LspServer()

    opened = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": uri,
                "version": 1,
                "text": text,
            },
        },
    })
    assert opened[0]["params"]["diagnostics"] == []

    lines = text.splitlines()
    declaration_line = next(
        index for index, value in enumerate(lines)
        if "IeeePacketMapper64" in value
    )
    completion = server.dispatch({
        "jsonrpc": "2.0",
        "id": 50,
        "method": "textDocument/completion",
        "params": {
            "textDocument": {"uri": uri},
            "position": {
                "line": declaration_line,
                "character": len(lines[declaration_line]),
            },
        },
    })
    assert completion == [{"jsonrpc": "2.0", "id": 50, "result": []}]

    definition = server.dispatch({
        "jsonrpc": "2.0",
        "id": 51,
        "method": "textDocument/definition",
        "params": {
            "textDocument": {"uri": uri},
            "position": {
                "line": declaration_line,
                "character": lines[declaration_line].index("IeeePacketMapper64")
                + len("IeeePacketMapper64"),
            },
        },
    })
    location = definition[0]["result"]
    assert location["uri"] == path_to_uri(source.parent / "mapper.zhl")
    assert location["range"]["start"] == {"line": 357, "character": 7}

    concise = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": disk_text}],
        },
    })
    assert concise[0]["params"]["diagnostics"] == []
    concise_lines = disk_text.splitlines()
    concise_line = next(
        index for index, value in enumerate(concise_lines)
        if "IeeePacketMapper64" in value
    )
    concise_completion = server.dispatch({
        "jsonrpc": "2.0",
        "id": 52,
        "method": "textDocument/completion",
        "params": {
            "textDocument": {"uri": uri},
            "position": {
                "line": concise_line,
                "character": len(concise_lines[concise_line]),
            },
        },
    })
    assert concise_completion == [
        {"jsonrpc": "2.0", "id": 52, "result": []}
    ]

    invalid = disk_text.replace("packet_mapper.command", "missing.command", 1)
    changed = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": {"uri": uri, "version": 3},
            "contentChanges": [{"text": invalid}],
        },
    })
    diagnostic = changed[0]["params"]["diagnostics"][0]
    invalid_lines = invalid.splitlines()
    error_line = next(
        index for index, value in enumerate(invalid_lines)
        if "command -> missing.command" in value
    )
    start = invalid_lines[error_line].index("missing")
    assert diagnostic["code"] == "ZL-SEMANTIC-001"
    assert diagnostic["message"] == (
        "unknown hierarchical protocol instance 'missing'"
    )
    assert diagnostic["range"] == {
        "start": {"line": error_line, "character": start},
        "end": {"line": error_line, "character": start + len("missing")},
    }
    assert source.read_text(encoding="utf-8") == disk_text


def test_open_project_dependency_and_root_share_one_unsaved_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zlang.workspace import update_project_lock

    cache_root = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    project = tmp_path / "demo"
    sources = project / "src"
    sources.mkdir(parents=True)
    (project / "zlang.toml").write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n',
        encoding="utf-8",
    )
    declarations = sources / "types.zhl"
    root = sources / "top.zhl"
    declarations.write_text("struct Shared { value:u8 }\n", encoding="utf-8")
    root.write_text(
        "import demo.types\nmodule Top { in x:Shared out y:u8 y=x.value }\n",
        encoding="utf-8",
    )
    update_project_lock(project / "zlang.toml")

    declarations_text = "struct Renamed { value:u8 }\n"
    root_text = (
        "import demo.types\n"
        "module Top { in x:Renamed out y:u8 y=x.value }\n"
    )
    declarations_uri = path_to_uri(declarations)
    root_uri = path_to_uri(root)
    server = LspServer()
    opened_declarations = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {
            "uri": declarations_uri,
            "version": 1,
            "text": declarations_text,
        }},
    })
    assert opened_declarations[0]["params"]["diagnostics"] == []
    opened_root = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {
            "uri": root_uri,
            "version": 1,
            "text": root_text,
        }},
    })
    assert opened_root[0]["params"]["diagnostics"] == []

    line = 1
    character = root_text.splitlines()[line].index("Renamed")
    definition = server.dispatch({
        "jsonrpc": "2.0",
        "id": 90,
        "method": "textDocument/definition",
        "params": {
            "textDocument": {"uri": root_uri},
            "position": {"line": line, "character": character},
        },
    })
    assert definition[0]["result"] == {
        "uri": declarations_uri,
        "range": {
            "start": {"line": 0, "character": 7},
            "end": {"line": 0, "character": 14},
        },
    }
    assert declarations.read_text(encoding="utf-8").startswith("struct Shared")
    assert "Shared" in root.read_text(encoding="utf-8")

    references = server.dispatch({
        "jsonrpc": "2.0",
        "id": 901,
        "method": "textDocument/references",
        "params": {
            "textDocument": {"uri": root_uri},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": True},
        },
    })[0]["result"]
    assert {(item["uri"], item["range"]["start"]["line"]) for item in references} == {
        (declarations_uri, 0),
        (root_uri, 1),
    }

    invalid_declarations = "struct Renamed { value: }\n"
    changed = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": {"uri": declarations_uri, "version": 2},
            "contentChanges": [{"text": invalid_declarations}],
        },
    })
    assert changed[0]["params"]["uri"] == declarations_uri
    assert changed[0]["params"]["diagnostics"][0]["code"] == "ZL-PARSE-001"
    assert changed[0]["params"]["diagnostics"][0]["range"]["start"] != {
        "line": 0,
        "character": 0,
    }

    routed = server._publish_document(server.documents[root_uri])
    assert routed[0] == {
        "jsonrpc": "2.0",
        "method": "textDocument/publishDiagnostics",
        "params": {"uri": root_uri, "diagnostics": []},
    }
    assert routed[1]["params"]["uri"] == declarations_uri
    assert routed[1]["params"]["diagnostics"][0]["code"] == "ZL-PARSE-001"
    completion = server.dispatch({
        "jsonrpc": "2.0",
        "id": 91,
        "method": "textDocument/completion",
        "params": {
            "textDocument": {"uri": root_uri},
            "position": {"line": line, "character": character},
        },
    })
    assert completion[-1] == {"jsonrpc": "2.0", "id": 91, "result": []}
    assert not tuple((cache_root / "zlang-hdl/lsp").rglob("*.json"))
    assert load_parse_index(
        "demo.top", hashlib.sha256(root_text.encode()).hexdigest()
    ) is None
    assert load_parse_index(
        "demo.types", hashlib.sha256(invalid_declarations.encode()).hexdigest()
    ) is None


def test_workspace_failure_is_not_a_fake_source_diagnostic(
    tmp_path: Path,
) -> None:
    project = tmp_path / "broken"
    sources = project / "src"
    sources.mkdir(parents=True)
    (project / "zlang.toml").write_text(
        'schema=1\n[project]\nname="broken"\nversion="1"\nsource-root="src"\n',
        encoding="utf-8",
    )
    source = sources / "top.zhl"
    source.write_text(VALID, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    messages = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {
            "uri": uri,
            "version": 1,
            "text": VALID,
        }},
    })
    assert messages[0] == {
        "jsonrpc": "2.0",
        "method": "textDocument/publishDiagnostics",
        "params": {"uri": uri, "diagnostics": []},
    }
    assert [item["method"] for item in messages[1:]] == [
        "window/logMessage",
        "window/showMessage",
    ]
    assert all("zlang.lock" in item["params"]["message"] for item in messages[1:])

    completion = server.dispatch({
        "jsonrpc": "2.0",
        "id": 92,
        "method": "textDocument/completion",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 0, "character": 0},
        },
    })
    assert completion == [{"jsonrpc": "2.0", "id": 92, "result": []}]


def test_definition_resolves_nested_generic_types_and_stdlib_sources(
    tmp_path: Path,
) -> None:
    """Resolve nested generic names in a self-contained locked project."""

    from zlang.workspace import update_project_lock

    project = tmp_path / "demo"
    source_root = project / "src"
    source_root.mkdir(parents=True)
    (project / "zlang.toml").write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n',
        encoding="utf-8",
    )
    data_types = source_root / "data_types.zhl"
    data_text = "struct PayloadFrameMeta { first:bit }\n"
    data_types.write_text(data_text, encoding="utf-8")
    source = source_root / "top.zhl"
    text = (
        "import std.stream.core\n"
        "import demo.data_types\n"
        "module Top {\n"
        "  in beat_in : rv<FrameBeat<bits<256>, PayloadFrameMeta>>\n"
        "  out y : bit\n"
        "  beat_in.ready = 1\n"
        "  y = beat_in.valid\n"
        "}\n"
    )
    source.write_text(text, encoding="utf-8")
    update_project_lock(project / "zlang.toml")
    uri = path_to_uri(source)
    lines = text.splitlines()
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })

    expected = {
        "FrameBeat": (Path("stdlib/stream/core.zhl").resolve(), 2, 7),
        "PayloadFrameMeta": (data_types, 0, data_text.index("PayloadFrameMeta")),
    }
    for request_id, (name, (target, target_line, target_column)) in enumerate(
        expected.items(), start=40
    ):
        line = next(index for index, value in enumerate(lines) if name in value)
        start = lines[line].index(name)
        response = server.dispatch({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "textDocument/definition",
            "params": {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": start + len(name)},
            },
        })
        location = response[0]["result"]
        assert location["uri"] == path_to_uri(target)
        assert location["range"]["start"] == {
            "line": target_line,
            "character": target_column,
        }


def test_opening_generic_only_stdlib_source_has_no_false_type_diagnostic() -> None:
    source = Path("stdlib/stream/core.zhl").resolve()
    text = source.read_text(encoding="utf-8")
    compiler_record = check_snapshot(source, text)
    assert compiler_record.status == "failed"
    assert compiler_record.diagnostics[0].code == (
        "ZL-GENERIC-SPECIALIZATION-REQUIRED"
    )
    response = LspServer().dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": path_to_uri(source),
                "version": 1,
                "text": text,
            },
        },
    })
    assert response[0]["params"]["diagnostics"] == []


def test_storage_library_definition_uses_enclosing_module_not_unbound_last_top(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live stdlib F12 request must resolve through semantic occurrences."""

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    source = Path("stdlib/storage/core.zhl").resolve()
    text = source.read_text(encoding="utf-8")
    record = check_snapshot(source, text)
    assert record.status == "failed"
    assert record.diagnostics[0].code == "ZL-SEMANTIC-PARAMETER-CONSTRAINT"
    uri = path_to_uri(source)
    server = LspServer()
    opened = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    assert opened[0]["params"]["diagnostics"] == []
    line = next(
        index for index, value in enumerate(text.splitlines())
        if "out status : StoragePingPongStatus<W>" in value
    )
    column = text.splitlines()[line].index("StoragePingPongStatus")
    from zlang import tooling

    compiler_calls = 0
    original = tooling.check_file_snapshot

    def observed(*args: object, **kwargs: object) -> object:
        nonlocal compiler_calls
        compiler_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    request = {
        "jsonrpc": "2.0",
        "id": 77,
        "method": "textDocument/definition",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": column},
        },
    }
    location = server.dispatch(request)[0]["result"]
    assert location == {
        "uri": uri,
        "range": {
            "start": {"line": 120, "character": 7},
            "end": {"line": 120, "character": 28},
        },
    }
    assert server.dispatch(request)[0]["result"] == location
    assert compiler_calls == 2  # failed unspecialized last top, then enclosing top

    changed = text.replace(
        "out status : StoragePingPongStatus<W>",
        "out status : MissingPingPongStatus<W>",
    )
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": changed}],
        },
    })
    changed_request = {
        **request,
        "params": {
            "textDocument": {"uri": uri},
            "position": {
                "line": line,
                "character": changed.splitlines()[line].index(
                    "MissingPingPongStatus"
                ),
            },
        },
    }
    assert server.dispatch(changed_request)[0]["result"] is None


def test_all_syntax_resource_names_have_semantic_definition_and_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live F12 on `require_resource` must use exact compiler records."""

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    source = Path("examples/all_syntax.zhl").resolve()
    text = source.read_text(encoding="utf-8")
    uri = path_to_uri(source)
    lines = text.splitlines()
    declaration_line = next(
        index for index, value in enumerate(lines)
        if value.startswith("resource CorpusLogic {")
    )
    use_lines = tuple(
        index for index, value in enumerate(lines)
        if "provides CorpusLogic" in value
        or "require_resource CorpusLogic" in value
    )
    assert len(use_lines) == 2
    server = LspServer()
    opened = server.dispatch({
        "jsonrpc": "2.0", "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    assert opened[0]["params"]["diagnostics"] == []
    target = {
        "uri": uri,
        "range": {
            "start": {
                "line": declaration_line,
                "character": lines[declaration_line].index("CorpusLogic"),
            },
            "end": {
                "line": declaration_line,
                "character": lines[declaration_line].index("CorpusLogic")
                + len("CorpusLogic"),
            },
        },
    }
    for line in use_lines:
        column = lines[line].index("CorpusLogic")
        response = server.dispatch({
            "jsonrpc": "2.0", "id": line,
            "method": "textDocument/definition",
            "params": {
                "textDocument": {"uri": uri},
                "position": {
                    "line": line,
                    "character": column + len("CorpusLogic"),
                },
            },
        })
        assert response[0]["result"] == target
    references = server.dispatch({
        "jsonrpc": "2.0", "id": 99,
        "method": "textDocument/references",
        "params": {
            "textDocument": {"uri": uri},
            "position": {
                "line": declaration_line,
                "character": lines[declaration_line].index("CorpusLogic"),
            },
            "context": {"includeDeclaration": True},
        },
    })[0]["result"]
    assert [item["range"]["start"]["line"] for item in references] == [
        declaration_line, *use_lines,
    ]


def test_all_syntax_enum_references_include_other_same_file_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shift+F12 on a declaration must search semantic sibling-module uses."""

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    source = Path("examples/all_syntax.zhl").resolve()
    text = source.read_text(encoding="utf-8")
    lines = text.splitlines()
    uri = path_to_uri(source)
    declaration_line = next(
        index for index, value in enumerate(lines)
        if value.startswith("enum CorpusCode :")
    )
    use_lines = tuple(
        index for index, value in enumerate(lines)
        if "enum_decode<CorpusCode>" in value
        or "enum_valid<CorpusCode>" in value
    )
    assert len(use_lines) == 2
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0", "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    expected = []
    for line in use_lines:
        start = 0
        while (column := lines[line].find("CorpusCode", start)) >= 0:
            expected.append((line, column))
            start = column + len("CorpusCode")
    # Three occurrences in enum_decode's line and one in enum_valid's line.
    assert len(expected) == 4
    for request_line, column in (
        (declaration_line, lines[declaration_line].index("CorpusCode")),
        (use_lines[0], lines[use_lines[0]].index("CorpusCode")),
    ):
        response = server.dispatch({
            "jsonrpc": "2.0", "id": request_line,
            "method": "textDocument/references",
            "params": {
                "textDocument": {"uri": uri},
                "position": {"line": request_line, "character": column},
                "context": {"includeDeclaration": False},
            },
        })[0]
        assert "error" not in response
        assert [
            (item["range"]["start"]["line"], item["range"]["start"]["character"])
            for item in response["result"]
        ] == expected
        assert all(
            item["range"]["end"]["character"]
            - item["range"]["start"]["character"] == len("CorpusCode")
            for item in response["result"]
        )
    with_declaration = server.dispatch({
        "jsonrpc": "2.0", "id": 99,
        "method": "textDocument/references",
        "params": {
            "textDocument": {"uri": uri},
            "position": {
                "line": declaration_line,
                "character": lines[declaration_line].index("CorpusCode"),
            },
            "context": {"includeDeclaration": True},
        },
    })[0]["result"]
    assert len(with_declaration) == 5
    assert with_declaration[0]["range"]["start"]["line"] == declaration_line


@pytest.mark.parametrize(
    ("relative", "symbol", "needle", "target", "target_line", "count"),
    (
        ("examples/all_syntax.zhl", "CorpusCode", "enum_decode<CorpusCode>",
         "all_syntax.zhl", 25, 5),
        ("stdlib/storage/core.zhl", "StoragePingPongStatus",
             "out status : StoragePingPongStatus", "core.zhl", 120, 3),
        ("examples/projects/80211a_transmitter/src/transmitter.zhl",
         "WifiTxCommand", "in command : rv<WifiTxCommand>", "data_types.zhl", 11, 6),
    ),
)
def test_navigation_matrix_definition_and_references_share_semantic_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    relative: str, symbol: str, needle: str,
    target: str, target_line: int, count: int,
) -> None:
    """Exercise both JSON-RPC methods on real nested and multi-top sources."""

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    source = Path(relative).resolve()
    text = source.read_text(encoding="utf-8")
    uri = path_to_uri(source)
    line = next(
        index for index, value in enumerate(text.splitlines())
        if needle in value
    )
    column = text.splitlines()[line].index(symbol)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0", "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    position = {"line": line, "character": column}
    definition_request = {
        "jsonrpc": "2.0", "id": 1, "method": "textDocument/definition",
        "params": {"textDocument": {"uri": uri}, "position": position},
    }
    reference_request = {
        "jsonrpc": "2.0", "id": 2, "method": "textDocument/references",
        "params": {
            "textDocument": {"uri": uri}, "position": position,
            "context": {"includeDeclaration": True},
        },
    }
    definition = server.dispatch(definition_request)[0]["result"]
    assert definition is not None
    assert Path(uri_to_path(definition["uri"])).name == target
    assert definition["range"]["start"]["line"] == target_line
    references = server.dispatch(reference_request)[0]["result"]
    assert len(references) == count
    assert definition in references
    assert any(
        item["uri"] == uri and item["range"]["start"]["line"] == line
        and item["range"]["start"]["character"] == column
        for item in references
    )
    assert server.dispatch(definition_request)[0]["result"] == definition
    assert server.dispatch(reference_request)[0]["result"] == references


def test_resource_navigation_does_not_guess_ambiguous_or_unknown_names(
    tmp_path: Path,
) -> None:
    from zlang.tooling import definition_at

    for name, declarations in (
        ("Unknown", ""),
        (
            "Logic",
            "resource Logic { operation boolean }\n"
            "resource Logic { operation boolean }\n",
        ),
    ):
        source = tmp_path / f"{name}.zhl"
        text = (
            declarations
            + f"target Demo {{ provides {name} }}\n"
            + "module Top { out y:u8 y=1 }\n"
        )
        source.write_text(text, encoding="utf-8")
        line = next(
            index for index, value in enumerate(text.splitlines())
            if "provides" in value
        )
        assert definition_at(
            source, text, line, text.splitlines()[line].index(name)
        ) is None


def test_concrete_parameter_constraint_failure_remains_editor_diagnostic(
    tmp_path: Path,
) -> None:
    source = tmp_path / "invalid.zhl"
    text = "module Invalid<N=3> where is_power_of_two(N) { out y:u8 y=1 }\n"
    source.write_text(text, encoding="utf-8")
    response = LspServer().dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": path_to_uri(source), "version": 1, "text": text,
            },
        },
    })
    assert response[0]["params"]["diagnostics"][0]["code"] == (
        "ZL-SEMANTIC-PARAMETER-CONSTRAINT"
    )


def test_child_module_boundary_is_checked_as_child_for_diagnostics_and_definition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legal enum-bearing child port is not a public-top ABI diagnostic."""

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    source = Path(
        "examples/projects/80211a_transmitter/src/scrambler.zhl"
    ).resolve()
    text = source.read_text(encoding="utf-8")
    from zlang.compiler import check_file_snapshot
    from zlang.semantic import SemanticError

    with pytest.raises(
        SemanticError, match="top-level input 'input' cannot expose type"
    ):
        check_file_snapshot(source, text)
    uri = path_to_uri(source)
    server = LspServer()
    opened = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": uri,
                "version": 1,
                "text": text,
            },
        },
    })
    assert opened[0]["params"]["diagnostics"] == []
    line = next(
        index for index, value in enumerate(text.splitlines())
        if "in input : rv<WifiFramedWord>" in value
    )
    response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 45,
        "method": "textDocument/definition",
        "params": {
            "textDocument": {"uri": uri},
            "position": {
                "line": line,
                "character": text.splitlines()[line].index("WifiFramedWord")
                + len("WifiFramedWord"),
            },
        },
    })
    location = response[0]["result"]
    assert location["uri"] == path_to_uri(
        source.parent / "data_types.zhl"
    )
    assert location["range"]["start"] == {"line": 47, "character": 5}


def test_generic_source_still_reports_parser_errors() -> None:
    source = Path("stdlib/stream/core.zhl").resolve()
    text = source.read_text(encoding="utf-8").replace("data : T", "data T", 1)
    response = LspServer().dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": path_to_uri(source),
                "version": 1,
                "text": text,
            },
        },
    })
    diagnostics = response[0]["params"]["diagnostics"]
    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "ZL-PARSE-001"


def test_definition_request_resolves_local_register_at_symbol_end(
    tmp_path: Path,
) -> None:
    source = (tmp_path / "register_read.zhl").resolve()
    text = (
        "module RegisterRead {\n"
        "  clock clk reset rst\n"
        "  reg lfsr_prbs : bits<7> = 0x7f\n"
        "  out y : bit\n"
        "  y = lfsr_prbs[0]\n"
        "}\n"
    )
    source.write_text(text, encoding="utf-8")
    lines = text.splitlines()
    use_line = next(
        index for index, line in enumerate(lines) if "y = lfsr_prbs" in line
    )
    name = "lfsr_prbs"
    character = lines[use_line].index(name) + len(name)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": path_to_uri(source),
                "version": 1,
                "text": text,
            },
        },
    })
    response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "textDocument/definition",
        "params": {
            "textDocument": {"uri": path_to_uri(source)},
            "position": {"line": use_line, "character": character},
        },
    })
    location = response[0]["result"]
    assert location["uri"] == path_to_uri(source)
    assert location["range"]["start"] == {
        "line": 2,
        "character": lines[2].index(name),
    }


def test_definition_request_resolves_register_rule_update_target(
    tmp_path: Path,
) -> None:
    """A scalar ``<-`` target is a compiler-resolved register occurrence."""

    source = (tmp_path / "register_update.zhl").resolve()
    text = (
        "module RegisterUpdate {\n"
        "  clock clk reset rst\n"
        "  reg par_idx : u8 = 0\n"
        "  when 1 {\n"
        "    par_idx <- 0\n"
        "  }\n"
        "  out y : u8\n"
        "  y = par_idx\n"
        "}\n"
    )
    source.write_text(text, encoding="utf-8")
    lines = text.splitlines()
    declaration_line = next(
        index for index, line in enumerate(lines) if "reg par_idx" in line
    )
    use_line = next(
        index for index, line in enumerate(lines) if "par_idx <-" in line
    )
    name = "par_idx"
    character = lines[use_line].index(name) + len(name)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": path_to_uri(source),
                "version": 1,
                "text": text,
            },
        },
    })
    response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "textDocument/definition",
        "params": {
            "textDocument": {"uri": path_to_uri(source)},
            "position": {"line": use_line, "character": character},
        },
    })
    location = response[0]["result"]
    assert location["uri"] == path_to_uri(source)
    assert location["range"]["start"] == {
        "line": declaration_line,
        "character": lines[declaration_line].index(name),
    }


def test_definition_request_resolves_enum_type_and_member(tmp_path: Path) -> None:
    source = (tmp_path / "enum_definition.zhl").resolve()
    text = (
        "enum TxState { Idle Active }\n"
        "module Top { out state:TxState out y:u8 "
        "state=TxState.Idle y=0 }\n"
    )
    source.write_text(text, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })

    line = 1
    for request_id, name, target in (
        (22, "TxState", {"line": 0, "character": 5}),
        (23, "Idle", {"line": 0, "character": 15}),
    ):
        response = server.dispatch({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "textDocument/definition",
            "params": {
                "textDocument": {"uri": uri},
                "position": {
                    "line": line,
                    "character": text.splitlines()[line].index(name),
                },
            },
        })
        assert response[0]["result"]["uri"] == uri
        assert response[0]["result"]["range"]["start"] == target


def test_references_request_respects_include_declaration_and_unsaved_text(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    text = (
        "module Top {\n"
        "    in a:u8\n"
        "    out y:u9\n"
        "    y = a + 1\n"
        "}\n"
    )
    source.write_text("module Top { out stale:u8 stale=1 }\n", encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    position = text.splitlines()[3].index("a")
    without = server.dispatch({
        "jsonrpc": "2.0",
        "id": 8,
        "method": "textDocument/references",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 3, "character": position},
            "context": {"includeDeclaration": False},
        },
    })
    with_declaration = server.dispatch({
        "jsonrpc": "2.0",
        "id": 9,
        "method": "textDocument/references",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 3, "character": position},
            "context": {"includeDeclaration": True},
        },
    })
    assert len(without[0]["result"]) == 1
    assert len(with_declaration[0]["result"]) == 2
    assert with_declaration[0]["result"][0]["range"]["start"] == {
        "line": 1,
        "character": 7,
    }
    invalid_context = server.dispatch({
        "jsonrpc": "2.0",
        "id": 10,
        "method": "textDocument/references",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 3, "character": position},
            "context": {"includeDeclaration": "yes"},
        },
    })
    assert invalid_context[0]["error"]["code"] == -32602


def test_references_request_resolves_cross_file_calls_and_empty_positions(
    tmp_path: Path,
) -> None:
    from zlang.workspace import update_project_lock

    root = tmp_path / "demo"
    source_root = root / "src"
    source_root.mkdir(parents=True)
    (root / "zlang.toml").write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n',
        encoding="utf-8",
    )
    dependency = source_root / "dep.zhl"
    dependency.write_text("fn inc(x:u8) -> u9 { x + 1 }\n", encoding="utf-8")
    source = source_root / "top.zhl"
    text = (
        "import demo.dep\n"
        "module Top { in a:u8 out y:u10 y=inc(a)+inc(a) }\n"
    )
    source.write_text(text, encoding="utf-8")
    update_project_lock(root / "zlang.toml")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    position = text.splitlines()[1].index("inc")
    response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 11,
        "method": "textDocument/references",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 1, "character": position},
            "context": {"includeDeclaration": True},
        },
    })
    assert len(response[0]["result"]) == 3
    assert response[0]["result"][0]["uri"] == path_to_uri(dependency)
    assert response[0]["result"][1]["uri"] == uri
    closed = server.dispatch({
        "jsonrpc": "2.0",
        "id": 12,
        "method": "textDocument/references",
        "params": {
            "textDocument": {"uri": path_to_uri(tmp_path / "missing.zhl")},
            "position": {"line": 0, "character": 0},
            "context": {"includeDeclaration": False},
        },
    })
    assert closed[0]["error"]["code"] == -32602


def test_references_request_collects_real_project_type_and_module_declaration(
    tmp_path: Path,
) -> None:
    type_source = Path(
        "examples/projects/80211a_transmitter/src/data_types.zhl"
    ).resolve()
    type_text = type_source.read_text(encoding="utf-8")
    type_lines = type_text.splitlines()
    type_line = next(
        index for index, item in enumerate(type_lines)
        if "struct WifiSampleMeta" in item
    )
    type_position = type_lines[type_line].index("WifiSampleMeta") + len(
        "WifiSampleMeta"
    )

    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": path_to_uri(type_source),
                "version": 1,
                "text": type_text,
            }
        },
    })
    type_response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 31,
        "method": "textDocument/references",
        "params": {
            "textDocument": {"uri": path_to_uri(type_source)},
            "position": {"line": type_line, "character": type_position},
            "context": {"includeDeclaration": True},
        },
    })
    assert "error" not in type_response[0]
    assert [
        (Path(uri_to_path(item["uri"])).name, item["range"]["start"]["line"])
        for item in type_response[0]["result"]
    ] == [
        ("data_types.zhl", 26),
        ("ifft.zhl", 46),
        ("ifft.zhl", 135),
        ("mapper.zhl", 104),
        ("mapper.zhl", 297),
        ("mapper.zhl", 344),
    ]

    module_source = (tmp_path / "unused_module.zhl").resolve()
    module_text = "module UnusedModule { out y:u8 y=1 }\n"
    module_source.write_text(module_text, encoding="utf-8")
    module_lines = module_text.splitlines()
    module_line = next(
        index for index, item in enumerate(module_lines)
        if "module UnusedModule" in item
    )
    module_position = module_lines[module_line].index(
        "UnusedModule"
    ) + len("UnusedModule")
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": path_to_uri(module_source),
                "version": 1,
                "text": module_text,
            }
        },
    })
    module_response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 32,
        "method": "textDocument/references",
        "params": {
            "textDocument": {"uri": path_to_uri(module_source)},
            "position": {"line": module_line, "character": module_position},
            "context": {"includeDeclaration": True},
        },
    })
    assert module_response[0]["result"] == [{
        "uri": path_to_uri(module_source),
        "range": {
            "start": {"line": 0, "character": 7},
            "end": {"line": 0, "character": 19},
        },
    }]


def test_rename_request_returns_exact_workspace_edit_from_unsaved_text(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    text = "module Top { in a:u8 out y:u9 y=a+1 }\n"
    source.write_text("module Top { out stale:u8 stale=1 }\n", encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 20,
        "method": "textDocument/rename",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 0, "character": text.rfind("a")},
            "newName": "input_value",
        },
    })
    result = response[0]["result"]
    assert list(result["changes"]) == [uri]
    edits = result["changes"][uri]
    assert [edit["range"] for edit in edits] == [
        {
            "start": {"line": 0, "character": 16},
            "end": {"line": 0, "character": 17},
        },
        {
            "start": {"line": 0, "character": 32},
            "end": {"line": 0, "character": 33},
        },
    ]
    assert all(edit["newText"] == "input_value" for edit in edits)


def test_rename_request_rejects_collision_invalid_name_and_unknown_document(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    text = (
        "fn add(a:u8, b:u8) -> u9 { a + b }\n"
        "module Top { out y:u9 y=add(1, 2) }\n"
    )
    source.write_text(text, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    position = text.splitlines()[0].index("a", text.splitlines()[0].index("{"))
    collision = server.dispatch({
        "jsonrpc": "2.0",
        "id": 21,
        "method": "textDocument/rename",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 0, "character": position},
            "newName": "b",
        },
    })
    assert collision[0]["error"]["code"] == -32602
    invalid = server.dispatch({
        "jsonrpc": "2.0",
        "id": 22,
        "method": "textDocument/rename",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 0, "character": position},
            "newName": "if",
        },
    })
    assert invalid[0]["error"]["code"] == -32602
    closed = server.dispatch({
        "jsonrpc": "2.0",
        "id": 23,
        "method": "textDocument/rename",
        "params": {
            "textDocument": {"uri": path_to_uri(tmp_path / "missing.zhl")},
            "position": {"line": 0, "character": 0},
            "newName": "renamed",
        },
    })
    assert closed[0]["error"]["code"] == -32602


def test_completion_request_uses_unsaved_compiler_scope(tmp_path: Path) -> None:
    source = tmp_path / "Completion.zhl"
    text = (
        "fn add(x:u8, y:u8) -> u9 { x+y }\n"
        "module Top { in a:u8 out z:u9 z=add(a,a) }\n"
    )
    source.write_text("module Top { out stale:u8 stale=1 }\n", encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    position = text.splitlines()[1].index("a", text.splitlines()[1].index("z="))
    response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 30,
        "method": "textDocument/completion",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 1, "character": position},
        },
    })
    items = response[0]["result"]
    assert [item["label"] for item in items] == ["a", "add"]
    assert items[0]["kind"] == 5
    assert items[1]["kind"] == 3
    assert items[1]["detail"] == "fn add(x : u8, y : u8) -> u9"
    assert items[0]["insertText"] == "a"


def test_completion_request_returns_empty_for_malformed_or_closed_document(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Completion.zhl"
    source.write_text("module Top {\n", encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": "module Top {\n"}},
    })
    malformed = server.dispatch({
        "jsonrpc": "2.0",
        "id": 31,
        "method": "textDocument/completion",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 0, "character": 7},
        },
    })
    assert malformed[0]["result"] == []
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didClose",
        "params": {"textDocument": {"uri": uri}},
    })
    closed = server.dispatch({
        "jsonrpc": "2.0",
        "id": 32,
        "method": "textDocument/completion",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 0, "character": 0},
        },
    })
    assert closed[0]["error"]["code"] == -32602


def test_signature_help_request_returns_compiler_resolved_parameters(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Signature.zhl"
    text = (
        "fn add(a:u8, b:u8, c:u8) -> u10 { a+b+c }\n"
        "module Top { out y:u10 y=add(1, 2, 3) }\n"
    )
    source.write_text("module Top { out stale:u8 stale=1 }\n", encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    call_line = text.splitlines()[1]
    response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 40,
        "method": "textDocument/signatureHelp",
        "params": {
            "textDocument": {"uri": uri},
            "position": {
                "line": 1,
                "character": call_line.index("2"),
            },
        },
    })
    assert response[0]["result"] == {
        "signatures": [{
            "label": "fn add(a : u8, b : u8, c : u8) -> u10",
            "parameters": [
                {"label": "a : u8"},
                {"label": "b : u8"},
                {"label": "c : u8"},
            ],
        }],
        "activeSignature": 0,
        "activeParameter": 1,
    }


def test_signature_help_request_returns_null_for_unknown_or_closed_document(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Signature.zhl"
    text = "module Top { out y:u8 y=1 }\n"
    source.write_text(text, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    no_call = server.dispatch({
        "jsonrpc": "2.0",
        "id": 41,
        "method": "textDocument/signatureHelp",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 0, "character": 0},
        },
    })
    assert no_call[0]["result"] is None
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didClose",
        "params": {"textDocument": {"uri": uri}},
    })
    closed = server.dispatch({
        "jsonrpc": "2.0",
        "id": 42,
        "method": "textDocument/signatureHelp",
        "params": {
            "textDocument": {"uri": uri},
            "position": {"line": 0, "character": 0},
        },
    })
    assert closed[0]["error"]["code"] == -32602


def test_semantic_token_relative_encoding_uses_utf16_units() -> None:
    source = "😀ab cd\nx\n"
    tokens = (
        ToolingSemanticToken(
            ToolingOrigin("Top.zhl", "parameter x", 2, 1, 2, 2),
            "parameter",
        ),
        ToolingSemanticToken(
            ToolingOrigin("Top.zhl", "function cd", 1, 5, 1, 7),
            "function",
        ),
        ToolingSemanticToken(
            ToolingOrigin("Top.zhl", "value ab", 1, 2, 1, 4),
            "variable",
            ("declaration",),
        ),
    )
    assert SEMANTIC_TOKEN_TYPES == (
        "parameter", "variable", "property", "function"
    )
    assert SEMANTIC_TOKEN_MODIFIERS == ("declaration",)
    assert semantic_tokens_to_lsp(tokens, source) == {
        "data": [
            0, 2, 2, 1, 1,
            0, 3, 2, 3, 0,
            1, 0, 1, 0, 0,
        ]
    }


def test_semantic_token_encoding_drops_overlapping_classifications() -> None:
    source = "value\n"
    tokens = (
        ToolingSemanticToken(
            ToolingOrigin("Top.zhl", "parameter value", 1, 1, 1, 6),
            "parameter",
        ),
        ToolingSemanticToken(
            ToolingOrigin("Top.zhl", "function value", 1, 1, 1, 6),
            "function",
        ),
    )
    assert semantic_tokens_to_lsp(tokens, source) == {
        "data": [0, 0, 5, 0, 0]
    }


def test_semantic_tokens_full_uses_unsaved_compiler_occurrences(
    tmp_path: Path,
) -> None:
    source = tmp_path / "SemanticTokens.zhl"
    text = (
        "fn f(x:u8) -> u8 { x }\n"
        "module Top { in a:u8 out y:u8 y=f(a) }\n"
    )
    source.write_text(
        "module Top { out stale:u8 stale=1 }\n",
        encoding="utf-8",
    )
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 50,
        "method": "textDocument/semanticTokens/full",
        "params": {"textDocument": {"uri": uri}},
    })
    assert response == [{
        "jsonrpc": "2.0",
        "id": 50,
        "result": {
            "data": [
                0, 3, 1, 3, 1,
                0, 2, 1, 0, 1,
                0, 14, 1, 0, 0,
                1, 16, 1, 2, 1,
                0, 9, 1, 2, 1,
                0, 7, 1, 3, 0,
                0, 2, 1, 2, 0,
            ]
        },
    }]
    changed = "module Top { in q:u8 out z:u8 z=q }\n"
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": changed}],
        },
    })
    changed_response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 52,
        "method": "textDocument/semanticTokens/full",
        "params": {"textDocument": {"uri": uri}},
    })
    assert changed_response[0]["result"] == {
        "data": [
            0, 16, 1, 2, 1,
            0, 9, 1, 2, 1,
            0, 7, 1, 2, 0,
        ]
    }


def test_semantic_tokens_full_handles_malformed_and_closed_documents(
    tmp_path: Path,
) -> None:
    source = tmp_path / "SemanticTokens.zhl"
    text = "module Top {\n"
    source.write_text(text, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    malformed = server.dispatch({
        "jsonrpc": "2.0",
        "id": 51,
        "method": "textDocument/semanticTokens/full",
        "params": {"textDocument": {"uri": uri}},
    })
    assert malformed[0]["result"] == {"data": []}
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didClose",
        "params": {"textDocument": {"uri": uri}},
    })
    closed = server.dispatch({
        "jsonrpc": "2.0",
        "id": 52,
        "method": "textDocument/semanticTokens/full",
        "params": {"textDocument": {"uri": uri}},
    })
    assert closed[0]["error"]["code"] == -32602


def test_semantic_tokens_full_preserves_shadowed_occurrences(tmp_path: Path) -> None:
    source = tmp_path / "ShadowedTokens.zhl"
    text = (
        "fn first(x:u8) -> u9 { x+1 }\n"
        "fn second(x:u8) -> u9 { x+2 }\n"
        "module Top { out y:u9 y=first(1) }\n"
    )
    source.write_text(text, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 53,
        "method": "textDocument/semanticTokens/full",
        "params": {"textDocument": {"uri": uri}},
    })
    assert response[0]["result"]["data"] == [
        0, 3, 5, 3, 1,
        0, 6, 1, 0, 1,
        0, 14, 1, 0, 0,
        1, 3, 6, 3, 1,
        0, 7, 1, 0, 1,
        0, 14, 1, 0, 0,
        1, 17, 1, 2, 1,
        0, 7, 5, 3, 0,
    ]


def test_code_action_maps_current_compiler_fix_to_exact_workspace_edit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "DuplicateImport.zhl"
    text = (
        "import std.bus.reg\n"
        "import std.bus.reg\n"
        "module Top { out y:u8 y=0 }\n"
    )
    source.write_text(text, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    published = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    diagnostic = published[0]["params"]["diagnostics"][0]
    assert diagnostic["code"] == "ZL-IMPORT-DUPLICATE"

    response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 60,
        "method": "textDocument/codeAction",
        "params": {
            "textDocument": {"uri": uri},
            "range": diagnostic["range"],
            "context": {
                "diagnostics": [diagnostic],
                "only": ["quickfix"],
            },
        },
    })
    assert response == [{
        "jsonrpc": "2.0",
        "id": 60,
        "result": [{
            "title": "Remove duplicate import declaration",
            "kind": "quickfix",
            "diagnostics": [diagnostic],
            "edit": {
                "changes": {
                    uri: [{
                        "range": {
                            "start": {"line": 1, "character": 0},
                            "end": {"line": 1, "character": 18},
                        },
                        "newText": "",
                    }],
                },
            },
        }],
    }]

    edit = response[0]["result"][0]["edit"]["changes"][uri][0]
    lines = text.splitlines(keepends=True)
    start = sum(len(item) for item in lines[: edit["range"]["start"]["line"]])
    start += edit["range"]["start"]["character"]
    end = sum(len(item) for item in lines[: edit["range"]["end"]["line"]])
    end += edit["range"]["end"]["character"]
    assert text[start:end] == "import std.bus.reg"
    fixed = text[:start] + edit["newText"] + text[end:]
    assert fixed == (
        "import std.bus.reg\n\nmodule Top { out y:u8 y=0 }\n"
    )
    assert check_snapshot(source, fixed).status == "passed"


def test_code_action_filters_range_kind_and_client_diagnostic(
    tmp_path: Path,
) -> None:
    source = tmp_path / "DuplicateImport.zhl"
    text = (
        "import std.bus.reg\n"
        "import std.bus.reg\n"
        "module Top { out y:u8 y=0 }\n"
    )
    source.write_text(text, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    published = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    diagnostic = published[0]["params"]["diagnostics"][0]

    def request(
        request_id: int,
        request_range: dict[str, object],
        only: list[str],
        requested: list[dict[str, object]],
    ) -> object:
        return server.dispatch({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "textDocument/codeAction",
            "params": {
                "textDocument": {"uri": uri},
                "range": request_range,
                "context": {"diagnostics": requested, "only": only},
            },
        })[0]["result"]

    unrelated = {
        "start": {"line": 2, "character": 0},
        "end": {"line": 2, "character": 10},
    }
    assert request(61, unrelated, ["quickfix"], [diagnostic]) == []
    assert request(62, diagnostic["range"], ["refactor"], [diagnostic]) == []
    stale_client = dict(diagnostic)
    stale_client["message"] = "stale client message"
    assert request(
        63,
        diagnostic["range"],
        ["quickfix"],
        [stale_client],
    ) == []


def test_code_action_never_promotes_prose_or_ambiguous_import_suggestions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "UnsafeFix.zhl"
    cases = (
        "module Top { in a:u8 out y:u7 y=a }\n",
        (
            "import std.bus.reg as first\n"
            "import std.bus.reg as second\n"
            "module Top { out y:u8 y=0 }\n"
        ),
    )
    for request_id, text in enumerate(cases, start=64):
        source.write_text(text, encoding="utf-8")
        uri = path_to_uri(source)
        server = LspServer()
        published = server.dispatch({
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {"uri": uri, "version": 1, "text": text}
            },
        })
        diagnostic = published[0]["params"]["diagnostics"][0]
        assert diagnostic["data"]["fixes"]
        response = server.dispatch({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "textDocument/codeAction",
            "params": {
                "textDocument": {"uri": uri},
                "range": diagnostic["range"],
                "context": {"diagnostics": [diagnostic]},
            },
        })
        assert response[0]["result"] == []


def test_code_action_does_not_synthesize_fix_for_malformed_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Malformed.zhl"
    text = "module Top {\n"
    source.write_text(text, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    published = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    diagnostic = published[0]["params"]["diagnostics"][0]
    response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 66,
        "method": "textDocument/codeAction",
        "params": {
            "textDocument": {"uri": uri},
            "range": diagnostic["range"],
            "context": {"diagnostics": [diagnostic]},
        },
    })
    assert response[0]["result"] == []


def test_code_action_rechecks_unsaved_text_and_rejects_stale_or_closed_document(
    tmp_path: Path,
) -> None:
    source = tmp_path / "StaleFix.zhl"
    duplicate = (
        "import std.bus.reg\n"
        "import std.bus.reg\n"
        "module Top { out y:u8 y=0 }\n"
    )
    valid = "import std.bus.reg\nmodule Top { out y:u8 y=0 }\n"
    source.write_text(duplicate, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    published = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {"uri": uri, "version": 1, "text": duplicate}
        },
    })
    stale_diagnostic = published[0]["params"]["diagnostics"][0]
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": valid}],
        },
    })
    stale = server.dispatch({
        "jsonrpc": "2.0",
        "id": 67,
        "method": "textDocument/codeAction",
        "params": {
            "textDocument": {"uri": uri},
            "range": stale_diagnostic["range"],
            "context": {"diagnostics": [stale_diagnostic]},
        },
    })
    assert stale[0]["result"] == []
    server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didClose",
        "params": {"textDocument": {"uri": uri}},
    })
    closed = server.dispatch({
        "jsonrpc": "2.0",
        "id": 68,
        "method": "textDocument/codeAction",
        "params": {
            "textDocument": {"uri": uri},
            "range": stale_diagnostic["range"],
            "context": {"diagnostics": [stale_diagnostic]},
        },
    })
    assert closed[0]["error"]["code"] == -32602


def test_code_action_edit_ranges_use_utf16_units(tmp_path: Path) -> None:
    source = tmp_path / "UnicodeFix.zhl"
    text = (
        "import std.bus.reg\n"
        "/*😀*/ import std.bus.reg\n"
        "module Top { out y:u8 y=0 }\n"
    )
    source.write_text(text, encoding="utf-8")
    uri = path_to_uri(source)
    server = LspServer()
    published = server.dispatch({
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "version": 1, "text": text}},
    })
    diagnostic = published[0]["params"]["diagnostics"][0]
    response = server.dispatch({
        "jsonrpc": "2.0",
        "id": 69,
        "method": "textDocument/codeAction",
        "params": {
            "textDocument": {"uri": uri},
            "range": diagnostic["range"],
            "context": {"diagnostics": [diagnostic]},
        },
    })
    edit_range = response[0]["result"][0]["edit"]["changes"][uri][0]["range"]
    assert edit_range == {
        "start": {"line": 1, "character": 7},
        "end": {"line": 1, "character": 25},
    }


def test_code_action_keeps_atomic_multi_edit_fix_together(tmp_path: Path) -> None:
    source = (tmp_path / "Atomic.zhl").resolve()
    text = "a b\n"
    uri = path_to_uri(source)
    state = DocumentState(uri, 1, text, source)
    fix = ToolingDiagnosticFix(
        "Apply both compiler edits",
        (
            ToolingDiagnosticEdit(
                source,
                ToolingOrigin("Atomic.zhl", "name a", 1, 1, 1, 2),
                "x",
            ),
            ToolingDiagnosticEdit(
                source,
                ToolingOrigin("Atomic.zhl", "name b", 1, 3, 1, 4),
                "y",
            ),
        ),
    )
    diagnostic = {
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 1},
        },
        "code": "ZL-TEST",
        "message": "compiler diagnostic",
    }
    action = _fix_to_code_action(fix, diagnostic, state)
    assert action is not None
    assert action["diagnostics"] == [diagnostic]
    assert action["edit"]["changes"][uri] == [
        {
            "range": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 1},
            },
            "newText": "x",
        },
        {
            "range": {
                "start": {"line": 0, "character": 2},
                "end": {"line": 0, "character": 3},
            },
            "newText": "y",
        },
    ]
