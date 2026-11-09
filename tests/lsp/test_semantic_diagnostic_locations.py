# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Source-owned error locations survive edits and compiler lowering boundaries."""

import hashlib
from textwrap import dedent

import pytest

from zlang.diagnostics import DiagnosticError, diagnostic_source
from zlang.lsp.server import LspServer, path_to_uri
from zlang.source import SourceOrigin, SourceSpan
from zlang.workspace import update_project_lock


# Each marker is an independent expectation, not a span read back from the DUT.
CASES = [
    pytest.param(
        """
        module Bad {
          in a:u8 out y:u8
          y=§architecture(auto){a}
        }
    """,
        "scalar architecture(auto) was removed",
        id="parser-callback",
    ),
    pytest.param(
        """
        module Bad<
          §W=N+1, N=2
        > { out y:uint<W> y=0 }
    """,
        "references later parameter",
        id="parameter-default",
    ),
    pytest.param(
        """
        module Bad<N=2,
          §N=3
        > { out y:u8 y=0 }
    """,
        "duplicate compile-time parameter",
        id="duplicate-parameter",
    ),
    pytest.param(
        """
        module Bad {
          §generate(i in 2..1) { x=0 }
        }
    """,
        "is reversed",
        id="generate-range",
    ),
    pytest.param(
        """
        module Bad {
          in x:bit
          if §x { z=0 }
        }
    """,
        "compile-time condition references runtime value",
        id="compile-time-if",
    ),
    pytest.param(
        """
        type §string=u8
        module Bad {}
    """,
        "reserved type name",
        id="reserved-type",
    ),
    pytest.param(
        """
        struct S {
          x:u8
          §x:u8
        }
        module Bad { in value:S }
    """,
        "duplicate field",
        id="duplicate-struct-field",
    ),
    pytest.param(
        """
        module Bad {
          §clock c
        }
    """,
        "clock and reset must be declared together",
        id="missing-reset",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r
          §clock c
        }
    """,
        "physical clock declarations must match clock declarations",
        id="duplicate-clock",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r
          in go:u2 reg x:u8=0
          rule a when §go { x <- 0 }
        }
    """,
        "guard for rule 'a' must be bit",
        id="rule-guard",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r
          rule a when 1 { §missing <- 0 }
        }
    """,
        "is not a register or output wire",
        id="rule-target",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r reg x:u8=0
          rule a when 1 { x <- 0 }
          §priority a > absent
        }
    """,
        "priority references an unknown rule",
        id="priority-target",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r reg x:u8=0
          rule a when 1 { x <- 0 }
          rule b when 1 { x <- 1 }
          §priority a > b
          priority b > a
        }
    """,
        "priority graph contains a cycle",
        id="priority-cycle",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r reg x:u8=0
          x <- 0
          §x <- 1
        }
    """,
        "more than one next-state assignment",
        id="duplicate-next-state",
    ),
    pytest.param(
        """
        module Bad {
          in x:u8
          §x=0
        }
    """,
        "cannot assign to input",
        id="input-driver",
    ),
    pytest.param(
        """
        module Bad {
          in input:rv<u8>
          §input.payload=0
        }
    """,
        "cannot drive incoming ready/valid field",
        id="protocol-driver",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r
          §fifo q:fifo<u8,0>
        }
    """,
        "depth",
        id="fifo-depth",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r fifo q:fifo<u8,2>
          §q.full=0
        }
    """,
        "is read-only",
        id="storage-control",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r in go:u2
          fifo q:fifo<u8,2>
          q.data=0 q.pop=0
          q.push=§go
        }
    """,
        "push and pop controls must be bit",
        id="fifo-control-type",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r
          §memory m:mem<u8,3>{read_latency 1 collision read_first}
        }
    """,
        "power of two",
        id="memory-depth",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r in address:u3
          rom t:rom<u8,4>{read_latency 1 init generate(i in 0..4) i}
          t.read_address=§address
        }
    """,
        "expected exact u2",
        id="rom-control-type",
    ),
    pytest.param(
        """
        module Bad {
          §csr registers @0 { R @0 { value u32 @31:0 rw=0 } }
        }
    """,
        "require a module clock and reset",
        id="csr-clock",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r
          csr registers @0 { R @0 {
            value bit @0 rw=0
            §value bit @1 rw=0
          } }
        }
    """,
        "duplicate field",
        id="csr-field",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r in a:bit out y:bit y=a
          §guarantee g @ missing disable iff r { y == a }
        }
    """,
        "references unknown clock",
        id="legacy-contract",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r
          §assert ok @ missing { 1 }
        }
    """,
        "references unknown clock",
        id="verification-clock",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r in a:bit
          contract s {
            assert same { a }
            §cover same { a }
          }
        }
    """,
        "duplicate verification clause",
        id="verification-clause",
    ),
    pytest.param(
        """
        module Child<type T> { in x:T }
        module Bad {
          inst §child:Child
        }
    """,
        "missing required type argument",
        id="instance-specialization",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r in x:u8 out y:u8
          y=pipeline(1){x}
          §timing { latency 2 ii 1 }
        }
    """,
        "module timing contract declares latency 2",
        id="timing-contract",
    ),
    pytest.param(
        """
        module Bad {
          in x:u8 out y:u9
          y=§implement { x+1 intent { latency >= 1 minimize ff } }
        }
    """,
        "requires a module clock and reset",
        id="implementation-intent",
    ),
    pytest.param(
        """
        fn §parity(x:u8)->u8{x}
        module Bad {}
    """,
        "reserved for a compiler intrinsic",
        id="function-declaration",
    ),
    pytest.param(
        """
        §operator +(a:u8,b:u8)->u9 { a+b }
        module Bad { out y:u8 y=0 }
    """,
        "must be owned by a nominal struct operand",
        id="operator-declaration",
    ),
    pytest.param(
        """
        struct S { a:u8 }
        module Bad {
          in x:S
          §S { a,a }=x
        }
    """,
        "repeats field",
        id="destructuring",
    ),
    pytest.param(
        """
        module Lane { in x:u8 out y:u8 y=x }
        module Bad {
          in x:u8
          inst lane[2]:Lane
          generate(i in 0..2) { lane[i].x=x }
          generate(i in 0..2) { §lane[i].x=x }
        }
    """,
        "assigned more than once",
        id="array-binding",
    ),
    pytest.param(
        """
        module Lane { in x:u8 out y:u8 y=x }
        module Bad {
          in x:u8
          inst §lane[2]:Lane
          generate(i in 0..1) { lane[i].x=x }
        }
    """,
        "no compile-time indexed binding",
        id="array-incomplete",
    ),
    pytest.param(
        """
        module Bad {
          in input:rv<bits<8>> out output:rv<u8>
          §connect input -> output
        }
    """,
        "connection payload mismatch",
        id="connection",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r
          §memory m:mem<u8,2>{read_latency 0 collision read_first}
          rule read when 1 { m.read(0) }
        }
    """,
        "requires read_latency 1",
        id="scheduled-memory-latency",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r
          memory a:mem<u8,2>{read_latency 1 collision read_first}
          §memory b:mem<u8,2>{read_latency 1 collision read_first}
          rule read when 1 { a.read(0) b.read(0) }
        }
    """,
        "at most one rule-owned memory",
        id="scheduled-memory-count",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r
          memory a:mem<u8,2>{read_latency 1 collision read_first}
          §memory b:mem<u8,2>{read_latency 1 collision read_first}
          rule read when 1 { a.read(0) }
        }
    """,
        "global and rule-owned memory",
        id="mixed-memory-ownership",
    ),
    pytest.param(
        """
        module Bad {
          clock c reset r reg x:u8=0
          §memory m:mem<u8,2>{read_latency 1 collision read_first}
          m.read_address=0 m.write_enable=0 m.write_address=0
        }
    """,
        "has no 'write_data' control assignment",
        id="memory-state-missing-control",
    ),
    pytest.param(
        """
        enum Mode { Idle
          §Idle
        }
        module Bad { out y:u8 y=0 }
    """,
        "duplicate member",
        id="enum-member",
    ),
    pytest.param(
        """
        enum Mode:bits<2> { Idle=0
          §Busy=0
        }
        module Bad { out y:u8 y=0 }
    """,
        "duplicate code",
        id="enum-code",
    ),
    pytest.param(
        """
        enum Mode:bits<2> { Idle=0
          §Busy=4
        }
        module Bad { out y:u8 y=0 }
    """,
        "does not fit bits<2>",
        id="enum-code-width",
    ),
    pytest.param(
        """
        enum Mode:
          §s8 { Idle }
        module Bad { out y:u8 y=0 }
    """,
        "backing type must be bit",
        id="enum-backing",
    ),
    pytest.param(
        """
        struct Box<type T> { value:T }
        module Bad {
          in a:§Box<u8,u8>
        }
    """,
        "expects 1 parameters",
        id="struct-arity-use-site",
    ),
    pytest.param(
        """
        union U { A
          §A
        }
        module Bad { out y:u8 y=0 }
    """,
        "duplicate variant",
        id="union-variant",
    ),
    pytest.param(
        """
        union U { A { x:u8
          §x:u8
        } }
        module Bad { out y:u8 y=0 }
    """,
        "duplicate field",
        id="union-field",
    ),
    pytest.param(
        """
        struct S { value:u8 }
        union U { A { value:
          §S
        } }
        module Bad { out y:u8 y=0 }
    """,
        "must have a flat scalar type",
        id="union-field-type",
    ),
]


def _publish(server, path, text, version):
    uri = path_to_uri(path)
    params = {"textDocument": {"uri": uri, "version": version}}
    method = "textDocument/didChange"
    if version == 1:
        method = "textDocument/didOpen"
        params["textDocument"]["text"] = text
    else:
        params["contentChanges"] = [{"text": text}]
    messages = server.dispatch({"jsonrpc": "2.0", "method": method, "params": params})
    return {
        item["params"]["uri"]: item["params"]["diagnostics"]
        for item in messages
        if item["method"] == "textDocument/publishDiagnostics"
    }


@pytest.mark.parametrize("marked_source,message", CASES)
def test_semantic_error_points_to_its_source_after_unsaved_edits(
    tmp_path, marked_source, message
):
    marked_source = "// source\n\n" + dedent(marked_source).lstrip()
    assert marked_source.count("§") == 1
    original = marked_source.replace("§", "")
    path = tmp_path / "diagnostic.zhl"
    path.write_text(original)
    server = LspServer()
    shifted = "// shifted\n\n" + marked_source.replace("§", "/* 🔧 */ §")
    for version, marked in enumerate(
        (marked_source, shifted, shifted.replace("\n", "\r\n")),
        1,
    ):
        before, after = marked.split("§")
        text = before + after
        expected = {
            "line": before.count("\n"),
            "character": len(before.rsplit("\n", 1)[-1].encode("utf-16-le")) // 2,
        }
        diagnostics = _publish(server, path, text, version)[path_to_uri(path)]
        diagnostic = next(item for item in diagnostics if message in item["message"])
        assert diagnostic["range"]["start"] == expected
        # Root parse/preparation errors can precede logical-unit binding. Their
        # publication URI above still identifies the exact open document.
        assert diagnostic["data"]["source_unit"] in {None, path.name}
    assert _publish(server, path, "module Fixed {}", 4)[path_to_uri(path)] == []
    assert path.read_text() == original


@pytest.mark.parametrize(
    "declaration,needle,message",
    [
        ("struct S {\n a:u8\n a:u8\n}\n", "a:u8", "duplicate field"),
        (
            "module Child {\n clock c reset r\n fifo q:fifo<u8,2>\n}\n",
            "fifo",
            "requires global controls",
        ),
        (
            "fn helper<\n W=N+1,N=2\n>(x:u8)->u8{x}\n",
            "W=",
            "references later parameter",
        ),
    ],
)
def test_closed_import_diagnostic_belongs_to_definition_file(
    tmp_path, declaration, needle, message
):
    sources = tmp_path / "src"
    sources.mkdir()
    project = tmp_path / "zlang.toml"
    project.write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n'
    )
    leaf = sources / "leaf.zhl"
    leaf_text = "// imported source\n\n" + declaration
    leaf.write_text(leaf_text)
    source = "import demo.leaf\nmodule Top {"
    source += " child:Child" if "module Child" in declaration else ""
    source += "}\n"
    root = sources / "top.zhl"
    root.write_text(source)
    update_project_lock(project)
    publications = _publish(LspServer(), root, source, 1)
    diagnostic = next(
        item for item in publications[path_to_uri(leaf)] if message in item["message"]
    )
    line = max(i for i, text in enumerate(leaf_text.splitlines()) if needle in text)
    column = leaf_text.splitlines()[line].index(needle)
    assert diagnostic["range"]["start"] == {"line": line, "character": column}
    assert diagnostic["data"]["source_unit"] == "demo.leaf"
    assert publications.get(path_to_uri(root), []) == []


@pytest.mark.parametrize("prefix", ["inst child:", "child:"])
@pytest.mark.parametrize(
    "arguments,message",
    [
        ("§T=Missing", "cannot resolve type argument"),
        ("§Unknown=1", "unknown specialization parameter"),
        ("N=1,\n    §N=2", "assigned more than once"),
    ],
)
def test_multiline_specialization_error_selects_the_supplied_argument(
    tmp_path,
    prefix,
    arguments,
    message,
):
    marked = (
        "module Cell<type T,N=1>{in value:T}\n"
        "module Top {\n  " + prefix + "Cell<\n    " + arguments + "\n  >\n}\n"
    )
    path = tmp_path / "arguments.zhl"
    path.write_text(marked.replace("§", ""))
    server = LspServer()
    for version, text in enumerate(
        (marked, "// 🔧\r\n\r\n" + marked.replace("\n", "\r\n")), 1
    ):
        before, after = text.split("§")
        publications = _publish(server, path, before + after, version)
        diagnostic = next(
            item
            for item in publications[path_to_uri(path)]
            if message in item["message"]
        )
        assert diagnostic["range"]["start"] == {
            "line": before.count("\n"),
            "character": len(before.rsplit("\n", 1)[-1].encode("utf-16-le")) // 2,
        }


def test_diagnostic_location_boundary_preserves_specific_error_and_metadata():
    origin = SourceOrigin(SourceSpan(3, 2, 3, 5), "expression", "file.zhl", "a" * 64)
    outer = SourceOrigin(SourceSpan(2, 1, 4, 2), "declaration", "file.zhl", "a" * 64)
    error = DiagnosticError(
        "invalid", code="specific", primary=origin, notes=("note",), fixes=("fix",)
    )
    with pytest.raises(DiagnosticError) as raised, diagnostic_source(outer):
        raise error
    assert raised.value is error
    assert error.primary == origin
    assert error.code == "specific"
    assert error.notes == ("note",)
    assert error.fixes == ("fix",)


def test_diagnostic_location_boundary_fills_only_missing_origin():
    origin = SourceOrigin(
        SourceSpan(3, 2, 3, 5),
        "expression",
        "file.zhl",
        hashlib.sha256(b"source").hexdigest(),
    )
    error = DiagnosticError("invalid")
    with pytest.raises(DiagnosticError) as raised, diagnostic_source(origin):
        raise error
    assert raised.value is error
    assert error.primary == origin
