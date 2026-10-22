"""Standalone navigation uses the exact compiler-retained stdlib closure."""

from pathlib import Path
from types import SimpleNamespace

from zlang.lsp.server import LspServer, path_to_uri
from zlang.tooling import check_snapshot
from zlang.tooling_workspace import _source_path_for_origin


def test_standalone_stdlib_definition_without_project_lock(tmp_path):
    text = """import std.math.fixed
module Top {
 in a,b:fixed<8,4>
 out y:fixed<8,4>
 y=fixed_min(a,b)
}
"""
    path = tmp_path / "top.zhl"
    path.write_text(text)
    assert check_snapshot(path, text).status == "passed"
    uri = path_to_uri(path)
    server = LspServer()
    server.dispatch({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
        "textDocument": {"uri": uri, "version": 1, "text": text}}})
    lines = text.splitlines()
    line = next(i for i, value in enumerate(lines) if "fixed_min" in value)
    response = server.dispatch({"jsonrpc": "2.0", "id": 1,
        "method": "textDocument/definition", "params": {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": lines[line].index("fixed_min") + 1}}})
    root = Path(__file__).resolve().parents[2]
    assert response[0]["result"]["uri"] == path_to_uri(root / "stdlib/math/fixed.zhl")
    assert not (tmp_path / "zlang.lock").exists()


def test_standalone_stdlib_navigation_rejects_unretained_source(tmp_path):
    path = tmp_path / "top.zhl"
    path.write_text("module Top {out y:bit y=0}")
    origin = SimpleNamespace(source_unit="std.math.fixed")
    inputs = SimpleNamespace(root_source=path, source_unit_paths=(), all_paths=())
    assert _source_path_for_origin(path, origin, inputs) is None
