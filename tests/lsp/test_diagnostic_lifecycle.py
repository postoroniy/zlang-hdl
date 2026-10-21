"""Empty/EOF spans, closed-import ownership and project-metadata recovery."""

from dataclasses import replace
import hashlib
from io import StringIO
import queue
import subprocess
import sys
import threading

import pytest

from zlang.lsp.diagnostic_publication import diagnostic_source
from zlang.lsp.positions import compiler_character_to_lsp, lsp_character_to_compiler
from zlang.lsp.protocol import LspProtocolError, read_message, write_message
from zlang.lsp.server import LspServer, path_to_uri
from zlang.tooling import ToolingError, check_snapshot
from zlang.workspace import update_project_lock


def _open(server, path, text=None):
    return server.dispatch({"method": "textDocument/didOpen", "params": {
        "textDocument": {"uri": path_to_uri(path), "version": 1,
                         "text": path.read_text() if text is None else text},
    }})


def _diagnostics(messages):
    return {item["params"]["uri"]: item["params"]["diagnostics"] for item in messages
            if item.get("method") == "textDocument/publishDiagnostics"}


def _watched(server, path, kind=2):
    return server.dispatch({"method": "workspace/didChangeWatchedFiles", "params": {
        "changes": [{"uri": path_to_uri(path), "type": kind}],
    }})


def _project(tmp_path):
    sources = tmp_path / "src"
    sources.mkdir()
    manifest = tmp_path / "zlang.toml"
    manifest.write_text('schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n')
    root = sources / "top.zhl"
    root.write_text("module Top { out y:u8 y=1 }\n")
    return manifest, root


@pytest.mark.parametrize(("text", "line", "column", "utf16"), [
    ("", 1, 1, 0), ("a\n", 2, 1, 0), ("a\r\n", 2, 1, 0),
    ("a\r", 2, 1, 0), ("\n\n", 3, 1, 0), ("😀\r\n", 1, 2, 2),
])
def test_empty_and_eof_positions_round_trip(text, line, column, utf16):
    assert compiler_character_to_lsp(text, line, column) == utf16
    assert lsp_character_to_compiler(text, line - 1, utf16) == column - 1


@pytest.mark.parametrize(("text", "line", "column"), [
    ("", 1, 2), ("", 2, 1), ("x\n", 2, 2), ("x\n", 3, 1), ("x", 0, 1),
])
def test_eof_support_does_not_clamp_invalid_ranges(text, line, column):
    with pytest.raises(LspProtocolError, match="outside"):
        compiler_character_to_lsp(text, line, column)
    with pytest.raises(LspProtocolError, match="outside"):
        lsp_character_to_compiler(text, line - 1, column - 1)
    with pytest.raises(LspProtocolError, match="surrogate"):
        lsp_character_to_compiler("😀", 0, 1)


@pytest.mark.parametrize("text", ["", "\n", "\r\n", "module Top {\n", "// 😀\r\n"])
def test_empty_buffer_publishes_and_recovers(tmp_path, text):
    path = tmp_path / "top.zhl"
    path.write_text(text)
    server = LspServer(log=StringIO())
    diagnostics = _diagnostics(_open(server, path, text))[path_to_uri(path)]
    assert diagnostics and diagnostics[0]["code"] == "ZL-PARSE-001"
    recovered = server.dispatch({"method": "textDocument/didChange", "params": {
        "textDocument": {"uri": path_to_uri(path), "version": 2},
        "contentChanges": [{"text": "module Top {}\n"}],
    }})
    assert _diagnostics(recovered)[path_to_uri(path)] == []


@pytest.mark.parametrize("declaration", ["struct Packet { value: Missing }", "struct Packet { value: }"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_closed_import_uses_its_own_file_and_clears_after_fix(tmp_path, declaration, newline):
    manifest, root = _project(tmp_path)
    leaf = root.with_name("types.zhl")
    leaf.write_text("struct Packet { value:u8 }\n")
    root.write_text("import demo.types\nmodule Top {}\n")
    update_project_lock(manifest)
    text = newline.join(["// 😀 imported", *["// filler"] * 10, declaration, ""])
    leaf.write_bytes(text.encode())
    server = LspServer(log=StringIO())
    published = _diagnostics(_open(server, root))
    assert published[path_to_uri(root)] == []
    diagnostic = published[path_to_uri(leaf)][0]
    assert diagnostic["range"]["start"]["line"] == 11
    assert diagnostic["data"]["source_unit"] == "demo.types"
    assert not server._log.getvalue()
    # Fixing the dependency and rechecking the root retires the foreign marker.
    leaf.write_text("struct Packet { value:u8 }\n")
    published = _diagnostics(server._publish_document(server.documents[path_to_uri(root)]))
    assert published == {path_to_uri(root): [], path_to_uri(leaf): []}


def test_closed_import_digest_is_not_reinterpreted_on_different_bytes(tmp_path):
    manifest, root = _project(tmp_path)
    leaf = root.with_name("types.zhl")
    leaf.write_text("struct Packet { value:Missing }\n")
    root.write_text("import demo.types\nmodule Top {}\n")
    update_project_lock(manifest)
    server = LspServer()
    _open(server, root)
    diagnostic = check_snapshot(root, root.read_text()).diagnostics[0]
    assert diagnostic.source_digest == hashlib.sha256(leaf.read_bytes()).hexdigest()
    leaf.write_text("struct Packet { value:u8 }\n")
    with pytest.raises(ToolingError, match="changed during checking"):
        diagnostic_source(diagnostic, root, server.tooling_session)
    with pytest.raises(ToolingError, match="cannot locate"):
        diagnostic_source(replace(diagnostic, primary=replace(diagnostic.primary,
            source_unit="missing.unit")), root, server.tooling_session)


@pytest.mark.parametrize(("kind", "member", "newline"), [
    ("stdlib", "Missing", "\n"), ("stdlib", "", "\n"),
    ("stdlib", "Missing", "\r\n"), ("stdlib", "", "\r\n"),
    ("path_dependency", "Missing", "\n"),
])
def test_closed_external_import_is_bound_by_the_compiler_resolver(tmp_path, monkeypatch, kind, member, newline):
    import zlang.stdlib as stdlib

    manifest, root = _project(tmp_path)
    external = tmp_path / "library"
    external.mkdir()
    leaf = external / "types.zhl"
    payload = newline.join(["// external", "", f"struct Packet {{ value:{member} }}", ""]).encode()
    leaf.write_bytes(payload)
    if kind == "stdlib":
        monkeypatch.setattr(stdlib, "_ROOTS", (external,))
        logical = "std.types"
        leaf.write_text("struct Packet { value:u8 }\n")
    else:
        (external / "zlang.toml").write_text(
            'schema=1\n[project]\nname="library"\nversion="1"\nsource-root="."\n')
        manifest.write_text(manifest.read_text() + '\n[dependencies.library]\npath="library"\n')
        logical = "library.types"
    root.write_text(f"import {logical}\nmodule Top {{}}\n")
    update_project_lock(manifest)
    if kind == "stdlib":
        leaf.write_bytes(payload)
    server = LspServer(log=StringIO())
    published = _diagnostics(_open(server, root))
    assert published[path_to_uri(root)] == []
    error = published[path_to_uri(leaf)][0]
    assert error["data"]["source_unit"] == logical
    assert error["range"]["start"]["line"] == 2
    assert error["code"] == ("ZL-PARSE-001" if not member else "ZL-SEMANTIC-001")
    assert not server._log.getvalue()


def test_dirty_dependency_syntax_fails_closed_without_rewriting_lock(tmp_path):
    manifest, root = _project(tmp_path)
    dependency = tmp_path / "library"
    dependency.mkdir()
    (dependency / "zlang.toml").write_text(
        'schema=1\n[project]\nname="library"\nversion="1"\nsource-root="."\n')
    leaf = dependency / "types.zhl"
    leaf.write_text("struct Packet { value:u8 }\n")
    manifest.write_text(manifest.read_text() + '\n[dependencies.library]\npath="library"\n')
    root.write_text("import library.types\nmodule Top {}\n")
    update_project_lock(manifest)
    lock = manifest.with_name("zlang.lock")
    accepted = lock.read_bytes()
    leaf.write_text("struct Packet { value: }\n")
    messages = _open(LspServer(log=StringIO()), root)
    assert any("is dirty" in str(message) for message in messages)
    assert _diagnostics(messages)[path_to_uri(root)] == []
    assert lock.read_bytes() == accepted


def test_import_publications_keep_other_root_contributions(tmp_path):
    manifest, root = _project(tmp_path)
    leaf = root.with_name("types.zhl")
    leaf.write_text("struct Packet { value:Missing }\n")
    root.write_text("import demo.types\nmodule Top {}\n")
    second = root.with_name("second.zhl")
    second.write_text("import demo.types\nmodule Second {}\n")
    update_project_lock(manifest)
    server = LspServer()
    _open(server, root)
    _open(server, second)
    def close(path):
        return _diagnostics(server.dispatch({"method": "textDocument/didClose", "params": {
            "textDocument": {"uri": path_to_uri(path)},
        }}))
    assert len(close(root)[path_to_uri(leaf)]) == 1
    assert close(second)[path_to_uri(leaf)] == []


def test_lock_create_delete_and_manifest_changes_recheck_read_only(tmp_path):
    manifest, root = _project(tmp_path)
    lock = manifest.with_name("zlang.lock")
    log = StringIO()
    server = LspServer(log=log)
    messages = _open(server, root)
    assert any("lock is unavailable" in str(item) for item in messages)
    assert not lock.exists()
    # Bootstrap is explicit, including zero-dependency projects.
    update_project_lock(manifest)
    accepted = lock.read_bytes()
    assert _diagnostics(_watched(server, lock, 1))[path_to_uri(root)] == []
    assert lock.read_bytes() == accepted
    # Warm the semantic/symbol caches before invalidating resolution metadata.
    server._publish_document(server.documents[path_to_uri(root)])
    manifest.write_text(manifest.read_text().replace('version="1"', 'version="2"'))
    assert any("does not match" in str(item) for item in _watched(server, manifest))
    assert lock.read_bytes() == accepted
    update_project_lock(manifest)
    assert _diagnostics(_watched(server, lock))[path_to_uri(root)] == []
    lock.unlink()
    assert any("lock is unavailable" in str(item) for item in _watched(server, lock, 3))
    assert not lock.exists()
    manifest.unlink()
    assert _diagnostics(_watched(server, manifest, 3))[path_to_uri(root)] == []
    assert not lock.exists()  # Standalone recovery never creates a project.


def test_metadata_recheck_preserves_dirty_buffers_and_reports_malformed_toml(tmp_path):
    manifest, root = _project(tmp_path)
    update_project_lock(manifest)
    server = LspServer(log=StringIO())
    _open(server, root, "module Top { out y:Missing }\n")
    messages = _watched(server, manifest)
    assert "unknown type 'Missing'" in str(messages)
    manifest.write_text("[project\n")
    messages = _watched(server, manifest)
    assert any(item.get("method") == "window/showMessage" for item in messages)
    assert _diagnostics(messages)[path_to_uri(root)] == []
    assert server.documents[path_to_uri(root)].text == "module Top { out y:Missing }\n"
    assert _watched(server, root) == []
    bad = server.dispatch({"method": "workspace/didChangeWatchedFiles", "params": {
        "changes": [{"uri": path_to_uri(manifest), "type": 99}],
    }})
    assert any("change type" in str(item) for item in bad)


def test_unprojectable_diagnostic_is_reported_without_worker_failure(tmp_path, monkeypatch):
    import zlang.lsp.server as server_api

    path = tmp_path / "top.zhl"
    text = "module Top { in a:Missing }\n"
    path.write_text(text)
    record = check_snapshot(path, text)
    item = record.diagnostics[0]
    malformed = replace(record, diagnostics=(replace(item,
        primary=replace(item.primary, start_line=500, end_line=500)),))
    server = LspServer(log=StringIO())
    with monkeypatch.context() as patch:
        patch.setattr(server_api, "check_snapshot", lambda *args, **kwargs: malformed)
        messages = _open(server, path, text)
    assert _diagnostics(messages)[path_to_uri(path)] == []
    assert any("source range is outside" in str(message) for message in messages)
    assert "unknown type 'Missing'" in str(server._publish_document(
        server.documents[path_to_uri(path)]))


def test_metadata_scheduling_survives_invalid_manifest_and_outside_source_root(tmp_path):
    manifest, root = _project(tmp_path)
    outside = tmp_path / "outside.zhl"
    outside.write_text("module Outside {}\n")
    server = LspServer(log=StringIO())
    _open(server, outside)
    assert path_to_uri(outside) in server._affected_document_uris(server.documents[path_to_uri(outside)])
    manifest.write_text("[project\n")
    _open(server, root)
    assert path_to_uri(root) in server._affected_document_uris(server.documents[path_to_uri(root)])


def test_stdio_empty_diagnostic_and_lock_creation_recover_without_restart(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    manifest, root = _project(project)
    standalone = tmp_path / "empty.zhl"
    standalone.write_text("")
    messages = queue.Queue()
    with subprocess.Popen([sys.executable, "-c",
                          "from zlang.lsp.server import main; raise SystemExit(main(['--stdio']))"],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as process:
        def reader():
            while (message := read_message(process.stdout)) is not None:
                messages.put(message)
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        def send(method, params=None, **extra):
            write_message(process.stdin, {"jsonrpc": "2.0", "method": method,
                                         "params": params or {}, **extra})
        def until(predicate):
            seen = []
            for _ in range(30):
                try:
                    message = messages.get(timeout=15)
                except queue.Empty:
                    pytest.fail(f"missing stdio notification; exit={process.poll()}, seen={seen}")
                seen.append(message)
                if predicate(message):
                    return message
            pytest.fail("expected stdio notification was not delivered")
        try:
            send("initialize", id=1)
            until(lambda item: item.get("id") == 1)
            send("textDocument/didOpen", {"textDocument": {
                "uri": path_to_uri(standalone), "version": 1, "text": "",
            }})
            diagnostic = until(lambda item: bool(_diagnostics([item]).get(path_to_uri(standalone))))
            assert _diagnostics([diagnostic])[path_to_uri(standalone)][0]["code"] == "ZL-PARSE-001"
            send("textDocument/didOpen", {"textDocument": {
                "uri": path_to_uri(root), "version": 1, "text": root.read_text(),
            }})
            until(lambda item: "lock is unavailable" in str(item))
            update_project_lock(manifest)
            send("workspace/didChangeWatchedFiles", {"changes": [
                {"uri": path_to_uri(manifest.with_name("zlang.lock")), "type": 1},
            ]})
            until(lambda item: _diagnostics([item]).get(path_to_uri(root)) == [])
            send("shutdown", id=2)
            until(lambda item: item.get("id") == 2)
            send("exit")
            process.stdin.close()
            assert process.wait(timeout=10) == 0
            assert "Traceback" not in process.stderr.read().decode()
        finally:
            if process.poll() is None:
                process.kill()
