from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
from pathlib import Path

import pytest

from zlang.cli import main
from zlang.generated_navigation_bundle import (
    SourceSnapshotStatus,
    load_generated_navigation_bundle,
)
from zlang.workspace import update_project_lock


SOURCE = """import std.math.complex

module PublishedBundle {
    in a : u8
    in b : u8
    out y : u9
    y = a + b
}
"""


def _invoke(source: Path, arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        status = main((str(source), *arguments))
    return status, stdout.getvalue(), stderr.getvalue()


def test_cli_publishes_bundle_with_existing_rtl_map_and_manifest_flow(
    tmp_path: Path,
) -> None:
    source = tmp_path / "published_bundle.zhl"
    source.write_text(SOURCE, encoding="utf-8")
    rtl = tmp_path / "outputs" / "PublishedBundle.sv"
    source_map = tmp_path / "outputs" / "PublishedBundle.source-map.json"
    build_manifest = tmp_path / "outputs" / "build.json"
    bundle = tmp_path / "outputs" / "navigation"

    status, stdout, stderr = _invoke(
        source,
        [
            "--systemverilog",
            str(rtl),
            "--source-map",
            str(source_map),
            "--build-manifest",
            str(build_manifest),
            "--generated-navigation-bundle",
            str(bundle),
        ],
    )

    assert status == 0, stderr
    assert stdout == "" and stderr == ""
    loaded = load_generated_navigation_bundle(bundle)
    assert loaded.generated_text == rtl.read_text(encoding="utf-8")
    assert loaded.source_map.to_json() == source_map.read_text(encoding="utf-8")
    assert loaded.source_status(source.name, SOURCE) is SourceSnapshotStatus.MATCH
    snapshots = {item.source_unit: item.digest for item in loaded.manifest.sources}
    assert snapshots[source.name] == hashlib.sha256(SOURCE.encode()).hexdigest()
    assert snapshots["std.math.complex"] == hashlib.sha256(
        (Path(__file__).parents[2] / "stdlib" / "math" / "complex.zhl").read_bytes()
    ).hexdigest()
    assert build_manifest.is_file()


def test_cli_bundle_can_publish_its_own_map_without_separate_sidecar(
    tmp_path: Path,
) -> None:
    source = tmp_path / "published_bundle.zhl"
    source.write_text(SOURCE, encoding="utf-8")
    rtl = tmp_path / "PublishedBundle.sv"
    bundle = tmp_path / "navigation"

    status, stdout, stderr = _invoke(
        source,
        [
            "--systemverilog",
            str(rtl),
            "--generated-navigation-bundle",
            str(bundle),
        ],
    )

    assert status == 0, stderr
    assert stdout == "" and stderr == ""
    assert load_generated_navigation_bundle(bundle).source_map.entries


def test_bundle_option_does_not_change_generated_rtl_text(tmp_path: Path) -> None:
    source = tmp_path / "published_bundle.zhl"
    source.write_text(SOURCE, encoding="utf-8")
    plain_rtl = tmp_path / "plain" / "PublishedBundle.sv"
    bundled_rtl = tmp_path / "bundled" / "PublishedBundle.sv"

    plain_status, _, plain_stderr = _invoke(
        source, ["--systemverilog", str(plain_rtl)]
    )
    bundled_status, _, bundled_stderr = _invoke(
        source,
        [
            "--systemverilog",
            str(bundled_rtl),
            "--generated-navigation-bundle",
            str(tmp_path / "navigation"),
        ],
    )

    assert plain_status == bundled_status == 0, plain_stderr + bundled_stderr
    assert plain_rtl.read_bytes() == bundled_rtl.read_bytes()


def test_cli_bundle_requires_explicit_systemverilog_output(tmp_path: Path) -> None:
    source = tmp_path / "published_bundle.zhl"
    source.write_text(SOURCE, encoding="utf-8")
    stdout = io.StringIO()
    stderr = io.StringIO()

    with redirect_stdout(stdout), redirect_stderr(stderr):
        with pytest.raises(SystemExit) as raised:
            main((str(source), "--generated-navigation-bundle", str(tmp_path / "nav")))

    assert raised.value.code == 2
    assert stdout.getvalue() == ""
    assert "--generated-navigation-bundle requires --systemverilog" in stderr.getvalue()


def test_cli_preflight_rejects_bundle_overlapping_the_source(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    source = root / "published_bundle.zhl"
    source.write_text(SOURCE, encoding="utf-8")
    rtl = tmp_path / "PublishedBundle.sv"
    stdout = io.StringIO()
    stderr = io.StringIO()

    with redirect_stdout(stdout), redirect_stderr(stderr):
        with pytest.raises(SystemExit) as raised:
            main(
                (
                    str(source),
                    "--systemverilog",
                    str(rtl),
                    "--generated-navigation-bundle",
                    str(root),
                )
            )

    assert raised.value.code == 2
    assert stdout.getvalue() == ""
    assert "source file collides with explicit output directory" in stderr.getvalue()


def test_project_bundle_records_locked_root_and_dependency_snapshots(
    tmp_path: Path,
) -> None:
    dependency = tmp_path / "logic"
    (dependency / "src").mkdir(parents=True)
    (dependency / "zlang.toml").write_text(
        'schema=1\n[project]\nname="logic"\nversion="1"\nsource-root="src"\n'
    )
    dependency_source = dependency / "src" / "identity.zhl"
    dependency_source.write_text(
        "module Identity { in x:u8 out y:u8 y=x }", encoding="utf-8"
    )

    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    project_manifest = project / "zlang.toml"
    project_manifest.write_text(
        'schema=1\n[project]\nname="demo"\nversion="1"\nsource-root="src"\n'
        '[dependencies]\nlogic={path="../logic"}\n'
    )
    source = project / "src" / "top.zhl"
    source.write_text(
        "import std.bus.reg import logic.identity "
        "module Top { in x:u8 out y:u8 "
        "inst child:Identity child.x=x y=child.y }",
        encoding="utf-8",
    )
    update_project_lock(project_manifest)
    rtl = tmp_path / "Top.sv"
    bundle = tmp_path / "navigation"

    status, stdout, stderr = _invoke(
        source,
        [
            "--project",
            str(project_manifest),
            "--systemverilog",
            str(rtl),
            "--generated-navigation-bundle",
            str(bundle),
        ],
    )

    assert status == 0, stderr
    assert stdout == "" and stderr == ""
    loaded = load_generated_navigation_bundle(bundle)
    snapshots = {
        item.source_unit: (item.role, item.digest)
        for item in loaded.manifest.sources
    }
    assert snapshots["demo.top"] == (
        "root",
        hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    assert snapshots["logic.identity"] == (
        "dependency",
        hashlib.sha256(dependency_source.read_bytes()).hexdigest(),
    )
    assert snapshots["std.bus.reg"][0] == "dependency"
    assert loaded.source_map.entries == ()
