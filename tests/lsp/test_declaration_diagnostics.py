"""Semantic declaration failures retain compiler-owned editor locations."""

from pathlib import Path

import pytest

from zlang.compiler import compile_file, compile_source
from zlang.ir.cdc import ClockDomain
from zlang.lsp.server import LspServer, path_to_uri
from zlang.semantic import SemanticError
from zlang.tooling import check_snapshot
from zlang.workspace import update_project_lock


def _publish(server: LspServer, path: Path, text: str, *, version: int = 1):
    uri = path_to_uri(path)
    if version == 1:
        method = "textDocument/didOpen"
        params = {"textDocument": {"uri": uri, "version": version, "text": text}}
    else:
        method = "textDocument/didChange"
        params = {
            "textDocument": {"uri": uri, "version": version},
            "contentChanges": [{"text": text}],
        }
    messages = server.dispatch({"jsonrpc": "2.0", "method": method, "params": params})
    return next(
        item["params"]["diagnostics"] for item in messages
        if item["method"] == "textDocument/publishDiagnostics"
        and item["params"]["uri"] == uri
    )


@pytest.mark.parametrize(
    ("source", "token", "message"),
    [
        ("module Bad {\n\n  in a: Missing\n}", "Missing", "unknown type 'Missing'"),
        ("module Bad {\n\n  out y: Missing\n}", "Missing", "unknown type 'Missing'"),
        ("struct Packet {\n  item: Missing\n}\nmodule Bad {}", "Missing", "unknown type 'Missing'"),
        ("type Payload = Missing\nmodule Bad {}", "Missing", "unknown type 'Missing'"),
        ("fn helper(a: Missing) -> u8 { 0 }\nmodule Bad {}", "Missing", "unknown type 'Missing'"),
        ("fn helper(a: u8) -> Missing { a }\nmodule Bad {}", "Missing", "unknown type 'Missing'"),
        ("module Bad {\n clock clk\n reset rst\n reg value: Missing = 0\n}", "Missing", "unknown type 'Missing'"),
        ("module Bad {\n\n reg address: u8 = 0\n}", "address", "requires a clock and reset"),
        ("module Bad {\n clock clk\n reset rst\n reg value: u8 @absent = 0\n}", "value", "unknown clock domain"),
        ("module Bad {\n clock clk\n reset rst\n out y: u8 @absent\n y=0\n}", "y", "unknown clock domain"),
        ("module Bad {\n in value: u8\n out value: u8\n}", "value", "duplicate port"),
        ("module First { out y:u8 y=0 }\nmodule Bad {\n\n out missing:u8\n}", "missing", "has no assignment"),
    ],
)
def test_declaration_diagnostics_follow_edits_and_clear(
    tmp_path: Path, source: str, token: str, message: str,
) -> None:
    path = tmp_path / "declaration.zhl"
    path.write_text(source)
    server = LspServer()
    for version, text in enumerate((source, "// shifted\n\n" + source), start=1):
        diagnostics = _publish(server, path, text, version=version)
        diagnostic = next(item for item in diagnostics if message in item["message"])
        lines = text.splitlines()
        # Duplicate names must identify the offending second declaration.
        line = next(
            index for index in reversed(range(len(lines)))
            if token in lines[index] and (":" in lines[index] or "type " in lines[index])
        )
        column = lines[line].index(token)
        assert diagnostic["range"] == {
            "start": {"line": line, "character": column},
            "end": {"line": line, "character": column + len(token)},
        }
        assert diagnostic["data"]["source_unit"] == "declaration.zhl"
    assert _publish(server, path, "module Fixed {}", version=3) == []


def test_ahb_source_requires_a_root_contract_but_points_to_address() -> None:
    path = Path(__file__).resolve().parents[2] / "stdlib/bus/ahb_lite.zhl"
    source = path.read_text()
    diagnostics = _publish(LspServer(), path, source)
    diagnostic = next(item for item in diagnostics if "register 'address'" in item["message"])
    line = next(i for i, text in enumerate(source.splitlines()) if "reg address :" in text)
    column = source.splitlines()[line].index("address")
    assert diagnostic["range"] == {
        "start": {"line": line, "character": column},
        "end": {"line": line, "character": column + len("address")},
    }
    assert any("standalone top" in note for note in diagnostic["data"]["notes"])
    with pytest.raises(SemanticError, match="requires a clock and reset"):
        compile_file(path, top="AHBLiteToRegBus")
    domain = ClockDomain("hclk", "hreset")
    compiled = compile_file(path, top="AHBLiteToRegBus", root_domain=domain)
    assert compiled.ir.clock_domains == (domain,)


def test_bus_child_still_inherits_its_parent_contract() -> None:
    result = compile_source("""
import std.bus.ahb_lite
import std.bus.reg
module Top {
    clock hclk
    reset hreset @hclk
    interface ahb: AHBLite<32,32>.slave @hclk
    bridge: AHBLiteToRegBus<32,32>
    csr: RegBusCSRTarget<32,32>
    ahb -> bridge.ahb
    bridge.regbus -> csr.regbus
}
""", top="Top")
    assert result.ir.children[0].clock_domains == result.ir.clock_domains


@pytest.mark.parametrize(
    "declaration",
    [
        "struct Packet {\n  item: Missing\n}\n",
        "type Packet = Missing\n",
        "struct Packet<type T> {\n  item: Missing\n}\n",
        "fn helper(a: Missing) -> u8 { 0 }\n",
        "fn helper(a: u8) -> Missing { a }\n",
    ],
)
def test_imported_type_error_retains_definition_file_and_digest(
    tmp_path: Path, declaration: str,
) -> None:
    import hashlib

    sources = tmp_path / "src"
    sources.mkdir()
    manifest = tmp_path / "zlang.toml"
    manifest.write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n'
    )
    leaf = sources / "types.zhl"
    leaf.write_text("// declaration\n\n" + declaration)
    source = "import demo.types\nmodule Top {"
    if "Packet<type T>" in declaration:
        source += " in a: Packet<u8>"
    source += "}\n"
    root = sources / "top.zhl"
    root.write_text(source)
    update_project_lock(manifest)
    record = check_snapshot(root, source)
    assert record.status == "failed"
    diagnostic = record.diagnostics[0]
    assert diagnostic.message == "unknown type 'Missing'"
    assert diagnostic.primary is not None
    assert diagnostic.primary.source_unit == "demo.types"
    line = next(i for i, text in enumerate(leaf.read_text().splitlines()) if "Missing" in text)
    column = leaf.read_text().splitlines()[line].index("Missing")
    assert (diagnostic.primary.start_line, diagnostic.primary.start_column) == (
        line + 1, column + 1,
    )
    with pytest.raises(SemanticError) as raised:
        compile_file(root)
    assert raised.value.primary.digest == hashlib.sha256(leaf.read_bytes()).hexdigest()


@pytest.mark.parametrize("annotation", ["vec<2,Missing>", "(u8, Missing)", "Box<Missing>"])
def test_nested_type_failure_retains_a_relevant_annotation_range(
    tmp_path: Path, annotation: str,
) -> None:
    source = f"struct Box<type T> {{ item:T }}\nmodule Top {{\n in a: {annotation}\n}}"
    path = tmp_path / "nested.zhl"
    path.write_text(source)
    diagnostics = _publish(LspServer(), path, source)
    diagnostic = next(item for item in diagnostics if "unknown type 'Missing'" in item["message"])
    start, end = diagnostic["range"]["start"], diagnostic["range"]["end"]
    assert start["line"] == end["line"] == 2
    token_column = source.splitlines()[2].index("Missing")
    assert start["character"] <= token_column < end["character"]


def test_non_bmp_comment_does_not_shift_type_diagnostic_utf16_column(tmp_path: Path) -> None:
    source = "module Top {\n /* 🔧 */ in a: Missing\n}"
    path = tmp_path / "unicode.zhl"
    path.write_text(source)
    diagnostic = _publish(LspServer(), path, source)[0]
    prefix = source.splitlines()[1].split("Missing")[0]
    column = len(prefix.encode("utf-16-le")) // 2
    assert diagnostic["range"] == {
        "start": {"line": 1, "character": column},
        "end": {"line": 1, "character": column + len("Missing")},
    }
