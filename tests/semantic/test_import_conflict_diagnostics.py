# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Import collisions retain declaration locations without creating namespaces."""

import hashlib
import json

import pytest

from zlang.compiler import compile_file
from zlang.semantic import SemanticError
from zlang.workspace import update_project_lock


@pytest.mark.parametrize("aliased", (False, True))
@pytest.mark.parametrize("declaration,name", (
    ("struct Packet { value:u8 }", "Packet"),
    ("fn choose(value:u8)->u8 {value}", "choose"),
    ("type Word=u8", "Word"),
    ("enum Mode { A\n B }", "Mode"),
    ("module Child { in x:u8 out y:u8 y=x }", "Child"),
))
def test_import_conflict_identifies_the_second_declaration(tmp_path, aliased, declaration, name):
    sources = tmp_path / "src"
    sources.mkdir()
    manifest = tmp_path / "zlang.toml"
    manifest.write_text('schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n')
    left = "// first owner\n\n" + declaration + "\n"
    right = "// second owner\n\n/* 🔧 */ " + declaration + "\n"
    (sources / "left.zhl").write_text(left)
    second = sources / "right.zhl"
    second.write_text(right)
    root = sources / "top.zhl"
    root.write_text(
        f"import demo.left{' as a' if aliased else ''}\n"
        f"import demo.right{' as b' if aliased else ''}\nmodule Top {{}}\n"
    )
    update_project_lock(manifest)
    with pytest.raises(SemanticError) as caught:
        compile_file(root, top="Top")
    error = caught.value
    assert error.code == "ZL-IMPORT-CONFLICT"
    assert error.primary.source_unit == "demo.right"
    assert error.primary.digest == hashlib.sha256(right.encode()).hexdigest()
    assert error.primary.span.start_line == 3
    assert error.primary.span.start_column == right.splitlines()[2].index(name) + 1
    assert error.physical_source == second
    assert any("demo.left:3:" in note for note in error.notes)
    assert any("aliases" in note and "identity" in note for note in error.notes)
    assert error.fixes and not error.machine_fixes
    assert json.loads(error.diagnostic.to_json())["primary"]["source_unit"] == "demo.right"


def test_unique_aliased_declarations_still_coexist(tmp_path):
    sources = tmp_path / "src"
    sources.mkdir()
    manifest = tmp_path / "zlang.toml"
    manifest.write_text('schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n')
    (sources / "left.zhl").write_text("struct PacketA { value:u8 } fn choose_a(v:u8)->u8 {v}")
    (sources / "right.zhl").write_text("struct PacketB { value:u8 } fn choose_b(v:u8)->u8 {v}")
    root = sources / "top.zhl"
    root.write_text("""import demo.left as a
import demo.right as b
module Top {in x:a.PacketA in y:b.PacketB out sum:u9 sum=a.choose_a(x.value)+b.choose_b(y.value)}
""")
    update_project_lock(manifest)
    assert compile_file(root).ir.name == "Top"
