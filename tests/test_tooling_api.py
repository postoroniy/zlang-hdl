from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from zlang.analysis_needs import AnalysisNeeds
from zlang.compiler import check_file_snapshot, compile_source
from zlang.tooling import (
    TOOLING_API_SCHEMA,
    TOOLING_HOVER_SCHEMA,
    TOOLING_DEFINITION_SCHEMA,
    TOOLING_REFERENCE_SCHEMA,
    TOOLING_RENAME_SCHEMA,
    TOOLING_COMPLETION_SCHEMA,
    TOOLING_DIAGNOSTIC_EDIT_SCHEMA,
    TOOLING_SIGNATURE_HELP_SCHEMA,
    TOOLING_SEMANTIC_TOKEN_SCHEMA,
    ToolingError,
    ToolingRenameError,
    ToolingHover,
    ToolingDefinition,
    ToolingReference,
    ToolingCompletion,
    ToolingDiagnosticEdit,
    ToolingDiagnosticFix,
    ToolingSignatureHelp,
    ToolingSemanticToken,
    ToolingSession,
    check_snapshot,
    document_symbols,
    discover_project,
    resolve_direct_imports,
    source_facts,
    unwritten_register_warnings,
    tooling_identity,
    workspace_index,
    hover_at,
    definition_at,
    references_at,
    rename_at,
    completion_at,
    signature_help_at,
    semantic_tokens,
)
from zlang.workspace import WorkspaceError, update_project_lock
from zlang.semantic import SemanticError


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    source = root / "src"
    source.mkdir(parents=True)
    (root / "zlang.toml").write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n'
    )
    (source / "dep.zhl").write_text("module Dep { out y:u8 y=1 }\n")
    (source / "top.zhl").write_text(
        "import demo.dep module Top { out y:u8 y=1 }\n"
    )
    update_project_lock(root / "zlang.toml")
    return root


def test_tooling_identity_is_stable_and_compiler_owned() -> None:
    first = tooling_identity()
    assert first == tooling_identity()
    assert first.api_schema == TOOLING_API_SCHEMA
    assert first.source_suffix == ".zhl"
    assert first.capability_count > 0
    assert len(first.capability_identity) == 64


def test_source_facts_and_direct_import_resolution(tmp_path: Path) -> None:
    root = _project(tmp_path)
    top = root / "src/top.zhl"
    facts = source_facts(top.read_text())
    assert facts.parsed is True
    assert facts.imports == ("demo.dep",)
    assert facts.declarations == ("Top",)
    resolved = resolve_direct_imports(top, facts.imports)
    assert len(resolved) == 1
    assert resolved[0].logical_path == "demo.dep"
    assert resolved[0].source_path == (root / "src/dep.zhl").resolve()
    assert resolved[0].error is None
    assert source_facts("not valid {").parsed is False


def test_project_and_workspace_records_hide_compiler_internal_models(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    location = discover_project(root / "src/top.zhl")
    assert location is not None
    assert location.manifest_path == (root / "zlang.toml").resolve()
    index = workspace_index(location.manifest_path)
    assert [item.logical_path for item in index.root_modules] == [
        "demo.dep",
        "demo.top",
    ]
    assert index.dependency_closure("demo.top") == ("demo.dep",)
    with pytest.raises(ToolingError, match="not indexed"):
        index.dependency_closure("demo.missing")


def test_semantic_check_returns_stable_success_and_failure_records(
    tmp_path: Path,
) -> None:
    source = tmp_path / "top.zhl"
    text = "module Top { in a:u8 out y:u9 y=a+1 }\n"
    source.write_text(text)
    passed = check_snapshot(source, text)
    assert passed.status == "passed"
    assert passed.phase == "complete"
    assert passed.resolved_top == "Top"
    assert passed.module_identity
    failed = check_snapshot(source, "module Top { in a:u8 out y:u7 y=a }\n")
    assert failed.status == "failed"
    assert failed.phase == "semantic"
    assert failed.diagnostics[0].code.startswith("ZL-")


def test_unwritten_register_is_only_an_editor_warning(tmp_path: Path) -> None:
    source = tmp_path / "Top.zhl"
    text = (
        "module Top {\n"
        "  clock clk reset rst\n"
        "  out y : u8\n"
        "  reg idle : u8 = 0\n"
        "  reg count : u8 = 0\n"
        "  count <- count\n"
        "  y = idle\n"
        "}\n"
    )
    source.write_text(text, encoding="utf-8")
    checked = check_snapshot(source, text)
    assert checked.status == "passed"
    assert checked.diagnostics == ()

    warnings = unwritten_register_warnings(text)
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.code == "ZL-REGISTER-NEVER-WRITTEN"
    assert warning.severity == "warning"
    assert warning.message == (
        "register 'idle' has no next-state or rule assignment; "
        "it will hold its reset value"
    )
    assert warning.primary is not None
    assert (
        warning.primary.start_line,
        warning.primary.start_column,
        warning.primary.end_line,
        warning.primary.end_column,
    ) == (4, 7, 4, 11)
    assert unwritten_register_warnings(
        (Path(__file__).resolve().parents[1] / "examples/rule_counter.zhl").read_text()
    ) == ()
    separate_modules = (
        "module A { clock clk reset rst out y:u8 reg x:u8=0 y=x }\n"
        "module B { clock clk reset rst out y:u8 reg x:u8=0 x <- x y=x }\n"
    )
    assert [item.primary.start_line for item in unwritten_register_warnings(
        separate_modules
    )] == [1]
    assert unwritten_register_warnings("module Broken { reg x:u8=0") == ()


def test_identical_duplicate_import_projects_exact_machine_edit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "top.zhl"
    text = (
        "import std.bus.reg\n"
        "import std.bus.reg\n"
        "module Top { out y:u8 y=0 }\n"
    )
    source.write_text(text)

    result = check_snapshot(source, text)
    diagnostic = result.diagnostics[0]
    assert diagnostic.code == "ZL-IMPORT-DUPLICATE"
    assert diagnostic.fixes == ("remove the duplicate import declaration",)
    assert len(diagnostic.machine_fixes) == 1
    fix = diagnostic.machine_fixes[0]
    assert isinstance(fix, ToolingDiagnosticFix)
    assert fix.title == "Remove duplicate import declaration"
    assert len(fix.edits) == 1
    edit = fix.edits[0]
    assert isinstance(edit, ToolingDiagnosticEdit)
    assert edit.source_path == source.resolve()
    assert edit.replacement == ""
    assert (
        edit.origin.start_line,
        edit.origin.start_column,
        edit.origin.end_line,
        edit.origin.end_column,
    ) == (2, 1, 2, 19)

    lines = text.splitlines(keepends=True)
    start = sum(len(item) for item in lines[: edit.origin.start_line - 1])
    start += edit.origin.start_column - 1
    end = sum(len(item) for item in lines[: edit.origin.end_line - 1])
    end += edit.origin.end_column - 1
    assert text[start:end] == "import std.bus.reg"
    fixed = text[:start] + edit.replacement + text[end:]
    assert check_snapshot(source, fixed).status == "passed"
    assert TOOLING_DIAGNOSTIC_EDIT_SCHEMA == 1


def test_unsafe_diagnostic_suggestions_do_not_project_machine_edits(
    tmp_path: Path,
) -> None:
    source = tmp_path / "top.zhl"
    width_error = "module Top { in a:u8 out y:u7 y=a }\n"
    source.write_text(width_error)
    width_diagnostic = check_snapshot(source, width_error).diagnostics[0]
    assert width_diagnostic.fixes == ("use an explicit exact-width conversion",)
    assert width_diagnostic.machine_fixes == ()

    ambiguous_imports = (
        "import std.bus.reg as first\n"
        "import std.bus.reg as second\n"
        "module Top { out y:u8 y=0 }\n"
    )
    source.write_text(ambiguous_imports)
    import_diagnostic = check_snapshot(source, ambiguous_imports).diagnostics[0]
    assert import_diagnostic.code == "ZL-IMPORT-DUPLICATE"
    assert import_diagnostic.machine_fixes == ()


def test_document_symbols_project_parser_structure_and_ranges() -> None:
    source = (
        "module Top {\n"
        "    in a:u8\n"
        "    out y:u9\n"
        "    y = a + 1\n"
        "}\n"
    )
    symbols = document_symbols(source)
    assert len(symbols) == 1
    top = symbols[0]
    assert (top.name, top.kind) == ("Top", "module")
    assert [item.name for item in top.children] == ["a", "y"]
    assert top.children[0].range is not None
    assert top.children[0].range.start_line == 2
    assert top.children[0].selection_range == top.children[0].range
    assert top.children[1].kind == "field"


def test_document_symbols_support_declaration_units_and_nested_structs() -> None:
    source = "struct Pair { left:u8 right:u8 }\nfn add(a:u8) { a }\n"
    symbols = document_symbols(source)
    assert [item.name for item in symbols] == ["Pair", "add"]
    assert symbols[0].kind == "struct"
    assert [item.name for item in symbols[0].children] == ["left", "right"]
    assert symbols[1].kind == "function"


def test_document_symbols_are_immutable_and_malformed_source_is_empty() -> None:
    from dataclasses import FrozenInstanceError

    symbols = document_symbols("module Top { out y:u8 y=1 }\n")
    try:
        symbols[0].name = "Other"  # type: ignore[misc]
    except FrozenInstanceError:
        pass
    else:  # pragma: no cover - protects the tooling API contract
        raise AssertionError("document symbols must be immutable")
    assert document_symbols("module Top {").__class__ is tuple
    assert document_symbols("module Top {") == ()


def test_hover_projection_exposes_compiler_owned_scalar_and_fixed_facts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    text = "module Top { in a:u8 in s:s8 in f:SF8.8 out y:SF9.8 y=f }\n"
    source.write_text(text)
    unsigned = hover_at(source, text, 0, text.index("a"))
    signed = hover_at(source, text, 0, text.index("s:s8"))
    fixed = hover_at(source, text, 0, text.index("f:SF8.8"))
    assert isinstance(unsigned, ToolingHover)
    assert unsigned.type_text == "u8"
    assert (unsigned.width, unsigned.signedness, unsigned.port_direction) == (
        8,
        "unsigned",
        "input",
    )
    assert signed is not None and signed.signedness == "signed"
    assert fixed is not None
    assert (fixed.type_text, fixed.width, fixed.fixed_point) == (
        "fixed<16,8>",
        16,
        "fixed<16,8>",
    )
    assert fixed.origin is not None
    assert fixed.origin.start_line == 1
    assert hover_at(source, text, 0, 0) is None
    assert hover_at(source, text, 10, 0) is None
    assert hover_at(source, "module Top {", 0, 7) is None
    assert TOOLING_HOVER_SCHEMA == 1


def test_hover_projection_exposes_function_signature_and_environment_errors(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    text = "fn add(x:u8, y:u8) -> u9 { x+y }\nmodule Top { out y:u9 y=1 }\n"
    source.write_text(text)
    hover = hover_at(source, text, 0, text.index("add"))
    assert hover is not None
    assert hover.kind == "function"
    assert hover.signature == "fn add(x : u8, y : u8) -> u9"
    assert hover.type_text == "u9"
    with pytest.raises(ToolingError):
        hover_at(tmp_path / "missing.zhl", text, 0, 0)


def test_definition_projection_uses_compiler_resolution_and_cross_file_origin(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    dependency = root / "src/dep.zhl"
    dependency.write_text("fn inc(x:u8) -> u9 { x + 1 }\n")
    top = root / "src/top.zhl"
    text = "import demo.dep\nmodule Top { in a:u8 out y:u9 y=inc(a) }\n"
    top.write_text(text)
    update_project_lock(root / "zlang.toml")
    definition = definition_at(top, text, 1, text.splitlines()[1].index("inc"))
    assert isinstance(definition, ToolingDefinition)
    assert definition.name == "inc"
    assert definition.kind == "function"
    assert definition.target_path == dependency.resolve()
    assert definition.target_origin.start_line == 1
    assert definition.target_origin.start_column == 4
    assert TOOLING_DEFINITION_SCHEMA == 1


def test_definition_projection_returns_none_for_unresolved_position(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    text = "module Top { out y:u8 y=1 }\n"
    source.write_text(text)
    assert definition_at(source, text, 0, 0) is None


def test_definition_projection_resolves_80211a_module_instance_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time
    import zlang.tooling as tooling

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("ZLANG_LSP_SYMBOL_CACHE", "persistent")
    project = Path("examples/projects/80211a_transmitter").resolve()
    source = project / "src/transmitter.zhl"
    text = source.read_text(encoding="utf-8")
    expected = {
        "IeeePacketMapper64": project / "src/mapper.zhl",
        "IeeeFramedIFFT64": project / "src/ifft.zhl",
        "IeeeIFFTFramedOutputBoundary": project / "src/ifft.zhl",
    }

    semantic = importlib.import_module("zlang.semantic.analyze")

    def unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError(
            "completion metadata was collected by module definition_at"
        )

    monkeypatch.setattr(semantic, "_record_completion_scope", unexpected)
    monkeypatch.setattr(semantic, "_completion_candidates", unexpected)
    monkeypatch.setattr(semantic, "_completion_function_detail", unexpected)
    original = tooling.check_file_snapshot
    calls = 0

    def observed(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    session = ToolingSession()

    cold_started = time.perf_counter()
    for name, target_path in expected.items():
        line = next(
            index for index, value in enumerate(text.splitlines()) if name in value
        )
        definition = definition_at(
            source,
            text,
            line,
            text.splitlines()[line].index(name),
            _session=session,
        )
        assert isinstance(definition, ToolingDefinition)
        assert (definition.name, definition.kind) == (name, "module")
        assert definition.target_path == target_path.resolve()
        assert definition.target_origin.construct == f"module {name}"
        assert definition.target_origin.start_column == 8
    cold_elapsed = time.perf_counter() - cold_started

    assert calls == 1
    assert len(tuple((tmp_path / "cache/zlang-hdl/lsp").rglob("*.json"))) == 1
    result = session.semantic_snapshot(
        source,
        text,
        analysis_needs=AnalysisNeeds.DEFINITIONS,
    )
    assert calls == 1
    root_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    module_records = {
        item.name: item
        for item in result.definition_resolutions
        if item.kind == "module" and item.name in expected
    }
    assert set(module_records) == set(expected)
    for name, record in module_records.items():
        assert record.occurrence.digest == root_digest
        assert record.target.digest == hashlib.sha256(
            expected[name].read_bytes()
        ).hexdigest()

    mapper_line = next(
        index
        for index, value in enumerate(text.splitlines())
        if "IeeePacketMapper64" in value
    )
    warm_started = time.perf_counter()
    disk_hit = definition_at(
        source,
        text,
        mapper_line,
        text.splitlines()[mapper_line].index("IeeePacketMapper64"),
        _session=ToolingSession(),
    )
    warm_elapsed = time.perf_counter() - warm_started
    assert disk_hit is not None
    assert disk_hit.target_path == expected["IeeePacketMapper64"].resolve()
    assert calls == 1
    assert warm_elapsed * 10 < cold_elapsed


def test_definition_projection_resolves_80211a_named_type_target() -> None:
    project = Path("examples/projects/80211a_transmitter").resolve()
    source = project / "src/transmitter.zhl"
    text = source.read_text(encoding="utf-8")
    line = next(
        index for index, value in enumerate(text.splitlines())
        if "command : rv<WifiTxCommand>" in value
    )
    definition = definition_at(
        source, text, line, text.splitlines()[line].index("WifiTxCommand")
    )
    assert isinstance(definition, ToolingDefinition)
    assert (definition.name, definition.kind) == ("WifiTxCommand", "type")
    assert definition.target_path == (project / "src/data_types.zhl").resolve()
    assert definition.target_origin.construct == "type WifiTxCommand"
    # Protocol/type constructors are compiler intrinsics, not user source
    # declarations; definition lookup must not fabricate a project target.
    assert definition_at(
        source,
        text,
        line,
        text.splitlines()[line].index("rv"),
    ) is None


def test_definition_projection_resolves_80211a_connection_endpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover live F12 on ``command -> packet_mapper.command`` and disk reuse."""

    import zlang.tooling as tooling

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("ZLANG_LSP_SYMBOL_CACHE", "persistent")
    original = tooling.check_file_snapshot
    calls = 0

    def observed(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    project = Path("examples/projects/80211a_transmitter").resolve()
    source = project / "src/transmitter.zhl"
    text = source.read_text(encoding="utf-8")
    line = next(
        index for index, value in enumerate(text.splitlines())
        if "command -> packet_mapper.command" in value
    )
    source_text = text.splitlines()[line]
    cases = (
        (source_text.index("command"), source, 14, 8, "port"),
        (source_text.index("packet_mapper"), source, 19, 5, "instance"),
        (source_text.rindex("command"), project / "src/mapper.zhl", 361, 8, "port"),
    )
    session = ToolingSession()
    for position, target, target_line, target_column, kind in cases:
        definition = definition_at(
            source, text, line, position, _session=session
        )
        assert definition is not None
        assert definition.kind == kind
        assert definition.target_path == target.resolve()
        assert (
            definition.target_origin.start_line,
            definition.target_origin.start_column,
        ) == (target_line, target_column)
    assert calls == 1

    warm = definition_at(
        source,
        text,
        line,
        source_text.index("command") + len("command"),
        _session=ToolingSession(),
    )
    assert warm is not None
    assert warm.target_path == source
    assert calls == 1


def test_definition_module_target_does_not_match_instance_name_spelling() -> None:
    project = Path("examples/projects/80211a_transmitter").resolve()
    source = project / "src/transmitter.zhl"
    text = source.read_text(encoding="utf-8")
    line = next(
        index for index, value in enumerate(text.splitlines())
        if "packet_mapper :" in value
    )
    assert definition_at(
        source,
        text,
        line,
        text.splitlines()[line].index("packet_mapper"),
    ) is None


def test_definition_projection_resolves_named_type_and_enum_member_spans(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    text = (
        "struct WifiTxCommand { opcode:u8 }\n"
        "enum TxState { Idle Active }\n"
        "module Top { out command:WifiTxCommand out state:TxState "
        "command=WifiTxCommand{opcode=0} state=TxState.Idle }\n"
    )
    source.write_text(text)
    command_line = next(
        index for index, value in enumerate(text.splitlines())
        if "command:WifiTxCommand" in value
    )
    command = definition_at(
        source,
        text,
        command_line,
        text.splitlines()[command_line].index("WifiTxCommand"),
    )
    assert isinstance(command, ToolingDefinition)
    assert (command.name, command.kind) == ("WifiTxCommand", "type")
    assert command.target_path == source.resolve()
    assert command.target_origin.start_line == 1
    assert command.target_origin.start_column == 8

    member_line = next(
        index for index, value in enumerate(text.splitlines())
        if "TxState.Idle" in value
    )
    member = definition_at(
        source,
        text,
        member_line,
        text.splitlines()[member_line].index("Idle"),
    )
    assert isinstance(member, ToolingDefinition)
    assert (member.name, member.kind) == ("Idle", "enum_member")
    assert member.target_path == source.resolve()
    assert member.target_origin.start_line == 2
    assert member.target_origin.start_column == 16


def test_enum_boundary_diagnostic_distinguishes_direct_and_aggregate_types() -> None:
    direct = (
        "enum TxState { Idle Active } "
        "module Top { in state:TxState out y:u8 y=0 }"
    )
    with pytest.raises(SemanticError) as direct_error:
        compile_source(direct)
    assert str(direct_error.value) == (
        "top-level input 'state' cannot expose enum type TxState; "
        "use an internal child interface"
    )

    aggregate = (
        "enum TxState { Idle Active } "
        "struct FrameBeat { state:TxState payload:u8 } "
        "module Top { in input:FrameBeat out y:u8 y=0 }"
    )
    with pytest.raises(SemanticError) as aggregate_error:
        compile_source(aggregate)
    assert str(aggregate_error.value) == (
        "top-level input 'input' cannot expose type FrameBeat because it "
        "contains an enum-valued field; use an internal child interface"
    )
    assert "pack/unpack" not in str(aggregate_error.value)


def test_definition_query_does_not_collect_completion_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    semantic = importlib.import_module("zlang.semantic.analyze")
    source = tmp_path / "Top.zhl"
    text = (
        "fn inc(x:u8) -> u9 { x + 1 }\n"
        "module Top { in a:u8 out y:u9 y=inc(a) }\n"
    )
    source.write_text(text)

    def unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError("completion metadata was collected by definition_at")

    monkeypatch.setattr(semantic, "_record_completion_scope", unexpected)
    monkeypatch.setattr(semantic, "_completion_candidates", unexpected)
    monkeypatch.setattr(semantic, "_completion_function_detail", unexpected)

    definition = definition_at(source, text, 1, text.splitlines()[1].index("inc"))
    assert isinstance(definition, ToolingDefinition)
    assert definition.name == "inc"


def test_completion_query_still_collects_function_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    semantic = importlib.import_module("zlang.semantic.analyze")
    source = tmp_path / "Top.zhl"
    text = (
        "fn inc(x:u8) -> u9 { x + 1 }\n"
        "module Top { in a:u8 out y:u9 y=inc(a) }\n"
    )
    source.write_text(text)
    original = semantic._completion_function_detail
    calls: list[str] = []

    def observed(*args: object, **kwargs: object) -> str:
        calls.append("detail")
        return original(*args, **kwargs)

    monkeypatch.setattr(semantic, "_completion_function_detail", observed)
    candidates = completion_at(source, text, 1, text.splitlines()[1].index("inc"))
    assert any(item.name == "inc" for item in candidates)
    assert calls


def test_snapshot_analysis_needs_are_empty_by_default_and_explicitly_scoped(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    text = (
        "fn inc(x:u8) -> u9 { x + 1 }\n"
        "module Top { in a:u8 out y:u9 y=inc(a) }\n"
    )
    source.write_text(text)

    ordinary = check_file_snapshot(source, text)
    assert ordinary.definition_resolutions == ()
    assert ordinary.completion_scopes == ()
    assert ordinary.signature_help_calls == ()

    definitions = check_file_snapshot(
        source, text, analysis_needs=AnalysisNeeds.DEFINITIONS
    )
    assert definitions.definition_resolutions
    assert definitions.completion_scopes == ()
    assert definitions.signature_help_calls == ()

    completion = check_file_snapshot(
        source,
        text,
        analysis_needs=AnalysisNeeds.DEFINITIONS | AnalysisNeeds.COMPLETION,
    )
    assert completion.completion_scopes

    signature = check_file_snapshot(
        source, text, analysis_needs=AnalysisNeeds.SIGNATURE_HELP
    )
    assert signature.signature_help_calls


def test_tooling_session_reuses_and_upgrades_semantic_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zlang.tooling as tooling

    source = tmp_path / "Top.zhl"
    text = (
        "fn inc(x:u8) -> u9 { x + 1 }\n"
        "module Top { in a:u8 out y:u9 y=inc(a) }\n"
    )
    source.write_text(text)
    original = tooling.check_file_snapshot
    calls = 0
    needs_seen: list[AnalysisNeeds] = []

    def observed(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        needs_seen.append(AnalysisNeeds(kwargs["analysis_needs"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    session = ToolingSession()
    position = text.splitlines()[1].index("inc")
    assert definition_at(source, text, 1, position, _session=session) is not None
    assert definition_at(source, text, 1, position, _session=session) is not None
    assert calls == 1
    assert needs_seen == [AnalysisNeeds.DEFINITIONS]

    hover_position = text.splitlines()[1].index("a")
    assert hover_at(source, text, 1, hover_position, _session=session) is not None
    assert hover_at(source, text, 1, hover_position, _session=session) is not None
    assert calls == 1

    candidates = completion_at(
        source,
        text,
        1,
        position,
        _session=session,
    )
    assert any(item.name == "inc" for item in candidates)
    assert calls == 2
    assert needs_seen == [
        AnalysisNeeds.DEFINITIONS,
        AnalysisNeeds.DEFINITIONS | AnalysisNeeds.COMPLETION,
    ]

    upgraded = ToolingSession()
    assert hover_at(source, text, 1, hover_position, _session=upgraded) is not None
    assert hover_at(source, text, 1, hover_position, _session=upgraded) is not None
    assert calls == 3


def test_tooling_session_retries_one_physical_snapshot_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zlang.tooling as tooling

    source = tmp_path / "Top.zhl"
    text = "module Top { out y:u8 y=1 }\n"
    source.write_text(text, encoding="utf-8")
    original = tooling.check_file_snapshot
    calls = 0

    def raced(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkspaceError(
                "root source 'Top' changed after its compilation snapshot"
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", raced)
    result = ToolingSession().semantic_snapshot(source, text)
    assert result.ir.name == "Top"
    assert calls == 2


def test_tooling_session_invalidation_for_changed_root_and_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zlang.tooling as tooling

    root = _project(tmp_path)
    dependency = root / "src/dep.zhl"
    dependency.write_text("fn inc(x:u8) -> u9 { x + 1 }\n")
    top = root / "src/top.zhl"
    text = (
        "import demo.dep\n"
        "module Top { in a:u8 out y:u9 y=inc(a) }\n"
    )
    top.write_text(text)
    update_project_lock(root / "zlang.toml")

    original = tooling.check_file_snapshot
    calls = 0

    def observed(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    session = ToolingSession()
    position = text.splitlines()[1].index("inc")
    assert definition_at(top, text, 1, position, _session=session) is not None
    assert definition_at(top, text, 1, position, _session=session) is not None
    assert calls == 1

    dependency.write_text("fn inc(x:u8) -> u9 { x + 2 }\n")
    # A changed dependency invalidates the cached environment fingerprint.
    # The locked workspace may still accept the updated source as a normal
    # semantic snapshot, so assert the observable guarantee directly: the
    # compiler is invoked again rather than reusing the stale product.
    assert definition_at(top, text, 1, position, _session=session) is not None
    assert calls == 2

    changed = text.replace("inc(a)", "inc(1)")
    top.write_text(changed)
    update_project_lock(root / "zlang.toml")
    session.invalidate(top)
    assert definition_at(top, changed, 1, changed.splitlines()[1].index("inc"), _session=session) is not None
    assert definition_at(top, changed, 1, changed.splitlines()[1].index("inc"), _session=session) is not None
    assert calls == 3


def test_symbol_cache_survives_semantic_lru_eviction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zlang.tooling as tooling

    monkeypatch.setenv("ZLANG_LSP_SYMBOL_CACHE", "memory")
    source = tmp_path / "Root.zhl"
    text = (
        "fn inc(x:u8) -> u9 { x + 1 }\n"
        "module Root { in a:u8 out y:u9 y=inc(a) }\n"
    )
    source.write_text(text, encoding="utf-8")
    original = tooling.check_file_snapshot
    calls = 0

    def observed(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    session = ToolingSession()
    position = text.splitlines()[1].index("inc")
    assert definition_at(source, text, 1, position, _session=session) is not None
    for index in range(session._MAX_ENTRIES + 1):
        other = tmp_path / f"Other{index}.zhl"
        other_text = (
            f"fn value{index}(x:u8) -> u8 {{ x }}\n"
            f"module Other{index} {{ in a:u8 out y:u8 "
            f"y=value{index}(a) }}\n"
        )
        other.write_text(other_text, encoding="utf-8")
        other_position = other_text.splitlines()[1].index(f"value{index}")
        assert definition_at(
            other,
            other_text,
            1,
            other_position,
            _session=session,
        ) is not None
    calls_after_eviction = calls
    assert definition_at(source, text, 1, position, _session=session) is not None
    assert calls == calls_after_eviction


def test_persistent_symbol_cache_reuses_saved_project_after_session_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zlang.tooling as tooling

    cache_root = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    monkeypatch.setenv("ZLANG_LSP_SYMBOL_CACHE", "persistent")
    root = _project(tmp_path)
    dependency = root / "src/dep.zhl"
    dependency.write_text("fn inc(x:u8) -> u9 { x + 1 }\n", encoding="utf-8")
    top = root / "src/top.zhl"
    text = "import demo.dep\nmodule Top { in a:u8 out y:u9 y=inc(a) }\n"
    top.write_text(text, encoding="utf-8")
    update_project_lock(root / "zlang.toml")
    original = tooling.check_file_snapshot
    calls = 0

    def observed(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    position = text.splitlines()[1].index("inc")
    cold = definition_at(top, text, 1, position, _session=ToolingSession())
    assert cold is not None
    assert calls == 1

    warm = definition_at(top, text, 1, position, _session=ToolingSession())
    assert warm == cold
    assert calls == 1
    shards = tuple(tooling._symbol_cache_root().rglob("*.json"))
    assert len(shards) == 1
    encoded = shards[0].read_text(encoding="utf-8")
    assert str(tmp_path) not in encoded
    assert text not in encoded
    assert dependency.read_text(encoding="utf-8") not in encoded


def test_persistent_symbol_cache_uses_content_not_timestamps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import zlang.tooling as tooling

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("ZLANG_LSP_SYMBOL_CACHE", "persistent")
    root = _project(tmp_path)
    dependency = root / "src/dep.zhl"
    dependency.write_text("fn inc(x:u8) -> u9 { x + 1 }\n", encoding="utf-8")
    top = root / "src/top.zhl"
    unrelated = root / "src/unrelated.zhl"
    unrelated.write_text("module Unrelated { out x:u8 x=1 }\n", encoding="utf-8")
    text = "import demo.dep\nmodule Top { in a:u8 out y:u9 y=inc(a) }\n"
    top.write_text(text, encoding="utf-8")
    update_project_lock(root / "zlang.toml")
    original = tooling.check_file_snapshot
    calls = 0

    def observed(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    position = text.splitlines()[1].index("inc")
    assert definition_at(top, text, 1, position, _session=ToolingSession())
    os.utime(top, None)
    unrelated.write_text(
        "module Unrelated { out x:u8 x=2 }\n", encoding="utf-8"
    )
    assert definition_at(top, text, 1, position, _session=ToolingSession())
    assert calls == 1

    dependency.write_text("fn inc(x:u8) -> u9 { x + 2 }\n", encoding="utf-8")
    assert definition_at(top, text, 1, position, _session=ToolingSession())
    assert calls == 2


def test_unsaved_and_corrupt_symbol_cache_fail_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zlang.tooling as tooling

    cache_root = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    monkeypatch.setenv("ZLANG_LSP_SYMBOL_CACHE", "persistent")
    standalone = tmp_path / "Standalone.zhl"
    saved = (
        "fn inc(x:u8) -> u9 { x + 1 }\n"
        "module Top { in a:u8 out y:u9 y=inc(a) }\n"
    )
    unsaved = saved.replace("x + 1", "x + 0")
    standalone.write_text(saved, encoding="utf-8")
    position = unsaved.splitlines()[1].index("inc")
    assert definition_at(
        standalone, unsaved, 1, position, _session=ToolingSession()
    )
    assert not tuple((cache_root / "zlang-hdl/lsp").rglob("*.json"))

    root = _project(tmp_path)
    top = root / "src/top.zhl"
    top.write_text(saved, encoding="utf-8")
    update_project_lock(root / "zlang.toml")
    position = saved.splitlines()[1].index("inc")
    assert definition_at(top, saved, 1, position, _session=ToolingSession())
    shard = next(iter((cache_root / "zlang-hdl/lsp").rglob("*.json")))
    shard.write_text("{broken", encoding="utf-8")
    original = tooling.check_file_snapshot
    calls = 0

    def observed(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    assert definition_at(top, saved, 1, position, _session=ToolingSession())
    assert calls == 1


def test_symbol_cache_modes_and_recipe_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zlang.tooling as tooling

    cache_root = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    monkeypatch.setenv("ZLANG_LSP_SYMBOL_CACHE", "off")
    monkeypatch.setattr(ToolingSession, "_MAX_ENTRIES", 0)
    root = _project(tmp_path)
    top = root / "src/top.zhl"
    text = "module Top { in a:u8 out y:u8 y=a }\n"
    top.write_text(text, encoding="utf-8")
    update_project_lock(root / "zlang.toml")
    original = tooling.check_file_snapshot
    calls = 0

    def observed(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    position = text.index("a", text.index("y=a"))
    session = ToolingSession()
    assert definition_at(top, text, 0, position, _session=session)
    assert definition_at(top, text, 0, position, _session=session)
    assert calls == 2
    assert not tuple((cache_root / "zlang-hdl/lsp").rglob("*.json"))

    monkeypatch.setenv("ZLANG_LSP_SYMBOL_CACHE", "persistent")
    first = ToolingSession()
    assert definition_at(top, text, 0, position, _session=first)
    assert calls == 3
    assert definition_at(top, text, 0, position, _session=ToolingSession())
    assert calls == 3

    monkeypatch.setattr(
        tooling, "SYMBOL_CACHE_SCHEMA", tooling.SYMBOL_CACHE_SCHEMA + 1
    )
    assert definition_at(top, text, 0, position, _session=ToolingSession())
    assert calls == 4


def test_symbol_cache_manifest_lock_and_symlink_invalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zlang.tooling as tooling

    cache_root = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    monkeypatch.setenv("ZLANG_LSP_SYMBOL_CACHE", "persistent")
    root = _project(tmp_path)
    top = root / "src/top.zhl"
    text = "module Top { in a:u8 out y:u8 y=a }\n"
    top.write_text(text, encoding="utf-8")
    update_project_lock(root / "zlang.toml")
    original = tooling.check_file_snapshot
    calls = 0

    def observed(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    position = text.index("a", text.index("y=a"))
    assert definition_at(top, text, 0, position, _session=ToolingSession())
    assert calls == 1

    manifest = root / "zlang.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace('version="1"', 'version="2"'),
        encoding="utf-8",
    )
    update_project_lock(manifest)
    assert definition_at(top, text, 0, position, _session=ToolingSession())
    assert calls == 2

    lock = root / "zlang.lock"
    lock.write_text(lock.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert definition_at(top, text, 0, position, _session=ToolingSession())
    assert calls == 3

    shards = tuple(cache_root.rglob("*.json"))
    assert shards
    shard = max(shards, key=lambda item: item.stat().st_mtime_ns)
    external = tmp_path / "outside.json"
    external.write_text("do not touch", encoding="utf-8")
    shard.unlink()
    shard.symlink_to(external)
    assert definition_at(top, text, 0, position, _session=ToolingSession())
    assert calls == 4
    assert external.read_text(encoding="utf-8") == "do not touch"
    assert shard.is_file() and not shard.is_symlink()


def test_symbol_cache_gc_uses_age_only_for_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import time
    import zlang.tooling as tooling

    root = tmp_path / "symbol-v1"
    namespace = root / "namespace"
    namespace.mkdir(parents=True)
    now = time.time()
    old = namespace / "old.json"
    old.write_text("{}", encoding="utf-8")
    os.utime(old, (now - 31 * 24 * 60 * 60,) * 2)
    recent = []
    for index in range(3):
        item = namespace / f"recent-{index}.json"
        item.write_text("{}", encoding="utf-8")
        os.utime(item, (now - 30 + index,) * 2)
        recent.append(item)
    monkeypatch.setattr(tooling, "_SYMBOL_DISK_MAX_ENTRIES", 2)
    tooling._garbage_collect_symbol_cache(root)
    assert not old.exists()
    assert not recent[0].exists()
    assert recent[1].exists()
    assert recent[2].exists()


def test_symbol_cache_concurrent_publication_is_atomic_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    import json
    import zlang.tooling as tooling

    cache_root = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    monkeypatch.setenv("ZLANG_LSP_SYMBOL_CACHE", "persistent")
    root = _project(tmp_path)
    top = root / "src/top.zhl"
    text = "module Top { in a:u8 out y:u8 y=a }\n"
    top.write_text(text, encoding="utf-8")
    update_project_lock(root / "zlang.toml")
    session = ToolingSession()
    result = session.semantic_snapshot(
        top,
        text,
        analysis_needs=AnalysisNeeds.DEFINITIONS,
    )
    _, payload = tooling._normalized_symbol_payload(top, result, session)
    shard = next(iter(cache_root.rglob("*.json")))
    expected = shard.read_bytes()
    shard.unlink()

    def publish(_: int) -> None:
        tooling._publish_persistent_symbol_snapshot(
            top,
            text,
            payload,
            project=None,
            profile=None,
            top=None,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        tuple(executor.map(publish, range(2)))
    assert shard.read_bytes() == expected
    assert isinstance(json.loads(shard.read_text(encoding="utf-8")), dict)
    assert not tuple(cache_root.rglob("*.tmp"))


def test_tooling_session_honors_explicit_top_selection(tmp_path: Path) -> None:
    source = tmp_path / "multi.zhl"
    text = (
        "module First { out first:u8 first=1 }\n"
        "module Second { out second:u8 second=2 }\n"
    )
    source.write_text(text, encoding="utf-8")
    session = ToolingSession()
    first = session.semantic_snapshot(
        source,
        text,
        top="First",
        analysis_needs=AnalysisNeeds.DEFINITIONS,
    )
    second = session.semantic_snapshot(
        source,
        text,
        top="Second",
        analysis_needs=AnalysisNeeds.DEFINITIONS,
    )
    assert first.ir.name == "First"
    assert second.ir.name == "Second"


def test_references_projection_tracks_port_occurrences_and_declaration_flag(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    text = (
        "module Top {\n"
        "    in a:u8\n"
        "    out y:u9\n"
        "    y = a + 1\n"
        "}\n"
    )
    source.write_text(text)
    position = text.splitlines()[3].index("a")
    usages = references_at(source, text, 3, position)
    with_declaration = references_at(source, text, 3, position, True)
    assert all(isinstance(item, ToolingReference) for item in usages)
    assert len(usages) == 1
    assert usages[0].origin.construct == "name a"
    assert len(with_declaration) == 2
    assert with_declaration[0].origin.construct == "port a"
    assert with_declaration[1].origin.construct == "name a"
    assert TOOLING_REFERENCE_SCHEMA == 1


def test_references_projection_uses_callable_identity_and_deterministic_order(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    text = (
        "fn add(x:u8) -> u9 { x + 1 }\n"
        "module Top { in a:u8 out y:u10 y=add(a)+add(a) }\n"
    )
    source.write_text(text)
    position = text.splitlines()[1].index("add")
    usages = references_at(source, text, 1, position)
    all_locations = references_at(source, text, 1, position, True)
    assert [item.origin.construct for item in usages] == ["call add", "call add"]
    assert len(all_locations) == 3
    assert all_locations[0].origin.construct == "function add"
    assert [item.origin.start_column for item in all_locations[1:]] == [34, 41]


def test_references_projection_supports_generic_function_calls(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    text = (
        "fn identity<type T>(x:T) { x }\n"
        "module Top { in a:u8 out y:u8 y=identity<T=u8>(a) }\n"
    )
    source.write_text(text)
    position = text.splitlines()[1].index("identity")
    locations = references_at(source, text, 1, position, True)
    assert len(locations) == 2
    assert locations[0].origin.construct == "function identity"
    assert locations[1].origin.construct == "call identity"


def test_references_projection_separates_same_spelling_parameter_scopes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    text = (
        "fn first(x:u8) -> u9 { x + 1 }\n"
        "fn second(x:u8) -> u9 { x + 2 }\n"
        "module Top { out y:u9 y=first(1) }\n"
    )
    source.write_text(text)
    first_body = text.splitlines()[0].index("x", 10)
    second_body = text.splitlines()[1].index("x", 11)
    first = references_at(source, text, 0, first_body, True)
    second = references_at(source, text, 1, second_body, True)
    assert len(first) == 2 and len(second) == 2
    assert {item.origin.start_line for item in first} == {1}
    assert {item.origin.start_line for item in second} == {2}


def test_references_projection_separates_parameters_in_one_callable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    text = (
        "fn add(x:u8, y:u8) -> u9 { x + y }\n"
        "module Top { out result:u9 result=add(1, 2) }\n"
    )
    source.write_text(text)
    x_position = text.splitlines()[0].index("x", text.splitlines()[0].index("{"))
    y_position = text.splitlines()[0].index("y", text.splitlines()[0].index("{"))
    x_locations = references_at(source, text, 0, x_position, True)
    y_locations = references_at(source, text, 0, y_position, True)
    assert len(x_locations) == 2 and len(y_locations) == 2
    assert x_locations[0].origin.construct == "parameter x"
    assert y_locations[0].origin.construct == "parameter y"
    assert x_locations[1].origin.construct == "name x"
    assert y_locations[1].origin.construct == "name y"


def test_references_projection_maps_imported_calls_and_safe_empty_cases(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    dependency = root / "src/dep.zhl"
    dependency.write_text("fn inc(x:u8) -> u9 { x + 1 }\n")
    top = root / "src/top.zhl"
    text = (
        "import demo.dep\n"
        "module Top { in a:u8 out y:u10 y=inc(a)+inc(a) }\n"
    )
    top.write_text(text)
    update_project_lock(root / "zlang.toml")
    position = text.splitlines()[1].index("inc")
    usages = references_at(top, text, 1, position)
    all_locations = references_at(top, text, 1, position, True)
    assert len(usages) == 2
    assert {item.source_path for item in usages} == {top.resolve()}
    assert len(all_locations) == 3
    assert all_locations[0].source_path == dependency.resolve()
    assert references_at(top, text, 0, 0) == ()
    malformed = tmp_path / "malformed.zhl"
    malformed_text = "module Top {"
    malformed.write_text(malformed_text)
    assert references_at(malformed, malformed_text, 0, 0) == ()


def test_references_projection_uses_register_declaration_identity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "State.zhl"
    text = (
        "module State { clock clk reset rst reg state:u8=0 "
        "out y:u8 y=state }\n"
    )
    source.write_text(text)
    position = text.index("state", 35)
    usages = references_at(source, text, 0, position)
    assert len(usages) == 1
    assert usages[0].origin.construct == "name state"
    with_declaration = references_at(source, text, 0, position, True)
    assert [item.origin.construct for item in with_declaration] == [
        "register state",
        "name state",
    ]


def test_references_projection_resolves_concise_locals_and_dotted_port_targets(
    tmp_path: Path,
) -> None:
    source = (tmp_path / "stream_locals.zhl").resolve()
    text = (
        "module StreamLocals {\n"
        "  clock clk reset rst\n"
        "  in symbol_enable : rv<bit>\n"
        "  out preamble_out : rv<bits<24>>\n"
        "  preamble_out_data : bit = symbol_enable.payload\n"
        "  preamble_beat : bits<24> = concat(preamble_out_data, zeros<23>)\n"
        "  preamble_out.payload = preamble_beat\n"
        "  preamble_out.valid = symbol_enable.valid\n"
        "  symbol_enable.ready = preamble_out.valid & preamble_out.ready\n"
        "}\n"
    )
    source.write_text(text, encoding="utf-8")
    lines = text.splitlines()
    session = ToolingSession()

    local_use_line = next(
        index for index, item in enumerate(lines)
        if "concat(preamble_out_data" in item
    )
    local_position = (
        lines[local_use_line].index("preamble_out_data")
        + len("preamble_out_data")
    )
    local_references = references_at(
        source,
        text,
        local_use_line,
        local_position,
        True,
        _session=session,
    )
    assert [item.origin.construct for item in local_references] == [
        "value preamble_out_data",
        "name preamble_out_data",
    ]

    port_use_line = next(
        index for index, item in enumerate(lines)
        if "symbol_enable.ready" in item
    )
    port_position = (
        lines[port_use_line].index("symbol_enable") + len("symbol_enable")
    )
    port_references = references_at(
        source,
        text,
        port_use_line,
        port_position,
        True,
        _session=session,
    )
    assert [item.origin.start_line for item in port_references] == [3, 5, 8, 9]
    assert port_references[0].origin.construct == "port symbol_enable"
    assert all(
        item.origin.construct == "name symbol_enable"
        for item in port_references[1:]
    )


def test_references_projection_collects_exact_project_type_occurrences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zlang.tooling as tooling

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("ZLANG_LSP_SYMBOL_CACHE", "persistent")
    source = Path(
        "examples/projects/80211a_transmitter/src/data_types.zhl"
    ).resolve()
    text = source.read_text(encoding="utf-8")
    line = next(
        index for index, item in enumerate(text.splitlines())
        if "struct WifiSampleMeta" in item
    )
    position = (
        text.splitlines()[line].index("WifiSampleMeta") + len("WifiSampleMeta")
    )
    original = tooling.check_file_snapshot
    calls = 0
    compiled_roots: list[tuple[str, object]] = []

    def observed(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        compiled_roots.append((str(args[0]), kwargs.get("top")))
        return original(*args, **kwargs)

    monkeypatch.setattr(tooling, "check_file_snapshot", observed)
    references = references_at(
        source,
        text,
        line,
        position,
        True,
        _session=ToolingSession(),
    )
    assert [
        (item.source_path.name, item.origin.start_line, item.origin.start_column)
        for item in references
    ] == [
        ("data_types.zhl", 27, 8),
        ("ifft.zhl", 47, 16),
        ("ifft.zhl", 136, 16),
        ("mapper.zhl", 105, 53),
        ("mapper.zhl", 298, 16),
        ("mapper.zhl", 345, 20),
    ]
    cold_calls = calls
    assert cold_calls > 0
    assert references_at(
        source,
        text,
        line,
        position,
        True,
        _session=ToolingSession(),
    ) == references
    # The cold project lookup publishes every contributing root as a symbol
    # shard.  A fresh LSP session must reuse those shards instead of compiling
    # the three contributing tops again.
    assert compiled_roots[cold_calls:] == []


def test_references_projection_includes_unused_module_declaration(
    tmp_path: Path,
) -> None:
    source = (tmp_path / "unused.zhl").resolve()
    text = "module UnusedModule { out y:u8 y=1 }\n"
    source.write_text(text, encoding="utf-8")
    line = next(
        index for index, item in enumerate(text.splitlines())
        if "module UnusedModule" in item
    )
    position = (
        text.splitlines()[line].index("UnusedModule")
        + len("UnusedModule")
    )
    session = ToolingSession()
    assert references_at(
        source, text, line, position, False, _session=session
    ) == ()
    with_declaration = references_at(
        source, text, line, position, True, _session=session
    )
    assert len(with_declaration) == 1
    assert with_declaration[0].origin.construct == "module UnusedModule"


def test_references_projection_collects_cross_file_module_instances(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    dependency = root / "src/dep.zhl"
    dependency.write_text("module Leaf { out y:u8 y=1 }\n", encoding="utf-8")
    top = root / "src/top.zhl"
    top.write_text(
        "import demo.dep\nmodule Top { child : Leaf out y:u8 y = child.y }\n",
        encoding="utf-8",
    )
    update_project_lock(root / "zlang.toml")
    text = dependency.read_text(encoding="utf-8")
    position = text.index("Leaf") + len("Leaf")
    references = references_at(
        dependency,
        text,
        0,
        position,
        True,
        _session=ToolingSession(),
    )
    assert [
        (item.source_path.name, item.origin.construct) for item in references
    ] == [
        ("dep.zhl", "module Leaf"),
        ("top.zhl", "module Leaf"),
    ]


def test_project_reference_root_bound_fails_before_partial_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zlang.tooling as tooling

    manifest = tmp_path / "zlang.toml"
    location = tooling.ProjectLocation(manifest, tmp_path, tmp_path)
    roots = tuple(
        tooling.WorkspaceModule(
            f"demo.root_{index}", tmp_path / f"root_{index}.zhl",
            ("demo.dep",), True,
        )
        for index in range(tooling._REFERENCE_MAX_PROJECT_ROOTS + 1)
    )
    index = tooling.WorkspaceIndex(manifest, tmp_path, roots, (
        tooling.WorkspaceModule("demo.dep", tmp_path / "dep.zhl", (), False),
    ))
    monkeypatch.setattr(tooling, "discover_project", lambda _source: location)
    monkeypatch.setattr(tooling, "workspace_index", lambda _manifest: index)
    with pytest.raises(ToolingError, match="root candidate limit exceeded"):
        tooling._project_reference_compilations(
            tmp_path / "dep.zhl", "", SimpleNamespace(source_unit="demo.dep"),
            "Leaf",
        )


def test_project_reference_source_change_fails_instead_of_publishing_stale_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zlang.tooling as tooling

    root = _project(tmp_path)
    dependency = root / "src/dep.zhl"
    dependency.write_text("module Leaf { out y:u8 y=1 }\n", encoding="utf-8")
    top = root / "src/top.zhl"
    top.write_text(
        "import demo.dep\nmodule Top { child : Leaf out y:u8 y=child.y }\n",
        encoding="utf-8",
    )
    update_project_lock(root / "zlang.toml")
    original = tooling._definition_snapshot

    def changed_after_analysis(source: Path, text: str, **kwargs: object) -> object:
        result = original(source, text, **kwargs)
        if Path(source).resolve() == top.resolve() and "child : Leaf" in text:
            top.write_text(text + "// changed after prefilter\n", encoding="utf-8")
        return result

    monkeypatch.setattr(tooling, "_definition_snapshot", changed_after_analysis)
    text = dependency.read_text(encoding="utf-8")
    with pytest.raises(ToolingError, match="changed during lookup"):
        references_at(
            dependency, text, 0, text.index("Leaf"), True,
            _session=ToolingSession(),
        )


def test_rename_projection_returns_exact_semantic_edits_and_preserves_text(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Top.zhl"
    text = (
        "module Top { in a:u8 out y:u9 y=a+1 }\n"
    )
    source.write_text(text)
    position = text.rfind("a")
    edits = rename_at(source, text, 0, position, "input_value")
    assert edits is not None
    assert [item.origin.construct for item in edits] == ["port a", "name a"]
    assert [(item.origin.start_column, item.origin.end_column) for item in edits] == [
        (17, 18), (33, 34)
    ]
    assert all(item.new_text == "input_value" for item in edits)
    assert TOOLING_RENAME_SCHEMA == 1


def test_rename_projection_supports_functions_parameters_and_shadowing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Functions.zhl"
    text = (
        "fn first(x:u8) -> u9 { x + 1 }\n"
        "fn second(x:u8) -> u9 { x + 2 }\n"
        "module Top { out y:u9 y=first(1) }\n"
    )
    source.write_text(text)
    first_x = text.splitlines()[0].index("x", text.splitlines()[0].index("{"))
    edits = rename_at(source, text, 0, first_x, "sample")
    assert edits is not None
    assert [item.origin.construct for item in edits] == [
        "parameter x", "name x"
    ]
    assert all(item.origin.start_line == 1 for item in edits)
    function_name = text.splitlines()[2].index("first")
    function_edits = rename_at(source, text, 2, function_name, "first_stage")
    assert function_edits is not None
    assert [item.origin.construct for item in function_edits] == [
        "function first", "call first"
    ]
    middle_name = rename_at(source, text, 2, function_name + 2, "first_stage")
    assert middle_name == function_edits


def test_rename_projection_rejects_invalid_names_and_collisions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Collision.zhl"
    text = (
        "fn add(a:u8, b:u8) -> u9 { a + b }\n"
        "module Top { out y:u9 y=add(1, 2) }\n"
    )
    source.write_text(text)
    position = text.splitlines()[0].index("a", text.splitlines()[0].index("{"))
    with pytest.raises(ToolingRenameError, match="valid ZLang identifier"):
        rename_at(source, text, 0, position, "if")
    with pytest.raises(ToolingRenameError, match="valid ZLang identifier"):
        rename_at(source, text, 0, position, "bad-name")
    with pytest.raises(ToolingRenameError, match="change or invalidate"):
        rename_at(source, text, 0, position, "b")


def test_rename_projection_fails_closed_for_unsupported_registers(
    tmp_path: Path,
) -> None:
    source = tmp_path / "State.zhl"
    text = (
        "module State { clock clk reset rst reg state:u8=0 "
        "out y:u8 y=state }\n"
    )
    source.write_text(text)
    position = text.rfind("state")
    assert rename_at(source, text, 0, position, "next_state") is None


def test_rename_projection_rejects_cross_file_target_without_overlay(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    dependency = root / "src/dep.zhl"
    dependency.write_text("fn inc(x:u8) -> u9 { x + 1 }\n")
    source = root / "src/top.zhl"
    text = "import demo.dep\nmodule Top { out y:u9 y=inc(1) }\n"
    source.write_text(text)
    update_project_lock(root / "zlang.toml")
    position = text.splitlines()[1].index("inc")
    with pytest.raises(ToolingRenameError, match="cross-file rename"):
        rename_at(source, text, 1, position, "increment")


def test_completion_projection_uses_compiler_scope_and_is_deterministic(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Completion.zhl"
    text = (
        "fn add(x:u8, y:u8) -> u9 { x+y }\n"
        "fn identity<type T>(value:T) { value }\n"
        "module Top { in a:u8 out z:u9 z=add(a,a) }\n"
    )
    source.write_text(text)
    body_position = text.splitlines()[0].index("x", text.splitlines()[0].index("{"))
    candidates = completion_at(source, text, 0, body_position)
    assert all(isinstance(item, ToolingCompletion) for item in candidates)
    assert [item.name for item in candidates] == ["add", "identity", "x", "y"]
    assert candidates[0].detail == "fn add(x : u8, y : u8) -> u9"
    assert [(item.name, item.kind) for item in candidates[-2:]] == [
        ("x", "parameter"), ("y", "parameter")
    ]
    module_position = text.splitlines()[2].index("a", text.splitlines()[2].index("z="))
    assert [item.name for item in completion_at(source, text, 2, module_position)] == [
        "a", "add", "identity"
    ]
    assert completion_at(source, "module Top {", 0, 7) == ()
    assert completion_at(source, text, 20, 0) == ()
    assert TOOLING_COMPLETION_SCHEMA == 1


def test_completion_projection_preserves_scope_shadowing_and_import_visibility(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Scopes.zhl"
    text = (
        "fn first(x:u8) -> u9 { x+1 }\n"
        "fn second(x:u8) -> u9 { x+2 }\n"
        "module Top { out z:u9 z=first(1) }\n"
    )
    source.write_text(text)
    first_position = text.splitlines()[0].index("x", text.splitlines()[0].index("{"))
    second_position = text.splitlines()[1].index("x", text.splitlines()[1].index("{"))
    first = completion_at(source, text, 0, first_position)
    second = completion_at(source, text, 1, second_position)
    assert [item.name for item in first if item.kind == "parameter"] == ["x"]
    assert [item.name for item in second if item.kind == "parameter"] == ["x"]
    assert [item.name for item in first if item.kind == "function"] == ["first", "second"]

    local_source = tmp_path / "Locals.zhl"
    local_text = "fn local(x:u8) -> u9 { t=x+1 t }\nmodule Top { out z:u9 z=local(1) }\n"
    local_source.write_text(local_text)
    initializer_position = local_text.splitlines()[0].index("x", local_text.splitlines()[0].index("="))
    body_position = local_text.splitlines()[0].rindex("t")
    assert "t" not in [
        item.name for item in completion_at(local_source, local_text, 0, initializer_position)
    ]
    assert "t" in [
        item.name for item in completion_at(local_source, local_text, 0, body_position)
    ]

    root = _project(tmp_path / "imported")
    dependency = root / "src/dep.zhl"
    dependency.write_text("fn inc(x:u8) -> u9 { x+1 }\n")
    top = root / "src/top.zhl"
    imported = "import demo.dep\nmodule Top { out z:u9 z=inc(1) }\n"
    top.write_text(imported)
    update_project_lock(root / "zlang.toml")
    position = imported.splitlines()[1].index("inc")
    names = [item.name for item in completion_at(top, imported, 1, position)]
    assert "inc" in names


def test_signature_help_uses_resolved_call_and_argument_spans(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Signature.zhl"
    text = (
        "fn add(a:u8, b:u8, c:u8) -> u10 { a+b+c }\n"
        "module Top { out y:u10 y=add(1, 2, 3) }\n"
    )
    source.write_text(text)
    call_line = text.splitlines()[1]
    for literal, active in (("1", 0), ("2", 1), ("3", 2)):
        position = call_line.index(literal, call_line.index("add("))
        result = signature_help_at(source, text, 1, position)
        assert isinstance(result, ToolingSignatureHelp)
        assert result.label == "fn add(a : u8, b : u8, c : u8) -> u10"
        assert result.parameters == ("a : u8", "b : u8", "c : u8")
        assert result.active_parameter == active
    between = call_line.index(", 3") + 1
    assert signature_help_at(source, text, 1, between).active_parameter == 2  # type: ignore[union-attr]
    assert signature_help_at(source, text, 0, 0) is None
    assert signature_help_at(source, "module Top {", 0, 7) is None
    assert signature_help_at(source, text, 20, 0) is None
    assert TOOLING_SIGNATURE_HELP_SCHEMA == 1


def test_signature_help_supports_generic_nested_and_imported_calls(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Nested.zhl"
    text = (
        "fn add(a:u8, b:u8, c:u8) -> u10 { a+b+c }\n"
        "fn identity<type T>(value:T) { value }\n"
        "fn outer(value:u10, tail:u8) -> u10 { value }\n"
        "module Top { out y:u10 y=outer(add(1,2,3),4) }\n"
    )
    source.write_text(text)
    line = text.splitlines()[3]
    inner = line.index("2")
    nested = signature_help_at(source, text, 3, inner)
    assert nested is not None
    assert nested.label == "fn add(a : u8, b : u8, c : u8) -> u10"
    generic_text = (
        "fn identity<type T>(value:T) { value }\n"
        "module Top { out y:u8 y=identity<u8>(1) }\n"
    )
    generic_source = tmp_path / "GenericSignature.zhl"
    generic_source.write_text(generic_text)
    generic = signature_help_at(
        generic_source,
        generic_text,
        1,
        generic_text.splitlines()[1].index("1"),
    )
    assert generic is not None
    assert generic.label == "fn identity(value : u8) -> u8"

    root = _project(tmp_path / "imported-signature")
    dependency = root / "src/dep.zhl"
    dependency.write_text("fn inc(a:u8, b:u8, c:u8) -> u10 { a+b+c }\n")
    top = root / "src/top.zhl"
    imported = "import demo.dep\nmodule Top { out y:u10 y=inc(1,2,3) }\n"
    top.write_text(imported)
    update_project_lock(root / "zlang.toml")
    imported_result = signature_help_at(
        top,
        imported,
        1,
        imported.splitlines()[1].index("2"),
    )
    assert imported_result is not None
    assert imported_result.label == "fn inc(a : u8, b : u8, c : u8) -> u10"
    assert imported_result.active_parameter == 1


def test_semantic_tokens_project_exact_declarations_and_resolved_uses(
    tmp_path: Path,
) -> None:
    source = tmp_path / "SemanticTokens.zhl"
    text = (
        "fn add(x:u8, y:u8) -> u9 { t=x+y t }\n"
        "module Top { in a:u8 out z:u9 z=add(a,a) }\n"
    )
    source.write_text(text)
    tokens = semantic_tokens(source, text)
    assert all(isinstance(item, ToolingSemanticToken) for item in tokens)
    assert [
        (item.origin.construct, item.kind, item.modifiers)
        for item in tokens
    ] == [
        ("function add", "function", ("declaration",)),
        ("parameter x", "parameter", ("declaration",)),
        ("parameter y", "parameter", ("declaration",)),
        ("value t", "variable", ("declaration",)),
        ("name x", "parameter", ()),
        ("name y", "parameter", ()),
        ("name t", "variable", ()),
        ("port a", "property", ("declaration",)),
        ("port z", "property", ("declaration",)),
        ("call add", "function", ()),
        ("name a", "property", ()),
        ("name a", "property", ()),
    ]
    for token in tokens:
        assert token.origin.start_line == token.origin.end_line
        construct_name = token.origin.construct.rsplit(" ", 1)[-1]
        assert token.origin.end_column - token.origin.start_column == len(
            construct_name
        )
    assert TOOLING_SEMANTIC_TOKEN_SCHEMA == 1


def test_semantic_tokens_preserve_shadowed_occurrences_and_root_identity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Scopes.zhl"
    text = (
        "fn first(x:u8) -> u9 { x+1 }\n"
        "fn second(x:u8) -> u9 { x+2 }\n"
        "module Top { out y:u9 y=first(1) }\n"
    )
    source.write_text(text)
    tokens = semantic_tokens(source, text)
    x_tokens = [
        item
        for item in tokens
        if item.origin.construct in {"parameter x", "name x"}
    ]
    assert [(item.origin.start_line, item.modifiers) for item in x_tokens] == [
        (1, ("declaration",)),
        (1, ()),
        (2, ("declaration",)),
        (2, ()),
    ]

    generic_source = tmp_path / "GenericTokens.zhl"
    generic_text = (
        "fn identity<type T>(value:T) { value }\n"
        "module Top { out y:u8 y=identity<u8>(1) }\n"
    )
    generic_source.write_text(generic_text)
    assert [
        (item.origin.construct, item.kind, item.modifiers)
        for item in semantic_tokens(generic_source, generic_text)
        if item.kind == "function"
    ] == [
        ("function identity", "function", ("declaration",)),
        ("call identity", "function", ()),
    ]

    root = _project(tmp_path / "imported-semantic-tokens")
    dependency = root / "src/dep.zhl"
    dependency.write_text("fn inc(x:u8) -> u9 { x+1 }\n")
    top = root / "src/top.zhl"
    imported = "import demo.dep\nmodule Top { out y:u9 y=inc(1) }\n"
    top.write_text(imported)
    update_project_lock(root / "zlang.toml")
    imported_tokens = semantic_tokens(top, imported)
    assert [
        item.origin.construct
        for item in imported_tokens
        if item.kind == "function"
    ] == ["call inc"]
    assert all(item.origin.source_unit == "demo.top" for item in imported_tokens)


def test_semantic_tokens_are_immutable_and_malformed_source_is_empty(
    tmp_path: Path,
) -> None:
    from dataclasses import FrozenInstanceError

    source = tmp_path / "Top.zhl"
    text = "module Top { out y:u8 y=1 }\n"
    source.write_text(text)
    token = semantic_tokens(source, text)[0]
    with pytest.raises(FrozenInstanceError):
        token.kind = "variable"  # type: ignore[misc]
    assert semantic_tokens(source, "module Top {") == ()
