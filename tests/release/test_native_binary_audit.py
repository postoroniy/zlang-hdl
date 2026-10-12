from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile, ZipInfo

import pytest

from tools.audit_native_binary import (
    NativeBinaryAuditError,
    audit_native_binary,
    audit_native_release_set,
)


def _native_payload(platform: str) -> bytes:
    if platform == "manylinux_2_28_x86_64":
        payload = bytearray(20)
        payload[:6] = b"\x7fELF\x02\x01"
        payload[18:20] = (62).to_bytes(2, "little")
        return bytes(payload)
    cpu_type = {
        "macosx_11_0_x86_64": 0x01000007,
        "macosx_11_0_arm64": 0x0100000C,
    }[platform]
    return b"\xcf\xfa\xed\xfe" + cpu_type.to_bytes(4, "little")


def _write_wheel(
    path: Path,
    *,
    platform: str,
    version: str = "0.1.0a16",
    payload_platform: str | None = None,
) -> None:
    distribution = f"zlang_native_sim-{version}.dist-info"
    with ZipFile(path, "w") as archive:
        archive.writestr("LICENSE", "license")
        archive.writestr("NOTICE", "notice")
        archive.writestr(
            "THIRD_PARTY_LICENSES.md",
            "| `serde` | `1.0.0` | `MIT` | `" + "0" * 64 + "` |\n",
        )
        archive.writestr("_zlang_native_sim/__init__.py", "package")
        archive.writestr(
            "_zlang_native_sim/_zlang_native_sim.abi3.so",
            _native_payload(payload_platform or platform),
        )
        archive.writestr(
            f"{distribution}/METADATA",
            f"Name: zlang-native-sim\nVersion: {version}\n",
        )
        archive.writestr(
            f"{distribution}/WHEEL",
            f"Root-Is-Purelib: false\nTag: cp312-abi3-{platform}\n",
        )
        archive.writestr(
            f"{distribution}/sboms/runtime.cdx.json",
            json.dumps(
                {
                    "bomFormat": "CycloneDX",
                    "specVersion": "1.5",
                    "components": [
                        {
                            "name": "serde",
                            "version": "1.0.0",
                            "purl": "pkg:cargo/serde@1.0.0",
                            "licenses": [{"expression": "MIT"}],
                        }
                    ],
                }
            ),
        )


def _rewrite_wheel(
    source: Path,
    destination: Path,
    *,
    replace: dict[str, bytes] | None = None,
    rename: dict[str, str] | None = None,
    omit: frozenset[str] = frozenset(),
) -> None:
    replacements = replace or {}
    renames = rename or {}
    with ZipFile(source) as source_archive, ZipFile(destination, "w") as target:
        for info in source_archive.infolist():
            if info.filename in omit:
                continue
            target.writestr(
                renames.get(info.filename, info.filename),
                replacements.get(info.filename, source_archive.read(info.filename)),
            )


def test_native_binary_audit_rejects_missing_sbom(tmp_path: Path) -> None:
    wheel = tmp_path / "native.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("LICENSE", "license")
        archive.writestr("NOTICE", "notice")
        archive.writestr(
            "THIRD_PARTY_LICENSES.md",
            "| `serde` | `1.0.0` | `MIT` | `" + "0" * 64 + "` |\n",
        )
        archive.writestr("_zlang_native_sim/__init__.py", "package")
        archive.writestr(
            "_zlang_native_sim/_zlang_native_sim.abi3.so",
            _native_payload("manylinux_2_28_x86_64"),
        )
        archive.writestr(
            "zlang_native_sim-1.0.dist-info/METADATA",
            "Name: zlang-native-sim\nVersion: 1.0\n",
        )
        archive.writestr(
            "zlang_native_sim-1.0.dist-info/WHEEL",
            "Root-Is-Purelib: false\nTag: cp312-abi3-manylinux_2_28_x86_64\n",
        )

    with pytest.raises(NativeBinaryAuditError, match="CycloneDX SBOM"):
        audit_native_binary(wheel)


def test_native_release_set_requires_linux_only(
    tmp_path: Path,
) -> None:
    linux = tmp_path / "linux.whl"
    _write_wheel(linux, platform="manylinux_2_28_x86_64")
    infos = audit_native_release_set((linux,), expected_version="0.1.0a16")
    assert [info.platform for info in infos] == ["linux_x86_64"]

    macos = tmp_path / "macos.whl"
    _write_wheel(macos, platform="macosx_11_0_arm64")
    with pytest.raises(NativeBinaryAuditError, match="extra=.*macos"):
        audit_native_release_set((linux, macos), expected_version="0.1.0a16")


def test_native_release_set_rejects_empty_or_wrong_version(
    tmp_path: Path,
) -> None:
    linux = tmp_path / "linux.whl"
    _write_wheel(linux, platform="manylinux_2_28_x86_64")

    with pytest.raises(NativeBinaryAuditError, match="missing=.*linux"):
        audit_native_release_set((), expected_version="0.1.0a16")
    with pytest.raises(NativeBinaryAuditError, match="does not match"):
        audit_native_binary(linux, expected_version="0.1.0a12")


def test_native_binary_audit_rejects_wrong_binary_architecture(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "native.whl"
    _write_wheel(
        wheel,
        platform="macosx_11_0_arm64",
        payload_platform="macosx_11_0_x86_64",
    )

    with pytest.raises(NativeBinaryAuditError, match="do not match release platform"):
        audit_native_binary(wheel)


@pytest.mark.parametrize(
    "member",
    (
        "src/lib.rs",
        "Cargo.toml",
        "Cargo.lock",
        "rust-toolchain",
        "rust-toolchain.toml",
        ".cargo/config.toml",
        "target/release/runtime.o",
    ),
)
def test_native_binary_audit_rejects_runtime_source_and_build_files(
    tmp_path: Path,
    member: str,
) -> None:
    wheel = tmp_path / "native.whl"
    _write_wheel(wheel, platform="manylinux_2_28_x86_64")
    with ZipFile(wheel, "a") as archive:
        archive.writestr(member, "not release content")

    with pytest.raises(NativeBinaryAuditError, match="Rust source/build files"):
        audit_native_binary(wheel)


@pytest.mark.parametrize(
    ("document", "message"),
    (
        (b"not json", "malformed JSON"),
        (json.dumps([]).encode(), "JSON object"),
        (
            json.dumps(
                {"bomFormat": "CycloneDX", "specVersion": "1.5", "components": []}
            ).encode(),
            "non-empty component list",
        ),
        (
            json.dumps(
                {
                    "bomFormat": "CycloneDX",
                    "specVersion": "1.5",
                    "components": [
                        {
                            "name": "serde",
                            "version": "1.0.0",
                            "licenses": [{"expression": "MIT"}],
                        }
                    ],
                }
            ).encode(),
            "exact Cargo package URL",
        ),
    ),
)
def test_native_binary_audit_rejects_malformed_or_incomplete_sbom(
    tmp_path: Path, document: bytes, message: str
) -> None:
    wheel = tmp_path / "native.whl"
    _write_wheel(wheel, platform="manylinux_2_28_x86_64")
    with ZipFile(wheel) as archive:
        original = next(
            name for name in archive.namelist() if "/sboms/" in name
        )
    # Duplicate names are independently rejected before a parser could
    # accidentally select one. Build a clean replacement archive instead.
    replacement = tmp_path / "replacement.whl"
    with ZipFile(wheel) as source, ZipFile(replacement, "w") as target:
        for info in source.infolist():
            if info.filename == original:
                target.writestr(info.filename, document)
            else:
                target.writestr(info, source.read(info.filename))

    with pytest.raises(NativeBinaryAuditError, match=message):
        audit_native_binary(replacement)


def test_native_binary_audit_rejects_unsafe_paths_and_symlinks(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe.whl"
    _write_wheel(unsafe, platform="manylinux_2_28_x86_64")
    with ZipFile(unsafe, "a") as archive:
        archive.writestr("../escape", b"unsafe")
    with pytest.raises(NativeBinaryAuditError, match="unsafe path"):
        audit_native_binary(unsafe)

    symlink = tmp_path / "symlink.whl"
    _write_wheel(symlink, platform="manylinux_2_28_x86_64")
    link = ZipInfo("package-link")
    link.create_system = 3
    link.external_attr = 0o120777 << 16
    with ZipFile(symlink, "a") as archive:
        archive.writestr(link, b"LICENSE")
    with pytest.raises(NativeBinaryAuditError, match="symbolic link"):
        audit_native_binary(symlink)


def test_native_binary_audit_rejects_multiple_or_misplaced_payloads(
    tmp_path: Path,
) -> None:
    multiple = tmp_path / "multiple.whl"
    _write_wheel(multiple, platform="manylinux_2_28_x86_64")
    with ZipFile(multiple, "a") as archive:
        archive.writestr("extra.so", _native_payload("manylinux_2_28_x86_64"))
    with pytest.raises(NativeBinaryAuditError, match="exactly one importable"):
        audit_native_binary(multiple)

    source = tmp_path / "source.whl"
    misplaced = tmp_path / "misplaced.whl"
    _write_wheel(source, platform="manylinux_2_28_x86_64")
    _rewrite_wheel(
        source,
        misplaced,
        rename={
            "_zlang_native_sim/_zlang_native_sim.abi3.so": "elsewhere/runtime.abi3.so"
        },
    )
    with pytest.raises(NativeBinaryAuditError, match="exactly one importable"):
        audit_native_binary(misplaced)


@pytest.mark.parametrize(
    ("member_suffix", "replacement", "message"),
    (
        (".dist-info/METADATA", b"Name: another-package\nVersion: 0.1.0a16\n", "package name"),
        (".dist-info/WHEEL", b"Root-Is-Purelib: true\nTag: cp312-abi3-manylinux_2_28_x86_64\n", "marked pure"),
        (".dist-info/WHEEL", b"Root-Is-Purelib: false\nTag: cp312-cp312-manylinux_2_28_x86_64\n", "stable abi3"),
        (".dist-info/WHEEL", b"Root-Is-Purelib: false\nTag: cp312-abi3-win_amd64\n", "unsupported native wheel platform"),
    ),
)
def test_native_binary_audit_rejects_invalid_package_and_wheel_metadata(
    tmp_path: Path, member_suffix: str, replacement: bytes, message: str
) -> None:
    source = tmp_path / "source.whl"
    changed = tmp_path / "changed.whl"
    _write_wheel(source, platform="manylinux_2_28_x86_64")
    with ZipFile(source) as archive:
        member = next(name for name in archive.namelist() if name.endswith(member_suffix))
    _rewrite_wheel(source, changed, replace={member: replacement})
    with pytest.raises(NativeBinaryAuditError, match=message):
        audit_native_binary(changed)


@pytest.mark.parametrize("document", ("LICENSE", "NOTICE", "THIRD_PARTY_LICENSES.md"))
def test_native_binary_audit_rejects_missing_legal_document(
    tmp_path: Path, document: str
) -> None:
    source = tmp_path / "source.whl"
    changed = tmp_path / "changed.whl"
    _write_wheel(source, platform="manylinux_2_28_x86_64")
    _rewrite_wheel(source, changed, omit=frozenset({document}))
    with pytest.raises(NativeBinaryAuditError, match=f"missing {document}"):
        audit_native_binary(changed)


@pytest.mark.parametrize(
    ("inventory", "message"),
    (
        (b"no package rows\n", "no dependency rows"),
        (
            b"| `serde` | `1.0.0` | `Proprietary` | `" + b"0" * 64 + b"` |\n",
            "unreviewed license",
        ),
        (b"\xff", "not valid UTF-8"),
    ),
)
def test_native_binary_audit_rejects_malformed_dependency_inventory(
    tmp_path: Path, inventory: bytes, message: str
) -> None:
    source = tmp_path / "source.whl"
    changed = tmp_path / "changed.whl"
    _write_wheel(source, platform="manylinux_2_28_x86_64")
    _rewrite_wheel(
        source, changed, replace={"THIRD_PARTY_LICENSES.md": inventory}
    )
    with pytest.raises(NativeBinaryAuditError, match=message):
        audit_native_binary(changed)
