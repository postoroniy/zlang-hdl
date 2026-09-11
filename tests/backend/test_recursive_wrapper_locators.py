"""Production recursive locators include the public ABI's private state root."""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact, emit_formal_artifact
from zlang.backend.systemverilog.emitter import physical_state_root_path
from zlang.backend.systemverilog.simulation_state import (
    build_systemverilog_simulation_state_bundle,
)
from zlang.compiler import compile_source
from zlang.formal import build_recursive_formal_design


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_recursive_locators_match_public_wrapper_state_root(tmp_path: Path) -> None:
    variants = (
        ("struct", "Pair", "p.a", "Pair {a=c.y b=p.b}", ""),
        ("vector", "vec<2,u8>", "p[0]", "[c.y,p[1]]", ""),
        ("collision", "Pair", "p.a", "Pair {a=c.y b=p.b}", "in zlang_top_core:bit"),
        ("scalar", "u8", "p", "c.y", ""),
    )
    for label, port_type, payload, result, extra_port in variants:
        source = f"""
        struct Pair {{ a:u8 b:u8 }}
        module Child {{
            clock clk reset rst
            in x:u8 out y:u8
            reg count:u8=0 count<-x y=count
        }}
        module Top {{
            clock clk reset rst
            in p:{port_type} out q:{port_type} {extra_port}
            out root_value:u8
            reg root_count:u8=0 root_count<-{payload} root_value=root_count
            c:Child {{x={payload}}}
            q={result}
        }}
        """
        compilation = compile_source(source)
        design = build_recursive_formal_design(compilation.ir)
        artifact = emit_artifact(
            compilation.ir, recursive_design=design,
            selected_ir_identity=compilation.selected_ir_identity,
        )
        assert artifact == emit_artifact(
            compilation.ir, recursive_design=design,
            selected_ir_identity=compilation.selected_ir_identity,
        )
        assert artifact.text == emit_artifact(compilation.ir).text
        restored = BackendArtifact.from_json(artifact.to_json())
        assert restored.to_json() == artifact.to_json()
        assert restored.recursive_bindings == artifact.recursive_bindings
        bundle = build_systemverilog_simulation_state_bundle(compilation.ir, artifact)
        state_root = physical_state_root_path(compilation.ir)
        expected_core = () if label == "scalar" else (state_root[2],)
        assert len(state_root) == (2 if label == "scalar" else 3)
        if label == "collision":
            assert state_root[2].startswith("zlang_top_core_")
        elif label != "scalar":
            assert state_root[2] == "zlang_top_core"

        registers = [b for b in artifact.recursive_bindings if b.local_semantic_id.startswith("register:")]
        assert len(registers) == 2
        assert {
            ".".join(("TOP", artifact.module, *b.rtl_path, b.signal_token))
            for b in registers
        } == {item.vpi_path for item in bundle.locators}
        for binding in registers:
            is_root = len(binding.physical_instance_path) == 1
            assert binding.rtl_path == expected_core + (() if is_root else ("c",))
            if is_root:
                assert binding.rtl_module == ("Top" if label == "scalar" else "Top_zlang_core")
            else:
                assert binding.rtl_module.startswith("Child_s")
            # These are production/debug locations, not executable formal
            # observations. The locator repair must not change applicability.
            assert not binding.physical_available
            assert binding.formal_observation_token is None
            body = re.search(
                r"module " + re.escape(binding.rtl_module) + r" \(.*?endmodule",
                artifact.text, re.DOTALL,
            )
            assert body is not None
            assert re.search(r"\blogic\s+\[7:0\]\s+" + re.escape(binding.signal_token) + r";", body.group())

        formal = emit_formal_artifact(compilation.ir, design)
        for binding in formal.recursive_bindings:
            if not binding.local_semantic_id.startswith("register:"):
                continue
            assert binding.physical_available
            assert binding.rtl_module == "Top__formal"
            assert binding.rtl_path == ()
            assert binding.formal_observation_token
        for mode, emitted in (("production", artifact), ("formal", formal)):
            rtl = tmp_path / f"{label}_{mode}.sv"
            rtl.write_text(emitted.text)
            lint = subprocess.run(
                ["verilator", "--lint-only", "--sv", "-Wall", "-Wno-DECLFILENAME",
                 "-Wno-UNUSEDSIGNAL", "-Wno-UNUSEDPARAM", str(rtl)],
                capture_output=True, text=True, timeout=30,
            )
            assert lint.returncode == 0, lint.stdout + lint.stderr
