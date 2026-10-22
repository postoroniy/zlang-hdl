"""Derived defaults are unbound templates, never valid unspecialized builds."""

import pytest

from zlang.compiler import compile_source
from zlang.lsp.server import LspServer, path_to_uri
from zlang.semantic import SemanticError
from zlang.tooling import check_snapshot


def diagnostics(path, text):
    response = LspServer().dispatch(
        {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {"uri": path_to_uri(path), "version": 1, "text": text}
            },
        }
    )
    return [
        d
        for item in response
        if item.get("method") == "textDocument/publishDiagnostics"
        for d in item["params"]["diagnostics"]
    ]




@pytest.mark.parametrize("expression", ("N-1", "1<<N", "index_width(N)", "(N+3)%7"))
def test_declared_earlier_parameter_requires_specialization(tmp_path, expression):
    text = f"module Template<N,D={expression}> {{out y:u8 y=0}}"
    path = tmp_path / "template.zhl"
    path.write_text(text)
    record = check_snapshot(path, text)
    assert record.status == "failed"
    assert record.diagnostics[0].code == "ZL-GENERIC-SPECIALIZATION-REQUIRED"
    assert diagnostics(path, text) == []
    with pytest.raises(SemanticError):
        compile_source(text)
    concrete = "module Top {inst child:Template<N=3> out y:u8 y=child.y}"
    assert compile_source(text + concrete, top="Top").ir.name == "Top"


@pytest.mark.parametrize(
    "parameters", ("N,D=typo+1", "N=3,D=1/0", "N=3,D=1<<-1", "D=N+1,N=3", "N=3,D=N,D=4")
)
def test_invalid_defaults_are_not_reclassified_or_hidden(tmp_path, parameters):
    text = f"module Broken<{parameters}> {{out y:u8 y=0}}"
    path = tmp_path / "broken.zhl"
    path.write_text(text)
    observed = diagnostics(path, text)
    assert observed
    assert all(d["code"] != "ZL-GENERIC-SPECIALIZATION-REQUIRED" for d in observed)
    with pytest.raises(SemanticError):
        compile_source(text)
