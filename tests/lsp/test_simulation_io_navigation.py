# SPDX-License-Identifier: Apache-2.0
import json

from zlang.lsp.server import LspServer, path_to_uri


def test_source_io_navigation_diagnostics_and_plan_preparation(tmp_path):
    source = """module T {
 clock clk reset rst
 in sample:u8
 out y:u8
 y=sample
 simulation io @clk {read src {v->sample} else {finish} write dst {v=y}}
}"""
    path = tmp_path / "test.zhl"
    path.write_text(source)
    vector = tmp_path / "input.jsonl"
    vector.write_text('{"v":7}\n')
    server = LspServer()
    uri = path_to_uri(path)
    result = server.dispatch(
        {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {"textDocument": {"uri": uri, "version": 1, "text": source}},
        }
    )
    assert not [
        d for item in result for d in item.get("params", {}).get("diagnostics", [])
    ]
    found = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "textDocument/definition",
            "params": {
                "textDocument": {"uri": uri},
                "position": {
                    "line": 5,
                    "character": source.splitlines()[5].index("sample") + 2,
                },
            },
        }
    )[0]
    assert found["result"]["range"]["start"]["line"] == 2
    response = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "zlang/prepareDebugBundle",
            "params": {
                "textDocument": {"uri": uri},
                "simulation": "io",
                "simulationInputs": {"src": str(vector)},
                "simulationOutputs": {"dst": "-"},
            },
        }
    )[0]
    assert "error" not in response, response
    result = response["result"]
    assert result["overlay_schema"] == "zlang-debug-overlay-v4"
    assert json.loads(result["simulation_io"]["json"])["records"] == [
        ["00000111"]
    ]
