"""Exact process-local compilation reuse remains bound to physical bytes."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from zlang.incremental_workspace import IncrementalWorkspaceSession
from zlang.simulation_plan import SimulationPlan, _identity_bytes
from zlang.source_rebinding import TriviaRebinding


def test_exact_snapshot_reuses_compiler_products(tmp_path: Path) -> None:
    source = tmp_path / "top.zhl"
    text = "module Top { in x:u8 out y:u8 y=x }\n"
    source.write_text(text)
    workspace = IncrementalWorkspaceSession()
    first = workspace.file_snapshot(source, text, top="Top")
    planned = first.simulation_plan
    workspace.refresh(first)
    second = workspace.file_snapshot(source, text, top="Top")
    assert second is first
    assert second.simulation_plan is planned

    changed = text.replace("y=x", "y=x+0")
    source.write_text(changed)
    assert workspace.file_snapshot(source, changed, top="Top") is not first


def test_new_project_module_invalidates_exact_snapshot(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    manifest = tmp_path / "zlang.toml"
    manifest.write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n'
    )
    top = src / "top.zhl"
    top.write_text("module Top { out y:u8 y=1 }\n")
    from zlang.workspace import update_project_lock

    update_project_lock(manifest)
    workspace = IncrementalWorkspaceSession()
    first = workspace.file_snapshot(top, top.read_text(), project=manifest)
    assert workspace.file_snapshot(top, top.read_text(), project=manifest) is first
    (src / "new.zhl").write_text("module Other { out y:u8 y=2 }\n")
    # Whether the new source is accepted or makes the lock dirty, the old
    # compilation must never be returned for the changed source inventory.
    try:
        current = workspace.file_snapshot(top, top.read_text(), project=manifest)
    except Exception:
        return
    assert current is not first


def test_unrelated_project_module_revalidates_but_keeps_selected_products(
    tmp_path: Path,
) -> None:
    from zlang.workspace import update_project_lock

    src = tmp_path / "src"
    src.mkdir()
    manifest = tmp_path / "zlang.toml"
    manifest.write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n'
    )
    top = src / "top.zhl"
    other = src / "other.zhl"
    top.write_text("module Top { out y:u8 y=1 }\n")
    other.write_text("module Other { out y:u8 y=2 }\n")
    update_project_lock(manifest)
    workspace = IncrementalWorkspaceSession()
    first = workspace.file_snapshot(top, top.read_text(), project=manifest)
    planned = first.simulation_plan
    workspace.refresh(first)
    other.write_text("module Other { out y:u8 y=3 }\n")
    second = workspace.file_snapshot(top, top.read_text(), project=manifest)
    assert second is first and second.simulation_plan is planned


def test_imported_project_module_change_invalidates_selected_root(
    tmp_path: Path,
) -> None:
    from zlang.workspace import update_project_lock

    src = tmp_path / "src"
    src.mkdir()
    manifest = tmp_path / "zlang.toml"
    manifest.write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n'
    )
    top = src / "top.zhl"
    dep = src / "dep.zhl"
    top.write_text("import demo.dep module Top { out y:u9 y=inc(1) }\n")
    dep.write_text("fn inc(x:u8) -> u9 { x + 1 }\n")
    update_project_lock(manifest)
    workspace = IncrementalWorkspaceSession()
    first = workspace.file_snapshot(top, top.read_text(), project=manifest)
    first.check()
    workspace.refresh(first)
    dep.write_text("fn inc(x:u8) -> u9 { x + 2 }\n")
    assert workspace.file_snapshot(top, top.read_text(), project=manifest) is not first


def test_execution_identity_ignores_only_provenance() -> None:
    from zlang.simulation_plan import build_simulation_plan
    from zlang.compiler import compile_source

    selected = compile_source("module Top { in x:u8 out y:u8 y=x }")
    plan = build_simulation_plan(selected.ir)
    changed = dict(plan.payload)
    changed["canonical_ir_identity"] = "another-source-snapshot"
    changed["nodes"] = [
        {**node, "origins": [{"changed": "source-only"}]}
        for node in plan.payload["nodes"]
    ]
    encoded, _ = _identity_bytes(changed)
    restored = SimulationPlan.from_bytes(encoded)
    assert restored.identity != plan.identity
    assert restored.execution_identity == plan.execution_identity

    code_changed = dict(plan.payload)
    code_changed["module"] = "DifferentTop"
    encoded, _ = _identity_bytes(code_changed)
    assert (
        SimulationPlan.from_bytes(encoded).execution_identity != plan.execution_identity
    )


def test_execution_identity_is_stable_across_process_hash_seeds() -> None:
    script = (
        "from zlang.compiler import compile_source; "
        "from zlang.simulation_plan import build_simulation_plan; "
        "print(build_simulation_plan(compile_source("
        "'module Top { in x:u8 out y:u8 y=x }').ir).execution_identity)"
    )
    values = []
    for seed in ("0", "63"):
        result = subprocess.run(
            (sys.executable, "-c", script),
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        values.append(result.stdout.strip())
    assert len(values[0]) == 64 and values[0] == values[1]


def test_jit_reuses_native_code_but_not_source_provenance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import zlang.sim as sim

    class Runtime:
        def __init__(self) -> None:
            self.calls = 0

        def compile_plan_bytes(self, _encoded: bytes) -> object:
            self.calls += 1
            return object()

    runtime = Runtime()
    monkeypatch.setattr(sim, "_native_runtime", lambda: runtime)
    monkeypatch.setattr(sim, "_COMPILATION_WORKSPACE", IncrementalWorkspaceSession())
    monkeypatch.setattr(sim, "_PROGRAM_CACHE", sim.OrderedDict())
    source = tmp_path / "top.zhl"
    source.write_text("module Top { in x:u8 out y:u8 y=x }\n")
    first = sim.compile(source, top="Top", engine="jit")
    assert sim.compile(source, top="Top", engine="jit").plan is first.plan
    source.write_text(
        "// comment shifts all following source spans\n" + source.read_text()
    )
    changed = sim.compile(source, top="Top", engine="jit")
    assert changed.plan.identity != first.plan.identity
    assert changed.plan.execution_identity == first.plan.execution_identity
    assert changed._native is first._native
    assert runtime.calls == 1
    source.write_text("module Top { in x:u8 out y:u8 y=(x) }\n")
    regrouped = sim.compile(source, top="Top", engine="jit")
    assert regrouped.plan.identity != changed.plan.identity
    assert regrouped.plan.execution_identity == changed.plan.execution_identity
    assert regrouped._native is first._native and runtime.calls == 1


def test_parser_anchored_trivia_rebinds_exact_positions() -> None:
    old = "module Top { in x:u8 out y:u8 y=x }\n"
    new = "// note 😀\nmodule Top { in x:u8 out y:u8 y=x }\n"
    mapping = TriviaRebinding.between(old, new)
    assert mapping is not None
    assert len(mapping.syntax_identity) == 64
    from zlang.source import SourceOrigin, SourceSpan

    origin = SourceOrigin(
        SourceSpan(1, old.index("x }") + 1, 1, old.index("x }") + 2), "input"
    )
    changed = mapping.origin(origin)
    assert changed is not None
    assert changed.span.start_line == 2
    assert changed.span.start_column == origin.span.start_column
    spaced = "module Top { in x:u8 out y:u8 y = x }\n"
    spaced_mapping = TriviaRebinding.between(old, spaced)
    assert spaced_mapping is not None
    spaced_origin = spaced_mapping.origin(origin)
    assert spaced_origin is not None
    assert spaced_origin.span.start_column == spaced.index("x }") + 1
    assert TriviaRebinding.between(old, new.replace("y=x", "y=1")) is None
    assert TriviaRebinding.between(old, "module Top { in x:u8") is None


def test_lsp_navigation_rebinds_trivia_without_semantic_recompilation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from zlang.analysis_needs import AnalysisNeeds
    from zlang.tooling import (
        EditorDocumentSnapshot,
        EditorWorkspaceSnapshot,
        ToolingSession,
        definition_at,
        references_at,
    )
    import zlang.tooling as tooling

    source = tmp_path / "top.zhl"
    text = "fn inc(x:u8) -> u9 { x + 1 }\nmodule Top { in a:u8 out y:u9 y=inc(a) }\n"
    source.write_text(text)
    session = ToolingSession()
    session.set_editor_workspace(
        EditorWorkspaceSnapshot((EditorDocumentSnapshot(source, text, 1),))
    )
    original = tooling.check_file_snapshot
    calls = 0

    def observed(*args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["analysis_needs"] & AnalysisNeeds.DEFINITIONS
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    position = text.splitlines()[1].index("inc")
    first = definition_at(source, text, 1, position, _session=session)
    assert first is not None and first.target_origin.start_line == 1
    assert calls == 1

    edited = "// comment 😀\n" + text
    session.invalidate(source)
    session.set_editor_workspace(
        EditorWorkspaceSnapshot((EditorDocumentSnapshot(source, edited, 2),))
    )
    shifted = definition_at(source, edited, 2, position, _session=session)
    assert shifted is not None and shifted.target_origin.start_line == 2
    assert calls == 1
    found = references_at(
        source,
        edited,
        1,
        edited.splitlines()[1].index("inc"),
        include_declaration=True,
        _session=session,
    )
    assert found and calls == 1


def test_project_navigation_rebinds_trivia_with_logical_source_units(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from zlang.tooling import (
        EditorDocumentSnapshot,
        EditorWorkspaceSnapshot,
        ToolingSession,
        definition_at,
    )
    from zlang.workspace import update_project_lock
    import zlang.tooling as tooling

    src = tmp_path / "src"
    src.mkdir()
    manifest = tmp_path / "zlang.toml"
    manifest.write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n'
    )
    path = src / "top.zhl"
    text = "fn inc(x:u8) -> u9 { x + 1 }\nmodule Top { out y:u9 y=inc(1) }\n"
    path.write_text(text)
    update_project_lock(manifest)
    session = ToolingSession()
    session.set_editor_workspace(
        EditorWorkspaceSnapshot((EditorDocumentSnapshot(path, text, 1),))
    )
    original = tooling.check_file_snapshot
    calls = 0

    def observed(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    assert (
        definition_at(
            path, text, 1, text.splitlines()[1].index("inc"), _session=session
        )
        is not None
    )
    assert calls == 1
    changed = "// note\n" + text
    session.invalidate(path)
    session.set_editor_workspace(
        EditorWorkspaceSnapshot((EditorDocumentSnapshot(path, changed, 2),))
    )
    result = definition_at(
        path, changed, 2, changed.splitlines()[2].index("inc"), _session=session
    )
    assert result is not None and result.target_origin.start_line == 2
    assert calls == 1


def test_lsp_exact_cache_rechecks_new_project_source(
    tmp_path: Path, monkeypatch
) -> None:
    from zlang.tooling import ToolingSession
    from zlang.workspace import update_project_lock
    import zlang.tooling as tooling

    src = tmp_path / "src"
    src.mkdir()
    manifest = tmp_path / "zlang.toml"
    manifest.write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n'
    )
    top = src / "top.zhl"
    text = "module Top { out y:u8 y=1 }\n"
    top.write_text(text)
    update_project_lock(manifest)
    original = tooling.check_file_snapshot
    calls = 0

    def observed(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    session = ToolingSession()
    session.semantic_snapshot(top, text)
    session.semantic_snapshot(top, text)
    assert calls == 1
    (src / "other.zhl").write_text("module Other { out y:u8 y=2 }\n")
    session.semantic_snapshot(top, text)
    assert calls == 2


def test_strict_persistent_plan_restores_and_corruption_misses(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from zlang.persistent_simulation_plan import load_or_build

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("ZLANG_SIM_PLAN_CACHE", "persistent")
    source = tmp_path / "top.zhl"
    text = "module Top { in x:u8 out y:u8 y=x }\n"
    source.write_text(text)
    first = IncrementalWorkspaceSession().file_snapshot(source, text, top="Top")
    original = load_or_build(first)
    saved = list((tmp_path / "cache").rglob("*.json"))
    assert len(saved) == 1
    assert source.as_posix().encode("utf-8") not in saved[0].read_bytes()
    assert text.encode("utf-8") not in saved[0].read_bytes()
    second = IncrementalWorkspaceSession().file_snapshot(source, text, top="Top")

    def unexpected():
        raise AssertionError("primitive plan was rebuilt on persistent hit")

    monkeypatch.setattr(second, "_build_simulation_plan", unexpected)
    assert load_or_build(second).canonical_bytes == original.canonical_bytes
    saved[0].write_bytes(b"corrupt")
    third = IncrementalWorkspaceSession().file_snapshot(source, text, top="Top")
    assert load_or_build(third).canonical_bytes == original.canonical_bytes


def test_trivia_proof_rejects_changed_open_import(tmp_path: Path) -> None:
    from zlang.tooling import (
        EditorDocumentSnapshot,
        EditorWorkspaceSnapshot,
        ToolingSession,
    )
    from zlang.workspace import update_project_lock

    src = tmp_path / "src"
    src.mkdir()
    manifest = tmp_path / "zlang.toml"
    manifest.write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n'
    )
    top = src / "top.zhl"
    dep = src / "dep.zhl"
    text = "import demo.dep module Top { out y:u9 y=inc(1) }\n"
    dep_text = "fn inc(x:u8) -> u9 { x + 1 }\n"
    top.write_text(text)
    dep.write_text(dep_text)
    update_project_lock(manifest)
    session = ToolingSession()
    session.set_editor_workspace(
        EditorWorkspaceSnapshot(
            (
                EditorDocumentSnapshot(top, text, 1),
                EditorDocumentSnapshot(dep, dep_text, 1),
            )
        )
    )
    session.semantic_snapshot(top, text)
    session.invalidate(top)
    session.set_editor_workspace(
        EditorWorkspaceSnapshot(
            (
                EditorDocumentSnapshot(top, "// note\n" + text, 2),
                EditorDocumentSnapshot(dep, dep_text.replace("+ 1", "+ 2"), 2),
            )
        )
    )
    assert not session.trivia_diagnostic_proof(top, "// note\n" + text)
