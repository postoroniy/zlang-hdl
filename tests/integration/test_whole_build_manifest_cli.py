"""End-to-end publication tests for evidence and whole-build manifests.

These tests deliberately exercise the public CLI boundary while keeping the
suite independent of optional external tools.  Real Verilator and
formal execution remain separate acceptance tests; generating a harness here
must therefore be recorded as ``not_run``, never as proof evidence.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
from pathlib import Path

import pytest

from zlang.build_manifest import (
    BuildManifestError,
    PublishedFile,
    WholeBuildManifest,
    validate_manifest_file_map,
)
from zlang.backend.companions import (
    collect_rom_companions,
    publish_companion_bundle,
)
import zlang.cli as cli_module
from zlang.cli import main
from zlang.compiler import compile_file, compile_file_snapshot
from zlang.workspace import WorkspaceError, update_project_lock


TIMED_NAMED_SOURCE = """
interface TimedIfc {
    clock clk
    reset rst
    in x : u8 @clk
    out y : u8 @clk
    timing { latency 0 ii 1 }
}

module TimedTop : TimedIfc {
    clock clk
    reset rst
    in x : u8 @clk
    out y : u8 @clk
    y = x
    timing { latency 0 ii 1 }
    guarantee output_is_self_equal @ clk disable iff rst { y == y }
}
"""


ENUM_SOURCE = """
enum Phase { Idle Active Done }
module EnumControl {
    clock clk
    reset rst
    in start : bit
    in finish : bit
    out phase : Phase
    reg state : Phase = Phase.Idle
    when 1 {
        state <- switch state {
            Phase.Idle => start ? Phase.Active : Phase.Idle
            Phase.Active => finish ? Phase.Done : Phase.Active
            Phase.Done => start ? Phase.Active : Phase.Idle
        }
    }
    phase = state
}
"""


PACKING_SOURCE = """
struct Pair { hi : u4 lo : u4 }
module PacketPacking {
    in raw : bits<8>
    in tag : u4
    out y : bits<12>
    pair : Pair = unpack<Pair>(raw)
    y = concat(pack(pair), tag[3:0])
}
"""


ROM_SOURCE = """
module InitializedRom {
    clock clk
    reset rst
    in address : u2
    out y : u8
    rom table : rom<u8,4> {
        read_latency 1
        init generate(i in 0..4) i
    }
    table.read_address = address
    y = table.read_data
}
"""


def _invoke(source: Path, arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        status = main([str(source), *arguments])
    return status, stdout.getvalue(), stderr.getvalue()


def _manifest_files(manifest: WholeBuildManifest) -> tuple[PublishedFile, ...]:
    values: list[PublishedFile] = [
        manifest.root_source,
        *manifest.dependency_closure,
    ]
    for backend in manifest.backend_builds:
        values.extend(backend.files)
        values.extend(backend.companions)
    return tuple(values)


def _physical_file_map(
    manifest: WholeBuildManifest,
    physical_files: tuple[Path, ...],
) -> dict[str, Path]:
    """Join logical manifest products to physical files by exact contents.

    The helper intentionally does not rely on a checkout/output directory
    layout: host paths are not part of a relocatable whole-build identity.
    """

    candidates = tuple(path for path in physical_files if path.is_file())
    result: dict[str, Path] = {}
    records: dict[str, tuple[str, int | None]] = {
        item.logical_path: (item.content_hash, item.size)
        for item in _manifest_files(manifest)
    }
    for report in manifest.reports:
        if report.logical_path is not None:
            records.setdefault(report.logical_path, (report.content_hash, None))
    for logical_path, (digest, size) in records.items():
        matches = [
            path for path in candidates
            if hashlib.sha256(path.read_bytes()).hexdigest() == digest
            and (size is None or path.stat().st_size == size)
        ]
        if len(matches) > 1:
            named = [path for path in matches if path.name == Path(logical_path).name]
            if len(named) == 1:
                matches = named
        assert len(matches) == 1, (
            f"could not uniquely resolve {logical_path!r}: "
            f"{[str(path) for path in matches]}"
        )
        result[logical_path] = matches[0]
    return result


def _write_source(root: Path, name: str, source: str) -> Path:
    path = root / "src" / f"{name}.zhl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _assert_cli_usage_error(source: Path, arguments: list[str], message: str) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        with pytest.raises(SystemExit) as raised:
            main([str(source), *arguments])
    assert raised.value.code == 2
    assert stdout.getvalue() == ""
    assert message in stderr.getvalue()


def _publish_timed_build(root: Path) -> tuple[WholeBuildManifest, dict[str, Path]]:
    source = _write_source(root, "timed", TIMED_NAMED_SOURCE)
    direct = root / "build" / "systemverilog" / "TimedTop.sv"
    evidence = root / "reports" / "evidence.json"
    harness = root / "formal" / "TimedTop.formal.sv"
    sby = root / "formal" / "TimedTop.sby"
    manifest_path = root / "build.json"
    status, stdout, stderr = _invoke(
        source,
        [
            "--systemverilog", str(direct),
            "--evidence-report", str(evidence),
            "--evidence-format", "json",
            "--formal-harness", str(harness),
            "--formal-sby", str(sby),
            "--formal-depth", "7",
            "--build-manifest", str(manifest_path),
        ],
    )
    assert status == 0, stderr
    assert stdout == ""
    assert stderr == ""
    manifest = WholeBuildManifest.from_json(manifest_path.read_text(encoding="utf-8"))
    physical = _physical_file_map(
        manifest,
        tuple(path for path in root.rglob("*") if path != manifest_path),
    )
    return manifest, physical


def test_explicit_backend_sink_cannot_alias_the_root_source(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, "packing", PACKING_SOURCE)
    original = source.read_bytes()

    _assert_cli_usage_error(
        source,
        ["--systemverilog", str(source)],
        "collides with the source file",
    )
    assert source.read_bytes() == original




def test_two_report_sinks_must_be_distinct_before_any_write(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, "packing", PACKING_SOURCE)
    report = tmp_path / "reports" / "shared.json"

    _assert_cli_usage_error(
        source,
        ["--high-level-ir", str(report), "--optimization-ir", str(report)],
        "explicit sinks --high-level-ir and --optimization-ir resolve to the same path",
    )
    assert not report.exists()
    assert not report.parent.exists()


def test_identical_direct_systemverilog_compatibility_aliases_share_one_sink(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, "packing", PACKING_SOURCE)
    output = tmp_path / "published" / "PacketPacking.sv"

    status, stdout, stderr = _invoke(
        source,
        [
            "--systemverilog", str(output),
            "--experimental-systemverilog", str(output),
        ],
    )
    assert status == 0, stderr
    assert stdout == ""
    assert stderr == ""
    assert output.read_text(encoding="utf-8").startswith("`default_nettype none")




def test_file_sink_cannot_be_inside_an_owned_cache_directory(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, "packing", PACKING_SOURCE)
    cache = tmp_path / "formal-cache"
    output = cache / "PacketPacking.sv"

    _assert_cli_usage_error(
        source,
        [
            "--systemverilog", str(output),
            "--formal-cache", str(cache),
        ],
        "explicit sink --systemverilog collides with explicit output directory "
        "--formal-cache",
    )
    assert not cache.exists()




def test_deterministic_companion_cannot_alias_an_explicit_file_sink(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, "rom", ROM_SOURCE)
    compilation = compile_file(source)
    companion = collect_rom_companions(compilation.ir)[0]
    collision = tmp_path / "published" / companion.logical_path

    _assert_cli_usage_error(
        source,
        ["--systemverilog", str(collision)],
        "deterministic direct-SystemVerilog companion collides with explicit "
        "sink --systemverilog",
    )
    assert not collision.parent.exists()




def test_deterministic_companion_cannot_resolve_to_a_compilation_input(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, "rom", ROM_SOURCE)
    original = source.read_bytes()
    compilation = compile_file(source)
    companion = collect_rom_companions(compilation.ir)[0]
    output_directory = tmp_path / "published"
    output_directory.mkdir()
    companion_alias = output_directory / companion.logical_path
    companion_alias.symlink_to(source)
    direct = output_directory / "InitializedRom.sv"

    _assert_cli_usage_error(
        source,
        ["--systemverilog", str(direct)],
        "deterministic direct-SystemVerilog companion collides with compilation input",
    )
    assert source.read_bytes() == original
    assert companion_alias.is_symlink()
    assert not direct.exists()


def test_build_manifest_preflight_rejects_resolved_source_alias(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, "packing", PACKING_SOURCE)
    original = source.read_bytes()
    manifest_alias = tmp_path / "manifest-link.json"
    manifest_alias.symlink_to(source)
    rtl = tmp_path / "PacketPacking.sv"

    _assert_cli_usage_error(
        source,
        [
            "--systemverilog", str(rtl),
            "--build-manifest", str(manifest_alias),
        ],
        "--build-manifest output collides with the source file",
    )
    assert source.read_bytes() == original
    assert manifest_alias.is_symlink()
    assert not rtl.exists()


@pytest.mark.parametrize(
    "sink_option",
    (
        "--systemverilog",
        "--evidence-report",
        "--implementation-manifest",
        "--source-map",
    ),
)
def test_build_manifest_preflight_rejects_resolved_explicit_file_sink_alias(
    tmp_path: Path, sink_option: str,
) -> None:
    source = _write_source(tmp_path, "packing", PACKING_SOURCE)
    collision = tmp_path / "nested" / ".." / "collision.json"
    safe_rtl = tmp_path / "safe" / "PacketPacking.sv"
    arguments = [
        "--systemverilog", str(safe_rtl),
        sink_option, str(collision),
        "--build-manifest", str(tmp_path / "collision.json"),
    ]
    # For --systemverilog itself, avoid publishing the same option twice with
    # implementation-dependent argparse precedence.
    if sink_option == "--systemverilog":
        arguments = [
            "--systemverilog", str(collision),
            "--build-manifest", str(tmp_path / "collision.json"),
        ]
    _assert_cli_usage_error(
        source,
        arguments,
        "--build-manifest output collides with explicit sink",
    )
    assert not safe_rtl.exists()
    assert not (tmp_path / "collision.json").exists()


@pytest.mark.parametrize(
    ("directory_option", "extra"),
    (
            ("--formal-cache", ("--systemverilog", "safe/PacketPacking.sv")),
        (
            "--synthesis-cache",
            (
                "--systemverilog", "safe/PacketPacking.sv",
                "--synthesis-report", "safe/synthesis.json",
            ),
        ),
    ),
)
def test_build_manifest_preflight_rejects_owned_output_directory(
    tmp_path: Path, directory_option: str, extra: tuple[str, ...],
) -> None:
    source = _write_source(tmp_path, "packing", PACKING_SOURCE)
    directory = tmp_path / "owned-output"
    manifest = directory / "nested" / "build.json"
    arguments: list[str] = [directory_option, str(directory)]
    for token in extra:
        if token.startswith("--"):
            arguments.append(token)
        else:
            arguments.append(str(tmp_path / token))
    arguments.extend(("--build-manifest", str(manifest)))
    _assert_cli_usage_error(
        source,
        arguments,
        "--build-manifest output is inside explicit output directory",
    )
    assert not directory.exists()




def test_build_manifest_preflight_rejects_deterministic_rom_companion(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, "rom", ROM_SOURCE)
    compilation = compile_file(source)
    companion = collect_rom_companions(compilation.ir)[0]
    rtl = tmp_path / "published" / "InitializedRom.sv"
    manifest = rtl.parent / companion.logical_path
    _assert_cli_usage_error(
        source,
        [
            "--systemverilog", str(rtl),
            "--build-manifest", str(manifest),
        ],
        "--build-manifest output collides with deterministic "
        "direct-SystemVerilog companion",
    )
    assert not rtl.parent.exists()


def test_cli_publishes_direct_backend_truthful_evidence_and_manifest(
    tmp_path: Path,
) -> None:
    manifest, physical = _publish_timed_build(tmp_path)
    compilation = compile_file(tmp_path / "src" / "timed.zhl")

    assert manifest.selected_ir.identity == compilation.selected_ir_identity
    assert manifest.high_level_ir.identity == compilation.high_level_ir_identity
    assert manifest.to_json() == (tmp_path / "build.json").read_text(encoding="utf-8")
    assert len(manifest.backend_builds) == 1
    direct = manifest.backend_builds[0]
    assert direct.backend == "direct_systemverilog"
    assert direct.selected_ir_identity == compilation.selected_ir_identity

    statuses = {item.status for item in manifest.evidence}
    assert {"typed_legal", "timing_validated", "not_run"} <= statuses
    assert "bounded_pass" not in statuses
    assert "proven" not in statuses
    generated_properties = [
        item for item in manifest.evidence
        if item.property_id is not None
    ]
    assert generated_properties
    assert all(item.status == "not_run" for item in generated_properties)
    assert all(item.mode is None and item.depth is None for item in generated_properties)
    timing = next(item for item in manifest.evidence if item.status == "timing_validated")
    assert dict(timing.details)["latency"] == "0"

    report_paths = {
        item.logical_path for item in manifest.reports if item.logical_path is not None
    }
    report_kinds = {item.kind for item in manifest.reports}
    assert any(path.endswith("evidence.json") for path in report_paths)
    assert {"formal_harness", "formal_configuration"} <= report_kinds

    evidence_payload = (tmp_path / "reports" / "evidence.json").read_text(
        encoding="utf-8"
    )
    assert '"status": "typed_legal"' in evidence_payload
    assert '"status": "timing_validated"' in evidence_payload
    assert '"status": "not_run"' in evidence_payload
    assert '"status": "proven"' not in evidence_payload
    validate_manifest_file_map(manifest, physical)


def test_cli_can_publish_the_same_evidence_as_deterministic_text(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, "timed", TIMED_NAMED_SOURCE)
    rtl = tmp_path / "build" / "TimedTop.sv"
    evidence = tmp_path / "reports" / "evidence.txt"
    manifest_path = tmp_path / "build.json"
    status, stdout, stderr = _invoke(
        source,
        [
            "--systemverilog", str(rtl),
            "--evidence-report", str(evidence),
            "--evidence-format", "text",
            "--build-manifest", str(manifest_path),
        ],
    )
    assert status == 0, stderr
    assert stdout == "" and stderr == ""
    rendered = evidence.read_text(encoding="utf-8")
    assert "status=typed_legal" in rendered
    assert "status=timing_validated" in rendered
    assert "status=proven" not in rendered
    manifest = WholeBuildManifest.from_json(manifest_path.read_text())
    report = next(item for item in manifest.reports if item.logical_path is not None)
    assert report.format == "text"
    assert report.content_hash == hashlib.sha256(evidence.read_bytes()).hexdigest()


def test_relocated_repeated_publication_has_the_same_build_identity(
    tmp_path: Path,
) -> None:
    first, _ = _publish_timed_build(tmp_path / "checkout-a")
    second, _ = _publish_timed_build(tmp_path / "different" / "checkout-b")
    assert first.build_identity == second.build_identity
    assert first.identity_data() == second.identity_data()


def test_validation_rejects_tampered_backend_output(tmp_path: Path) -> None:
    manifest, physical = _publish_timed_build(tmp_path)
    root_source = physical[manifest.root_source.logical_path]
    original_source = root_source.read_bytes()
    root_source.write_bytes(original_source + b"\n// changed after compilation\n")
    with pytest.raises(BuildManifestError, match="hash/size mismatch"):
        validate_manifest_file_map(manifest, physical)
    root_source.write_bytes(original_source)

    direct = next(
        item for item in _manifest_files(manifest)
        if item.logical_path.endswith(".sv") and item.kind != "formal_harness"
    )
    physical[direct.logical_path].write_text("module Tampered; endmodule\n")
    with pytest.raises(BuildManifestError, match="hash/size mismatch"):
        validate_manifest_file_map(manifest, physical)


def test_crlf_root_source_is_compiled_and_published_as_exact_bytes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "crlf.zhl"
    source.parent.mkdir(parents=True)
    source_bytes = (
        b"module CrLfTop {\r\n"
        b"    in x : u8\r\n"
        b"    out y : u8\r\n"
        b"    y = x\r\n"
        b"}\r\n"
    )
    source.write_bytes(source_bytes)
    rtl = tmp_path / "build" / "CrLfTop.sv"
    manifest_path = tmp_path / "build" / "CrLfTop.build.json"

    status, stdout, stderr = _invoke(
        source,
        [
            "--systemverilog", str(rtl),
            "--build-manifest", str(manifest_path),
        ],
    )

    assert status == 0, stderr
    assert stdout == "" and stderr == ""
    manifest = WholeBuildManifest.from_json(manifest_path.read_text(encoding="utf-8"))
    assert manifest.root_source.content_hash == hashlib.sha256(source_bytes).hexdigest()
    assert manifest.root_source.size == len(source_bytes)
    validate_manifest_file_map(
        manifest,
        {
            manifest.root_source.logical_path: source,
            manifest.backend_builds[0].files[0].logical_path: rtl,
        },
    )


def test_cli_compiles_captured_snapshot_and_rejects_later_source_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_source(
        tmp_path,
        "snapshot",
        "module SnapshotTop { in x:u8 out y:u8 y=x }",
    )
    replacement = b"module MutatedTop { in x:u8 out y:u8 y=x }\n"
    rtl = tmp_path / "build" / "SnapshotTop.sv"
    manifest_path = tmp_path / "build" / "SnapshotTop.build.json"
    real_compile_snapshot = cli_module.compile_file_snapshot
    compiled_modules: list[str] = []

    def compile_after_mutation(*args, **kwargs):
        # The physical file changes after CLI acquisition but before semantic
        # analysis.  Compilation must still consume the captured text; final
        # publication must reject attributing that IR to the replacement file.
        source.write_bytes(replacement)
        result = real_compile_snapshot(*args, **kwargs)
        compiled_modules.append(result.ir.name)
        return result

    monkeypatch.setattr(cli_module, "compile_file_snapshot", compile_after_mutation)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        with pytest.raises(SystemExit) as raised:
            main([
                str(source),
                "--systemverilog", str(rtl),
                "--build-manifest", str(manifest_path),
            ])

    assert raised.value.code == 2
    assert stdout.getvalue() == ""
    assert "hash/size mismatch" in stderr.getvalue()
    assert compiled_modules == ["SnapshotTop"]
    assert "module SnapshotTop" in rtl.read_text(encoding="utf-8")
    assert not manifest_path.exists()


def test_validation_rejects_tampered_source_map(tmp_path: Path) -> None:
    source = _write_source(tmp_path, "packing", PACKING_SOURCE)
    rtl = tmp_path / "build" / "PacketPacking.sv"
    source_map = tmp_path / "build" / "PacketPacking.source-map.json"
    manifest_path = tmp_path / "build.json"
    status, stdout, stderr = _invoke(
        source,
        [
            "--systemverilog", str(rtl),
            "--source-map", str(source_map),
            "--build-manifest", str(manifest_path),
        ],
    )
    assert status == 0, stderr
    assert stdout == "" and stderr == ""
    manifest = WholeBuildManifest.from_json(manifest_path.read_text())
    backend = manifest.backend_builds[0]
    assert backend.source_map_hash is not None
    source_map_record = next(
        item for item in backend.files
        if item.content_hash == backend.source_map_hash
    )
    physical = _physical_file_map(
        manifest,
        tuple(path for path in tmp_path.rglob("*") if path != manifest_path),
    )
    source_map.write_text("{}\n")
    assert physical[source_map_record.logical_path] == source_map
    with pytest.raises(BuildManifestError, match="hash/size mismatch"):
        validate_manifest_file_map(manifest, physical)


def test_validation_rejects_missing_rom_companion(tmp_path: Path) -> None:
    source = _write_source(tmp_path, "rom", ROM_SOURCE)
    rtl = tmp_path / "build" / "InitializedRom.sv"
    manifest_path = tmp_path / "build.json"
    status, stdout, stderr = _invoke(
        source,
        [
            "--systemverilog", str(rtl),
            "--build-manifest", str(manifest_path),
        ],
    )
    assert status == 0, stderr
    assert stdout == "" and stderr == ""
    manifest = WholeBuildManifest.from_json(manifest_path.read_text())
    companions = manifest.backend_builds[0].companions
    assert companions and all(item.kind == "rom_image" for item in companions)
    physical = _physical_file_map(
        manifest,
        tuple(path for path in tmp_path.rglob("*") if path != manifest_path),
    )
    missing = companions[0]
    physical[missing.logical_path].unlink()
    with pytest.raises(BuildManifestError, match="missing"):
        validate_manifest_file_map(manifest, physical)








@pytest.mark.parametrize(
    ("name", "source"),
    (
        ("EnumControl", ENUM_SOURCE),
        ("PacketPacking", PACKING_SOURCE),
        ("InitializedRom", ROM_SOURCE),
        ("TimedTop", TIMED_NAMED_SOURCE),
    ),
)
def test_representative_language_surfaces_publish_whole_manifests(
    tmp_path: Path, name: str, source: str,
) -> None:
    source_path = _write_source(tmp_path, name, source)
    rtl = tmp_path / "build" / f"{name}.sv"
    manifest_path = tmp_path / "build" / f"{name}.build.json"
    status, stdout, stderr = _invoke(
        source_path,
        [
            "--systemverilog", str(rtl),
            "--build-manifest", str(manifest_path),
        ],
    )
    assert status == 0, stderr
    assert stdout == "" and stderr == ""
    manifest = WholeBuildManifest.from_json(manifest_path.read_text())
    compilation = compile_file(source_path)
    assert manifest.selected_ir.identity == compilation.selected_ir_identity
    assert manifest.high_level_ir.identity == compilation.high_level_ir_identity
    assert manifest.backend_builds[0].artifact_hash == hashlib.sha256(
        rtl.read_bytes()
    ).hexdigest()
    if name == "InitializedRom":
        assert manifest.backend_builds[0].companions


def _path_dependency_project(root: Path) -> tuple[Path, Path, Path]:
    dependency = root / "logic"
    (dependency / "src").mkdir(parents=True)
    (dependency / "zlang.toml").write_text(
        'schema=1\n[project]\nname="logic"\nversion="1"\nsource-root="src"\n'
    )
    dependency_source = dependency / "src" / "identity.zhl"
    dependency_source.write_text(
        "module Identity { in x:u8 out y:u8 y=x }", encoding="utf-8"
    )

    project = root / "project"
    (project / "src").mkdir(parents=True)
    manifest = project / "zlang.toml"
    manifest.write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n'
        '[dependencies]\nlogic={path="../logic"}\n'
    )
    top = project / "src" / "top.zhl"
    top.write_text(
        "import std.bus.reg import logic.identity "
        "module Top { in x:u8 out y:u8 "
        "inst child:Identity child.x=x y=child.y }",
        encoding="utf-8",
    )
    return manifest, top, dependency_source


def test_locked_path_dependency_closure_is_part_of_the_build_identity(
    tmp_path: Path,
) -> None:
    project, source, dependency_source = _path_dependency_project(tmp_path)
    lock = update_project_lock(project)
    rtl = tmp_path / "published" / "Top.sv"
    manifest_path = tmp_path / "published" / "Top.build.json"
    status, stdout, stderr = _invoke(
        source,
        [
            "--project", str(project),
            "--systemverilog", str(rtl),
            "--build-manifest", str(manifest_path),
        ],
    )
    assert status == 0, stderr
    assert stdout == "" and stderr == ""
    manifest = WholeBuildManifest.from_json(manifest_path.read_text())
    compilation = compile_file(source, project=project)
    assert compilation.ir.dependency_closure is not None
    assert manifest.dependency_closure
    dependencies = {
        item.logical_path: item.content_hash
        for item in manifest.dependency_closure
    }
    assert dependencies["dependencies/logic/identity.zhl"] == hashlib.sha256(
        dependency_source.read_bytes()
    ).hexdigest()
    assert dependencies["dependencies/std/bus/reg.zhl"] == hashlib.sha256(
        (Path(__file__).parents[2] / "stdlib" / "bus" / "reg.zhl").read_bytes()
    ).hexdigest()
    serialized = manifest.to_json()
    assert compilation.ir.dependency_closure.identity in serialized
    assert compilation.ir.dependency_closure.lock_identity in serialized
    assert lock.identity == compilation.ir.dependency_closure.lock_identity


def test_project_snapshot_preserves_workspace_resolution_and_rejects_root_change(
    tmp_path: Path,
) -> None:
    project, source, _ = _path_dependency_project(tmp_path)
    update_project_lock(project)
    snapshot_bytes = source.read_bytes()
    snapshot_text = snapshot_bytes.decode("utf-8")
    snapshot_digest = hashlib.sha256(snapshot_bytes).hexdigest()

    unchanged = compile_file_snapshot(
        source,
        snapshot_text,
        source_digest=snapshot_digest,
        project=project,
    )
    assert unchanged.ir.name == "Top"
    assert unchanged.ir.dependency_closure is not None

    source.write_text(
        "import std.bus.reg import logic.identity "
        "module Top { in x:u8 out y:u8 y=x }",
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceError, match="changed after its compilation snapshot"):
        compile_file_snapshot(
            source,
            snapshot_text,
            source_digest=snapshot_digest,
            project=project,
        )
