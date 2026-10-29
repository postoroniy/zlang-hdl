# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Deferred generic bodies retain exact compiler-owned navigation targets."""

from pathlib import Path
from io import BytesIO
import subprocess
import sys

import pytest

from zlang.lsp.server import LspServer, path_to_uri
from zlang.tooling import check_snapshot, definition_at, ToolingSession


SOURCE = """struct Envelope<type T> { item:T }
type ByteEnvelope = Envelope<u8>
fn identity<type T>(value:T) -> T { value }
fn build<type T>(value:T) -> Envelope<T> {
    Envelope { item=identity<T=T>(value) }
}
fn cast<N>(raw:bits<N>) -> ByteEnvelope { unpack<ByteEnvelope>(raw) }
"""


def location(server, path, text, needle, name, request=1, *, edge=False):
    line, row = next((i, row) for i, row in enumerate(text.splitlines()) if needle in row)
    column = row.index(name, row.index(needle))
    return server.dispatch({
        "jsonrpc": "2.0", "id": request, "method": "textDocument/definition",
        "params": {"textDocument": {"uri": path_to_uri(path)},
                   "position": {"line": line, "character": column + (len(name) if edge else 1)}},
    })[0]["result"]


def opened(path, text):
    server = LspServer()
    server.dispatch({"jsonrpc": "2.0", "method": "textDocument/didOpen",
                     "params": {"textDocument": {"uri": path_to_uri(path),
                                 "version": 1, "text": text}}})
    return server


@pytest.mark.parametrize("needle,name,target_line,target_column", [
    ("struct Envelope", "Envelope", 0, 7),
    ("fn identity", "identity", 2, 3),
    ("fn build", "build", 3, 3),
    ("    Envelope", "Envelope", 0, 7),
    ("item=identity", "identity", 2, 3),
    ("unpack<ByteEnvelope>", "ByteEnvelope", 1, 5),
])
@pytest.mark.parametrize("edge", [False, True])
def test_deferred_body_and_declaration_definition(tmp_path, needle, name, target_line,
                                                 target_column, edge):
    path = tmp_path / "library.zhl"
    path.write_text(SOURCE)
    assert check_snapshot(path, SOURCE).status == "passed"
    server = opened(path, SOURCE)
    for request in (1, 2):  # The normalized symbol shard must retain the same facts.
        found = location(server, path, SOURCE, needle, name, request, edge=edge)
        assert found is not None
        assert found["uri"] == path_to_uri(path)
        assert found["range"] == {
            "start": {"line": target_line, "character": target_column},
            "end": {"line": target_line, "character": target_column + len(name)},
        }


def test_real_fp_format_constructor_and_declaration():
    path = Path(__file__).resolve().parents[2] / "stdlib/math/fp_format.zhl"
    text = path.read_text()
    server = opened(path, text)
    expected = {"uri": path_to_uri(path), "range": {
        "start": {"line": 2, "character": 7}, "end": {"line": 2, "character": 16}}}
    for i, needle in enumerate(("struct FpDecoded", "-> FpDecoded", "    FpDecoded {"), 1):
        assert location(server, path, text, needle, "FpDecoded", i) == expected


@pytest.mark.parametrize("method,include_declaration", [
    ("textDocument/definition", False),
    ("textDocument/references", False),
    ("textDocument/references", True),
])
def test_real_stdio_worker_resolves_fp_decoded_constructor(method, include_declaration):
    from zlang.lsp.server import read_message, write_message

    path = Path(__file__).resolve().parents[2] / "stdlib/math/fp_format.zhl"
    text = path.read_text()
    line, row = next((i, row) for i, row in enumerate(text.splitlines())
                     if "    FpDecoded {" in row)
    payload = BytesIO()
    for message in (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
            "textDocument": {"uri": path_to_uri(path), "version": 1, "text": text}}},
        {"jsonrpc": "2.0", "id": 2, "method": method, "params": {
            "textDocument": {"uri": path_to_uri(path)},
            "position": {"line": line, "character": row.index("FpDecoded") + 2},
            "context": {"includeDeclaration": include_declaration}}},
        {"jsonrpc": "2.0", "id": 3, "method": "shutdown"},
        {"jsonrpc": "2.0", "method": "exit"},
    ):
        write_message(payload, message)
    run = subprocess.run(
        (sys.executable, "-c", "from zlang.lsp.server import main; raise SystemExit(main(['--stdio']))"),
        input=payload.getvalue(), capture_output=True, timeout=10,
    )
    assert run.returncode == 0, run.stderr.decode()
    stream, responses = BytesIO(run.stdout), {}
    while (message := read_message(stream)) is not None:
        if "id" in message:
            responses[message["id"]] = message
    declaration = {"uri": path_to_uri(path), "range": {
        "start": {"line": 2, "character": 7}, "end": {"line": 2, "character": 16}}}
    if method == "textDocument/definition":
        assert responses[2]["result"] == declaration
    else:
        expected = [declaration] if include_declaration else []
        for row, column in ((34, 50), (41, 4)):
            expected.append({"uri": path_to_uri(path), "range": {
                "start": {"line": row, "character": column},
                "end": {"line": row, "character": column + len("FpDecoded")}}})
        assert responses[2]["result"] == expected


@pytest.mark.parametrize("cursor_line", [2, 34, 41])
@pytest.mark.parametrize("include_declaration", [False, True])
@pytest.mark.parametrize("edge", [False, True])
def test_fp_decoded_references_from_each_occurrence(cursor_line, include_declaration, edge):
    path = Path(__file__).resolve().parents[2] / "stdlib/math/fp_format.zhl"
    text = path.read_text()
    server = opened(path, text)
    column = text.splitlines()[cursor_line].index("FpDecoded") + (len("FpDecoded") if edge else 2)
    expected = [
        {"uri": path_to_uri(path), "range": {
            "start": {"line": row, "character": start},
            "end": {"line": row, "character": start + len("FpDecoded")}}}
        for row, start in ([(2, 7)] if include_declaration else []) + [(34, 50), (41, 4)]
    ]
    for request in (1, 2):
        response = server.dispatch({
            "jsonrpc": "2.0", "id": request, "method": "textDocument/references",
            "params": {"textDocument": {"uri": path_to_uri(path)},
                       "position": {"line": cursor_line, "character": column},
                       "context": {"includeDeclaration": include_declaration}},
        })
        assert response == [{"jsonrpc": "2.0", "id": request, "result": expected}]


@pytest.mark.parametrize("include_declaration", [False, True])
def test_fp_decode_function_references_remain_distinct_from_result_type(include_declaration):
    path = Path(__file__).resolve().parents[2] / "stdlib/math/fp_format.zhl"
    text = path.read_text()
    server = opened(path, text)
    expected = [
        {"uri": path_to_uri(path), "range": {
            "start": {"line": row, "character": start},
            "end": {"line": row, "character": start + len("fp_decode")}}}
        for row, start in ([(34, 3)] if include_declaration else []) + [(61, 8)]
    ]
    for request, column in enumerate((8, 12), 1):
        response = server.dispatch({
            "jsonrpc": "2.0", "id": request, "method": "textDocument/references",
            "params": {"textDocument": {"uri": path_to_uri(path)},
                       "position": {"line": 34, "character": column},
                       "context": {"includeDeclaration": include_declaration}},
        })
        assert response == [{"jsonrpc": "2.0", "id": request, "result": expected}]


def test_generic_callable_parameter_does_not_bind_global_function(tmp_path):
    text = """fn helper<N>(value:u8) -> u8 { value }
fn invoke<helper:fn(u8)->u8>(value:u8) -> u8 { helper(value) }
"""
    path = tmp_path / "callbacks.zhl"
    path.write_text(text)
    assert check_snapshot(path, text).status == "passed"
    server = opened(path, text)
    assert location(server, path, text, "{ helper(value)", "helper") is None


def test_malformed_source_never_fabricates_a_generic_definition(tmp_path):
    text = SOURCE.replace("item=identity", "item=)")
    path = tmp_path / "broken.zhl"
    path.write_text(text)
    server = opened(path, text)
    assert location(server, path, text, "    Envelope", "Envelope") is None


def test_generic_type_parameter_does_not_bind_same_named_global_type(tmp_path):
    text = """struct Envelope { item:u8 }
fn use<type Envelope>(value:Envelope) -> Envelope { value }
"""
    path = tmp_path / "types.zhl"
    path.write_text(text)
    assert check_snapshot(path, text).status == "passed"
    assert location(opened(path, text), path, text, "-> Envelope", "Envelope") is None


def test_intrinsic_call_does_not_bind_same_named_global_function(tmp_path):
    text = """fn repeat<N>(value:u8) -> u8 { value }
fn use<N>(value:u8) -> vec<N,u8> { repeat<N>(value) }
"""
    path = tmp_path / "intrinsics.zhl"
    path.write_text(text)
    assert check_snapshot(path, text).status == "passed"
    assert location(opened(path, text), path, text, "{ repeat<N>", "repeat") is None


def test_deferred_operator_constructor_is_a_nominal_type_reference(tmp_path):
    text = """struct Envelope<type T> { item:T }
operator +<type T>(a:Envelope<T>,b:Envelope<T>) { Envelope { item=a.item } }
"""
    path = tmp_path / "overloads.zhl"
    path.write_text(text)
    assert check_snapshot(path, text).status == "passed"
    found = location(opened(path, text), path, text, "{ Envelope {", "Envelope")
    assert found is not None and found["range"]["start"] == {"line": 0, "character": 7}


def test_imported_constructor_survives_cache_restart_and_dependency_edit(tmp_path, monkeypatch):
    from zlang import tooling_session
    from zlang.workspace import update_project_lock

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("ZLANG_LSP_SYMBOL_CACHE", "persistent")
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "zlang.toml").write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n'
    )
    dependency = root / "src/types.zhl"
    dependency.write_text("struct Envelope<type T> { item:T }\n")
    path = root / "src/library.zhl"
    text = "import demo.types\nfn wrap<type T>(value:T)->Envelope<T> { Envelope {item=value} }\n"
    path.write_text(text)
    update_project_lock(root)
    column = text.splitlines()[1].index("{ Envelope") + 3
    calls = []
    original = tooling_session.check_file_snapshot

    def observed(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling_session, "check_file_snapshot", observed)
    for _ in range(2):
        found = definition_at(path, text, 1, column, _session=ToolingSession())
        assert found is not None and found.target_path == dependency
        assert found.target_origin.start_line == 1
    assert len(calls) == 1
    dependency.write_text("// shifted declaration\n" + dependency.read_text())
    changed = definition_at(path, text, 1, column, _session=ToolingSession())
    assert changed is not None and changed.target_origin.start_line == 2
    assert len(calls) == 2


def test_navigation_observations_leave_production_hardware_unchanged():
    from zlang.analysis_needs import AnalysisNeeds
    from zlang.compilation_session import CompilationSession
    from zlang.backend.systemverilog import emit

    text = SOURCE + "module Top {in a:u8 out y:Envelope<u8> y=build<T=u8>(a)}\n"
    plain = CompilationSession(text, analysis_needs=AnalysisNeeds.NONE)
    observed = CompilationSession(text, analysis_needs=AnalysisNeeds.DEFINITIONS)
    assert plain.semantic_ir == observed.semantic_ir
    assert plain.high_level_ir == observed.high_level_ir
    assert emit(plain.selected_ir) == emit(observed.selected_ir)
    assert not plain.semantic_definition_resolutions
    assert observed.semantic_definition_resolutions
