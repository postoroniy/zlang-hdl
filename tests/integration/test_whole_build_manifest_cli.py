"""End-to-end publication tests for evidence and whole-build manifests.

These tests deliberately exercise the public CLI boundary while keeping the
suite independent of optional external tools.  Real Clash, Verilator, and
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
    path = root / "src" / f"{name}.zl"
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
    clash = root / "build" / "clash" / "TimedTop.hs"
    direct = root / "build" / "systemverilog" / "TimedTop.sv"
    evidence = root / "reports" / "evidence.json"
    harness = root / "formal" / "TimedTop.formal.sv"
    sby = root / "formal" / "TimedTop.sby"
    manifest_path = root / "build.json"
    status, stdout, stderr = _invoke(
        source,
        [
            "-o", str(clash),
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


@pytest.mark.parametrize("sink_option", ("-o", "--systemverilog"))
def test_explicit_backend_sink_cannot_alias_the_root_source(
    tmp_path: Path, sink_option: str,
) -> None:
    source = _write_source(tmp_path, "packing", PACKING_SOURCE)
    original = source.read_bytes()

    _assert_cli_usage_error(
        source,
        [sink_option, str(source)],
        "collides with the source file",
    )
    assert source.read_bytes() == original


def test_clash_and_direct_systemverilog_sinks_must_be_distinct(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, "packing", PACKING_SOURCE)
    output = tmp_path / "published" / "shared-output"

    _assert_cli_usage_error(
        source,
        ["-o", str(output), "--systemverilog", str(output)],
        "explicit sinks --output and --systemverilog resolve to the same path",
    )
    assert not output.exists()
    assert not output.parent.exists()


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


def test_source_cannot_be_inside_an_owned_output_directory(
    tmp_path: Path,
) -> None:
    output_directory = tmp_path / "project"
    source = _write_source(output_directory, "packing", PACKING_SOURCE)
    original = source.read_bytes()

    _assert_cli_usage_error(
        source,
        ["--verilog-dir", str(output_directory)],
        "source file collides with explicit output directory --verilog-dir",
    )
    assert source.read_bytes() == original


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


def test_independently_owned_output_directories_cannot_overlap(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, "packing", PACKING_SOURCE)
    rtl_directory = tmp_path / "generated"
    formal_cache = rtl_directory / "formal-cache"

    _assert_cli_usage_error(
        source,
        [
            "--verilog-dir", str(rtl_directory),
            "--formal-cache", str(formal_cache),
        ],
        "explicit output directories --verilog-dir and --formal-cache overlap",
    )
    assert not rtl_directory.exists()


def test_deterministic_companion_cannot_alias_an_explicit_file_sink(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, "rom", ROM_SOURCE)
    compilation = compile_file(source, include_clash=False)
    companion = collect_rom_companions(compilation.ir)[0]
    collision = tmp_path / "published" / companion.logical_path

    _assert_cli_usage_error(
        source,
        ["--systemverilog", str(collision)],
        "deterministic direct-SystemVerilog companion collides with explicit "
        "sink --systemverilog",
    )
    assert not collision.parent.exists()


def test_identical_clash_and_direct_companions_may_share_a_destination(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, "rom", ROM_SOURCE)
    compilation = compile_file(source, include_clash=False)
    companion = collect_rom_companions(compilation.ir)[0]
    output_directory = tmp_path / "published"
    clash = output_directory / "InitializedRom.hs"
    direct = output_directory / "InitializedRom.sv"

    status, stdout, stderr = _invoke(
        source,
        ["-o", str(clash), "--systemverilog", str(direct)],
    )
    assert status == 0, stderr
    assert stdout == ""
    assert stderr == ""
    image = output_directory / companion.logical_path
    assert image.read_text(encoding="ascii") == companion.text


def test_deterministic_companion_cannot_resolve_to_a_compilation_input(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, "rom", ROM_SOURCE)
    original = source.read_bytes()
    compilation = compile_file(source, include_clash=False)
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
        ("--verilog-dir", ()),
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


def test_build_manifest_preflight_resolves_symlinked_output_directory(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, "packing", PACKING_SOURCE)
    real_directory = tmp_path / "real-rtl"
    real_directory.mkdir()
    alias = tmp_path / "rtl-link"
    alias.symlink_to(real_directory, target_is_directory=True)
    manifest = real_directory / "PacketPacking.topEntity" / "PacketPacking.v"
    _assert_cli_usage_error(
        source,
        [
            "--verilog-dir", str(alias),
            "--build-manifest", str(manifest),
        ],
        "--build-manifest output is inside explicit output directory --verilog-dir",
    )
    assert tuple(real_directory.iterdir()) == ()


def test_build_manifest_preflight_rejects_deterministic_rom_companion(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, "rom", ROM_SOURCE)
    compilation = compile_file(source, include_clash=False)
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


def test_cli_publishes_two_backends_truthful_evidence_and_manifest(
    tmp_path: Path,
) -> None:
    manifest, physical = _publish_timed_build(tmp_path)
    compilation = compile_file(tmp_path / "src" / "timed.zl", include_clash=False)

    assert manifest.selected_ir.identity == compilation.selected_ir_identity
    assert manifest.high_level_ir.identity == compilation.high_level_ir_identity
    assert manifest.to_json() == (tmp_path / "build.json").read_text(encoding="utf-8")
    assert len(manifest.backend_builds) == 2
    by_backend = {item.backend: item for item in manifest.backend_builds}
    assert set(by_backend) == {"clash", "direct_systemverilog"}
    clash = by_backend["clash"]
    direct = by_backend["direct_systemverilog"]
    assert clash.selected_ir_identity == direct.selected_ir_identity
    assert clash.selected_ir_identity == compilation.selected_ir_identity
    assert clash.build_identity != direct.build_identity
    assert clash.artifact_hash != direct.artifact_hash
    assert clash.backend != direct.backend

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
    source = tmp_path / "src" / "crlf.zl"
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


def test_validation_rejects_missing_generated_external_rtl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_generate_verilog(
        _clash: str,
        module_name: str,
        output_directory: Path,
        _executable: str | None = None,
        *,
        companions=(),
        source_map=None,
        public_wrapper=None,
    ) -> tuple[Path, ...]:
        del companions, source_map, public_wrapper
        rtl = Path(output_directory) / f"{module_name}.topEntity" / f"{module_name}.v"
        rtl.parent.mkdir(parents=True, exist_ok=True)
        rtl.write_text(f"module {module_name}; endmodule\n", encoding="utf-8")
        return (rtl,)

    monkeypatch.setattr("zlang.cli.generate_verilog", fake_generate_verilog)
    source = _write_source(tmp_path, "packing", PACKING_SOURCE)
    clash = tmp_path / "build" / "PacketPacking.hs"
    rtl_dir = tmp_path / "build" / "rtl"
    manifest_path = tmp_path / "build.json"
    status, stdout, stderr = _invoke(
        source,
        [
            "-o", str(clash),
            "--verilog-dir", str(rtl_dir),
            "--build-manifest", str(manifest_path),
        ],
    )
    assert status == 0, stderr
    assert stdout == "" and stderr == ""
    manifest = WholeBuildManifest.from_json(manifest_path.read_text())
    generated = next(
        item for item in manifest.backend_builds[0].files
        if item.logical_path.endswith(".v")
    )
    physical = _physical_file_map(
        manifest,
        tuple(path for path in tmp_path.rglob("*") if path != manifest_path),
    )
    physical[generated.logical_path].unlink()
    with pytest.raises(BuildManifestError, match="missing"):
        validate_manifest_file_map(manifest, physical)


def test_verilog_directory_only_manifest_publishes_exact_clash_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_generate_verilog(
        _clash: str,
        module_name: str,
        output_directory: Path,
        _executable: str | None = None,
        *,
        companions=(),
        source_map=None,
        public_wrapper=None,
    ) -> tuple[Path, ...]:
        del companions, source_map, public_wrapper
        rtl = Path(output_directory) / f"{module_name}.topEntity" / f"{module_name}.v"
        rtl.parent.mkdir(parents=True, exist_ok=True)
        rtl.write_text(f"module {module_name}; endmodule\n", encoding="utf-8")
        return (rtl,)

    monkeypatch.setattr("zlang.cli.generate_verilog", fake_generate_verilog)
    monkeypatch.setattr("zlang.cli._query_tool_version", lambda *_args: "Clash 1.11")
    source = _write_source(tmp_path, "packing", PACKING_SOURCE)
    rtl_dir = tmp_path / "build" / "rtl"
    rtl_dir.mkdir(parents=True)
    stale = rtl_dir / "stale.v"
    stale.write_text("module Stale; endmodule\n", encoding="utf-8")
    manifest_path = tmp_path / "build" / "PacketPacking.build.json"

    status, stdout, stderr = _invoke(
        source,
        [
            "--verilog-dir", str(rtl_dir),
            "--clash", "/tool/clash",
            "--build-manifest", str(manifest_path),
        ],
    )
    assert status == 0, stderr
    assert stdout == "" and stderr == ""

    manifest = WholeBuildManifest.from_json(manifest_path.read_text())
    assert len(manifest.backend_builds) == 1
    backend = manifest.backend_builds[0]
    clash_source = next(item for item in backend.files if item.kind == "clash_source")
    generated = next(item for item in backend.files if item.kind == "generated_verilog")
    assert clash_source.content_hash == backend.artifact_hash
    assert all(item.content_hash != hashlib.sha256(stale.read_bytes()).hexdigest()
               for item in backend.files)
    assert tuple(manifest.tool_executions[0].outputs) == (generated.logical_path,)

    retained_source = rtl_dir / ".zlang" / "generated" / "PacketPacking.hs"
    generated_rtl = rtl_dir / "PacketPacking.topEntity" / "PacketPacking.v"
    validate_manifest_file_map(
        manifest,
        {
            manifest.root_source.logical_path: source,
            clash_source.logical_path: retained_source,
            generated.logical_path: generated_rtl,
        },
    )


def test_verilog_dir_manifest_records_root_and_module_local_rom_companions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_generate_verilog(
        _clash: str,
        module_name: str,
        output_directory: Path,
        _executable: str | None = None,
        *,
        companions=(),
        source_map=None,
        public_wrapper=None,
    ) -> tuple[Path, ...]:
        del source_map, public_wrapper
        output_directory = Path(output_directory)
        rtl = output_directory / f"{module_name}.topEntity" / f"{module_name}.v"
        rtl.parent.mkdir(parents=True, exist_ok=True)
        rtl.write_text(f"module {module_name}; endmodule\n", encoding="utf-8")
        publish_companion_bundle(companions, output_directory)
        publish_companion_bundle(companions, rtl.parent)
        return (rtl,)

    monkeypatch.setattr("zlang.cli.generate_verilog", fake_generate_verilog)
    monkeypatch.setattr("zlang.cli._query_tool_version", lambda *_args: "Clash 1.11")
    source = _write_source(tmp_path, "rom", ROM_SOURCE)
    rtl_dir = tmp_path / "build" / "rtl"
    manifest_path = tmp_path / "build" / "InitializedRom.build.json"

    status, stdout, stderr = _invoke(
        source,
        [
            "--verilog-dir", str(rtl_dir),
            "--clash", "/tool/clash",
            "--build-manifest", str(manifest_path),
        ],
    )
    assert status == 0, stderr
    assert stdout == "" and stderr == ""

    manifest = WholeBuildManifest.from_json(manifest_path.read_text())
    companions = manifest.backend_builds[0].companions
    assert len(companions) == 2
    assert len({item.logical_path for item in companions}) == 2
    expected_hash = collect_rom_companions(
        compile_file(source, include_clash=False).ir
    )[0].file_hash
    assert {item.content_hash for item in companions} == {expected_hash}
    prefix = "backends/clash/companions/rtl/"
    for companion in companions:
        assert companion.logical_path.startswith(prefix)
        physical = rtl_dir / companion.logical_path.removeprefix(prefix)
        assert physical.is_file()
        assert hashlib.sha256(physical.read_bytes()).hexdigest() == expected_hash


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
    compilation = compile_file(source_path, include_clash=False)
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
    dependency_source = dependency / "src" / "identity.zl"
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
    top = project / "src" / "top.zl"
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
    compilation = compile_file(source, include_clash=False, project=project)
    assert compilation.ir.dependency_closure is not None
    assert manifest.dependency_closure
    dependencies = {
        item.logical_path: item.content_hash
        for item in manifest.dependency_closure
    }
    assert dependencies["dependencies/logic/identity.zl"] == hashlib.sha256(
        dependency_source.read_bytes()
    ).hexdigest()
    assert dependencies["dependencies/std/bus/reg.zl"] == hashlib.sha256(
        (Path(__file__).parents[2] / "stdlib" / "bus" / "reg.zl").read_bytes()
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
        include_clash=False,
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
            include_clash=False,
        )
