from __future__ import annotations

from pathlib import Path

import zlang
from zlang.workspace import update_project_lock


def test_native_runtime_packed_index_and_slice_use_existing_scalar_ops(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime-packed-selection"
    source_dir = root / "src"
    source_dir.mkdir(parents=True)
    manifest = root / "zlang.toml"
    manifest.write_text(
        'schema=1\n[project]\nname="runtime_packed_selection"\n'
        'version="1"\nsource-root="src"\n',
        encoding="utf-8",
    )
    source = source_dir / "runtime_packed_selection.zhl"
    source.write_text(
        "module RuntimePackedSelection { "
        "in raw:bits<8> in bit_index:u3 in offset:u2 "
        "out selected:bit out window:bits<3> "
        "selected=raw[bit_index] window=raw[offset +: 3] }",
        encoding="utf-8",
    )
    update_project_lock(manifest)

    instance = zlang.sim.load(source, top="RuntimePackedSelection", engine="native")
    with instance:
        for raw in (0x00, 0x01, 0x96, 0xD6, 0xFF):
            instance.set("raw", raw)
            for bit_index in range(8):
                instance.set("bit_index", bit_index)
                for offset in range(4):
                    instance.set("offset", offset)
                    assert instance.eval() == {
                        "selected": (raw >> bit_index) & 1,
                        "window": (raw >> offset) & 0b111,
                    }
