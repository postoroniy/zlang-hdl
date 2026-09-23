from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

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
    version: str = "0.1.0a11",
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
                    "components": [
                        {
                            "name": "serde",
                            "version": "1.0.0",
                            "licenses": [{"expression": "MIT"}],
                        }
                    ],
                }
            ),
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
    infos = audit_native_release_set((linux,), expected_version="0.1.0a11")
    assert [info.platform for info in infos] == ["linux_x86_64"]

    macos = tmp_path / "macos.whl"
    _write_wheel(macos, platform="macosx_11_0_arm64")
    with pytest.raises(NativeBinaryAuditError, match="extra=.*macos"):
        audit_native_release_set((linux, macos), expected_version="0.1.0a11")


def test_native_release_set_rejects_empty_or_wrong_version(
    tmp_path: Path,
) -> None:
    linux = tmp_path / "linux.whl"
    _write_wheel(linux, platform="manylinux_2_28_x86_64")

    with pytest.raises(NativeBinaryAuditError, match="missing=.*linux"):
        audit_native_release_set((), expected_version="0.1.0a11")
    with pytest.raises(NativeBinaryAuditError, match="does not match"):
        audit_native_binary(linux, expected_version="0.1.0a13")


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
