# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Unbound module templates still expose compiler-resolved type names."""

from pathlib import Path
from io import BytesIO
import subprocess
import sys

import pytest

from zlang.lsp.server import LspServer, path_to_uri
from zlang.tooling import check_snapshot


ROOT = Path(__file__).resolve().parents[2]


def opened(path, text):
    server = LspServer()
    server.dispatch({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
        "textDocument": {"uri": path_to_uri(path), "version": 1, "text": text}}})
    return server


def request(server, path, text, needle, name, *, edge=False, method="definition", request_id=1):
    line, row = next((i, row) for i, row in enumerate(text.splitlines()) if needle in row)
    return server.dispatch({
        "jsonrpc": "2.0", "id": request_id, "method": "textDocument/" + method,
        "params": {"textDocument": {"uri": path_to_uri(path)},
                   "position": {"line": line, "character": row.index(name) +
                                (len(name) if edge else 2)},
                   "context": {"includeDeclaration": True}},
    })[0]["result"]


@pytest.mark.parametrize("needle", ["struct SinCos", "out output : rv<SinCos", "rom table : rom<SinCos"])
@pytest.mark.parametrize("edge", [False, True])
def test_real_nco_same_file_type_definition(needle, edge):
    path = ROOT / "stdlib/dsp/nco.zhl"
    text = path.read_text()
    record = check_snapshot(path, text)
    assert record.status == "failed"
    assert record.diagnostics[0].code == "ZL-GENERIC-SPECIALIZATION-REQUIRED"
    server = opened(path, text)
    expected = {"uri": path_to_uri(path), "range": {
        "start": {"line": 6, "character": 7}, "end": {"line": 6, "character": 13}}}
    for repeat in range(2):
        assert request(server, path, text, needle, "SinCos", edge=edge,
                       request_id=repeat + 1) == expected


def test_real_nco_references_include_generic_ports_and_rom():
    path = ROOT / "stdlib/dsp/nco.zhl"
    text = path.read_text()
    server = opened(path, text)
    for repeat in range(2):
        references = request(server, path, text, "out output : rv<SinCos", "SinCos",
                             method="references", request_id=repeat + 1)
        assert [item["range"]["start"]["line"] + 1 for item in references] == [7, 13, 48, 52]
        assert all(item["uri"] == path_to_uri(path) for item in references)


@pytest.mark.parametrize("needle", ["phase <- phase_advance", "init nco_sincos_table"])
def test_real_nco_calls_in_unspecialized_module(needle):
    path = ROOT / "stdlib/dsp/nco.zhl"
    text = path.read_text()
    name = "phase_advance" if "phase_advance" in needle else "nco_sincos_table"
    found = request(opened(path, text), path, text, needle, name)
    assert found is not None
    assert found["uri"] == path_to_uri(path)
    assert found["range"]["start"]["line"] == (2 if name == "phase_advance" else 11)


@pytest.mark.parametrize("shadowed", [False, True])
def test_template_type_parameter_does_not_bind_same_named_global(tmp_path, shadowed):
    parameters = "type Payload,N,D=1<<N" if shadowed else "N,D=1<<N"
    text = f"""struct Payload {{ data:u8 }}
module Template<{parameters}> {{
 in source:rv<Payload>
 out sink:rv<Payload>
 connect source -> sink
}}
"""
    path = tmp_path / "template.zhl"
    path.write_text(text)
    found = request(opened(path, text), path, text, "in source", "Payload")
    if shadowed:
        assert found is None
    else:
        assert found == {"uri": path_to_uri(path), "range": {
            "start": {"line": 0, "character": 7}, "end": {"line": 0, "character": 14}}}


def test_unknown_type_and_malformed_template_never_fabricate_definition(tmp_path):
    for body in ("in source:rv<Missing>", "in source:rv<Missing"):
        text = f"struct Payload {{ data:u8 }}\nmodule Template<N,D=1<<N> {{{body}}}"
        path = tmp_path / "broken.zhl"
        path.write_text(text)
        assert request(opened(path, text), path, text, "in source", "Missing") is None


def test_declarations_do_not_make_unbound_hardware_valid(tmp_path):
    from zlang.compiler import check_file_snapshot, observe_file_declarations
    from zlang.semantic import SemanticError

    path = tmp_path / "template.zhl"
    text = "struct Payload {data:u8}\nmodule Template<N,D=1<<N>{out y:Payload}"
    path.write_text(text)
    observations = observe_file_declarations(path, text)
    assert observations.definition_resolutions
    assert not hasattr(observations, "ir")
    with pytest.raises(SemanticError, match="unresolved compile-time parameter"):
        check_file_snapshot(path, text)


def test_declaration_observation_does_not_replace_production_products():
    from zlang.analysis_needs import AnalysisNeeds
    from zlang.compilation_session import CompilationSession
    from zlang.backend.systemverilog import emit

    text = """struct Payload {data:u8}
module Template<N,D=1<<N>{out y:Payload y=Payload {data=0}}
module Top {out y:u8 y=1}
"""
    ordinary = CompilationSession(text, analysis_needs=AnalysisNeeds.DEFINITIONS)
    observed = CompilationSession(text, analysis_needs=AnalysisNeeds.DEFINITIONS)
    observed.observe_declarations()
    assert observed.computed_products == ("syntax",)
    assert ordinary.semantic_ir == observed.semantic_ir
    assert ordinary.high_level_ir == observed.high_level_ir
    assert emit(ordinary.selected_ir) == emit(observed.selected_ir)


@pytest.mark.parametrize("method", ["definition", "references"])
def test_real_stdio_worker_resolves_unbound_nco_type_repeatedly(method):
    from zlang.lsp.server import read_message, write_message

    path = ROOT / "stdlib/dsp/nco.zhl"
    text = path.read_text()
    line = next(i for i, row in enumerate(text.splitlines()) if "out output : rv<SinCos" in row)
    column = text.splitlines()[line].index("SinCos") + 2
    payload = BytesIO()
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
            "textDocument": {"uri": path_to_uri(path), "version": 1, "text": text}}},
    ]
    messages.extend({"jsonrpc": "2.0", "id": request_id, "method": "textDocument/" + method,
                     "params": {"textDocument": {"uri": path_to_uri(path)},
                                "position": {"line": line, "character": column},
                                "context": {"includeDeclaration": True}}}
                    for request_id in (2, 3))
    messages.extend([{"jsonrpc": "2.0", "id": 4, "method": "shutdown"},
                     {"jsonrpc": "2.0", "method": "exit"}])
    for message in messages:
        write_message(payload, message)
    run = subprocess.run(
        [sys.executable, "-c", "from zlang.lsp.server import main; raise SystemExit(main(['--stdio']))"],
        input=payload.getvalue(), capture_output=True, timeout=10,
    )
    assert run.returncode == 0, run.stderr.decode()
    stream, responses = BytesIO(run.stdout), {}
    while (message := read_message(stream)) is not None:
        if "id" in message:
            responses[message["id"]] = message
    assert responses[2]["result"] == responses[3]["result"]
    if method == "definition":
        assert responses[2]["result"] == {"uri": path_to_uri(path), "range": {
            "start": {"line": 6, "character": 7}, "end": {"line": 6, "character": 13}}}
    else:
        assert [item["range"]["start"]["line"] + 1
                for item in responses[2]["result"]] == [7, 13, 48, 52]


def test_declaration_cache_rechecks_dependency_and_root_edits(tmp_path, monkeypatch):
    from zlang import tooling_session
    from zlang.tooling import ToolingSession, definition_at
    from zlang.workspace import update_project_lock

    monkeypatch.setenv("ZLANG_LSP_SYMBOL_CACHE", "memory")
    (tmp_path / "src").mkdir()
    (tmp_path / "zlang.toml").write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n'
    )
    types = tmp_path / "src/types.zhl"
    types.write_text("struct Payload {data:u8}\n")
    path = tmp_path / "src/template.zhl"
    text = "import demo.types\nmodule Template<N,D=1<<N>{out y:rv<Payload>}"
    path.write_text(text)
    update_project_lock(tmp_path)
    session = ToolingSession()
    calls = []
    original = tooling_session.observe_file_declarations

    def observed(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling_session, "observe_file_declarations", observed)
    column = text.splitlines()[1].index("Payload") + 2
    for _ in range(2):
        found = definition_at(path, text, 1, column, _session=session)
        assert found is not None and found.target_path == types
        assert found.target_origin.start_line == 1
    assert len(calls) == 1
    types.write_text("// shifted\nstruct Payload {data:u8}\n")
    changed = definition_at(path, text, 1, column, _session=session)
    assert changed is not None and changed.target_origin.start_line == 2
    assert len(calls) == 2
    renamed = text.replace("Payload", "Missing")
    assert definition_at(path, renamed, 1, column, _session=session) is None
