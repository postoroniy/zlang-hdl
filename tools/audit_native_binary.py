"""Audit a distributed native-simulation wheel without requiring Rust sources."""

from __future__ import annotations

from dataclasses import dataclass
from email.parser import Parser
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from zipfile import ZipFile, ZipInfo


ALLOWED_LICENSE_IDS = frozenset(
    {
        "Apache-2.0",
        "BSD-2-Clause",
        "LLVM-exception",
        "MIT",
        "Unicode-3.0",
        "Unlicense",
        "Zlib",
    }
)
MAX_WHEEL_BYTES = 16 * 1024 * 1024
MAX_SBOM_BYTES = 4 * 1024 * 1024
RELEASE_PLATFORMS = frozenset({"linux_x86_64"})
_INVENTORY_ROW = re.compile(
    r"^\| `(?P<name>[^`]+)` \| `(?P<version>[^`]+)` \| "
    r"`(?P<license>[^`]+)` \| `(?P<checksum>[0-9a-f]{64})` \|$"
)


class NativeBinaryAuditError(ValueError):
    """A native-runtime wheel is malformed or not release-safe."""


@dataclass(frozen=True)
class NativeBinaryInfo:
    """Release-relevant identity recovered from a validated binary wheel."""

    path: Path
    version: str
    platform: str


def license_ids(expression: str) -> frozenset[str]:
    """Return reviewed SPDX identifiers from a simple license expression."""

    normalized = expression.replace("/", " OR ")
    identifiers = frozenset(
        item
        for item in re.findall(r"[A-Za-z0-9.-]+", normalized)
        if item not in {"AND", "OR", "WITH"}
    )
    if not identifiers or not identifiers <= ALLOWED_LICENSE_IDS:
        unknown = sorted(identifiers - ALLOWED_LICENSE_IDS)
        raise NativeBinaryAuditError(
            f"unreviewed license expression {expression!r}; unknown IDs: {unknown}"
        )
    return identifiers


def parse_inventory(document: str) -> dict[tuple[str, str], frozenset[str]]:
    """Parse the exact locked-crate table shipped in the wheel."""

    result: dict[tuple[str, str], frozenset[str]] = {}
    for line in document.splitlines():
        match = _INVENTORY_ROW.fullmatch(line)
        if match is None:
            continue
        key = (match.group("name"), match.group("version"))
        if key in result:
            raise NativeBinaryAuditError(f"duplicate dependency inventory row {key}")
        result[key] = license_ids(match.group("license"))
    if not result:
        raise NativeBinaryAuditError("third-party inventory has no dependency rows")
    return result


def _safe_member(info: ZipInfo) -> None:
    path = PurePosixPath(info.filename)
    if path.is_absolute() or ".." in path.parts:
        raise NativeBinaryAuditError("wheel contains an unsafe path")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise NativeBinaryAuditError("wheel contains a symbolic link")


def _single_member(archive: ZipFile, suffix: str) -> str:
    candidates = [name for name in archive.namelist() if name.endswith(suffix)]
    if len(candidates) != 1:
        raise NativeBinaryAuditError(
            f"wheel must contain exactly one member ending with {suffix!r}"
        )
    return candidates[0]


def _read_utf8_member(archive: ZipFile, name: str, context: str) -> str:
    try:
        return archive.read(name).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NativeBinaryAuditError(f"wheel {context} is not valid UTF-8") from exc


def _sbom_document(archive: ZipFile) -> tuple[bytes, dict[str, object]]:
    candidates = [
        name
        for name in archive.namelist()
        if "/sboms/" in name and name.endswith(".json")
    ]
    if len(candidates) != 1:
        raise NativeBinaryAuditError("wheel must contain exactly one CycloneDX SBOM")
    if archive.getinfo(candidates[0]).file_size > MAX_SBOM_BYTES:
        raise NativeBinaryAuditError("wheel SBOM exceeds 4 MiB")
    payload = archive.read(candidates[0])
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NativeBinaryAuditError("wheel SBOM is malformed JSON") from exc
    if not isinstance(value, dict):
        raise NativeBinaryAuditError("wheel SBOM must be a JSON object")
    if value.get("bomFormat") != "CycloneDX":
        raise NativeBinaryAuditError("wheel SBOM is not CycloneDX")
    if value.get("specVersion") != "1.5":
        raise NativeBinaryAuditError("wheel SBOM has an unsupported specification version")
    components = value.get("components")
    if not isinstance(components, list) or not components:
        raise NativeBinaryAuditError("wheel SBOM has no non-empty component list")
    return payload, value


def _sbom_inventory(archive: ZipFile) -> dict[tuple[str, str], frozenset[str]]:
    _, value = _sbom_document(archive)
    components = value["components"]
    assert isinstance(components, list)
    result: dict[tuple[str, str], frozenset[str]] = {}
    for component in components:
        if not isinstance(component, dict):
            raise NativeBinaryAuditError("wheel SBOM component is not an object")
        name = component.get("name")
        version = component.get("version")
        purl = component.get("purl")
        licenses = component.get("licenses")
        if not isinstance(name, str) or not isinstance(version, str):
            raise NativeBinaryAuditError("wheel SBOM component has no name/version")
        if purl != f"pkg:cargo/{name}@{version}":
            raise NativeBinaryAuditError(
                "wheel SBOM component has no exact Cargo package URL"
            )
        if not isinstance(licenses, list) or len(licenses) != 1:
            raise NativeBinaryAuditError("wheel SBOM component has invalid licenses")
        license_record = licenses[0]
        if not isinstance(license_record, dict):
            raise NativeBinaryAuditError("wheel SBOM license is not an object")
        expression = license_record.get("expression")
        if not isinstance(expression, str):
            raise NativeBinaryAuditError(
                "wheel SBOM component has no license expression"
            )
        key = (name, version)
        if key in result:
            raise NativeBinaryAuditError(f"duplicate wheel SBOM component {key}")
        result[key] = license_ids(expression)
    return result


def read_native_sbom(wheel: Path) -> bytes:
    """Return the exact validated CycloneDX bytes embedded in a binary wheel."""

    if wheel.stat().st_size > MAX_WHEEL_BYTES:
        raise NativeBinaryAuditError("native runtime wheel exceeds 16 MiB")
    with ZipFile(wheel) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise NativeBinaryAuditError("wheel contains duplicate members")
        for info in infos:
            _safe_member(info)
        payload, _ = _sbom_document(archive)
        return payload


def _release_platform(tags: list[str]) -> str:
    platforms = set()
    for tag in tags:
        parts = tag.split("-", 2)
        if len(parts) != 3 or parts[0] != "cp312" or parts[1] != "abi3":
            raise NativeBinaryAuditError(f"unsupported native wheel tag {tag!r}")
        platform = parts[2]
        if platform == "manylinux_2_28_x86_64":
            platforms.add("linux_x86_64")
        elif platform == "macosx_11_0_x86_64":
            platforms.add("macos_x86_64")
        elif platform == "macosx_11_0_arm64":
            platforms.add("macos_arm64")
        else:
            raise NativeBinaryAuditError(
                f"unsupported native wheel platform tag {platform!r}"
            )
    if len(platforms) != 1:
        raise NativeBinaryAuditError("wheel mixes release platform tags")
    return platforms.pop()


def _validate_native_payload(payload: bytes, platform: str) -> None:
    if platform == "linux_x86_64":
        valid = (
            len(payload) >= 20
            and payload[:4] == b"\x7fELF"
            and payload[4] == 2
            and payload[5] == 1
            and int.from_bytes(payload[18:20], "little") == 62
        )
    else:
        cpu_type = {
            "macos_x86_64": 0x01000007,
            "macos_arm64": 0x0100000C,
        }[platform]
        valid = (
            len(payload) >= 8
            and payload[:4] == b"\xcf\xfa\xed\xfe"
            and int.from_bytes(payload[4:8], "little") == cpu_type
        )
    if not valid:
        raise NativeBinaryAuditError(
            f"native extension bytes do not match release platform {platform!r}"
        )


def audit_native_binary(
    wheel: Path, *, expected_version: str | None = None
) -> NativeBinaryInfo:
    """Validate a platform wheel and its self-contained supply-chain evidence."""

    if wheel.stat().st_size > MAX_WHEEL_BYTES:
        raise NativeBinaryAuditError("native runtime wheel exceeds 16 MiB")
    with ZipFile(wheel) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise NativeBinaryAuditError("wheel contains duplicate members")
        for info in infos:
            _safe_member(info)
        if any(
            name.endswith(
                (
                    ".rs",
                    "Cargo.toml",
                    "Cargo.lock",
                    "rust-toolchain",
                    "rust-toolchain.toml",
                )
            )
            or ".cargo" in PurePosixPath(name).parts
            or "target" in PurePosixPath(name).parts
            for name in names
        ):
            raise NativeBinaryAuditError("binary wheel contains Rust source/build files")
        native_payloads = [
            name for name in names if name.endswith((".so", ".dylib", ".a", ".o"))
        ]
        extensions = [
            name
            for name in native_payloads
            if PurePosixPath(name).parent == PurePosixPath("_zlang_native_sim")
            and PurePosixPath(name).name == "_zlang_native_sim.abi3.so"
        ]
        if (
            len(native_payloads) != 1
            or len(extensions) != 1
            or names.count("_zlang_native_sim/__init__.py") != 1
        ):
            raise NativeBinaryAuditError(
                "wheel must contain exactly one importable abi3 native package"
            )
        metadata_name = _single_member(archive, ".dist-info/METADATA")
        metadata = Parser().parsestr(
            _read_utf8_member(archive, metadata_name, "package metadata")
        )
        if metadata["Name"] != "zlang-native-sim":
            raise NativeBinaryAuditError("wheel has an unexpected package name")
        version = metadata["Version"]
        if not version:
            raise NativeBinaryAuditError("wheel has no package version")
        if expected_version is not None and version != expected_version:
            raise NativeBinaryAuditError(
                f"native wheel version {version!r} does not match "
                f"release version {expected_version!r}"
            )
        wheel_name = _single_member(archive, ".dist-info/WHEEL")
        wheel_metadata = Parser().parsestr(
            _read_utf8_member(archive, wheel_name, "wheel metadata")
        )
        if wheel_metadata["Root-Is-Purelib"] != "false":
            raise NativeBinaryAuditError("native wheel is incorrectly marked pure")
        tags = wheel_metadata.get_all("Tag", failobj=[])
        if not tags or not all("-abi3-" in tag for tag in tags):
            raise NativeBinaryAuditError("native wheel does not use the stable abi3 tag")
        platform = _release_platform(tags)
        _validate_native_payload(archive.read(extensions[0]), platform)
        for document in ("LICENSE", "NOTICE", "THIRD_PARTY_LICENSES.md"):
            if document not in names:
                raise NativeBinaryAuditError(f"wheel is missing {document}")
        inventory = parse_inventory(
            _read_utf8_member(
                archive, "THIRD_PARTY_LICENSES.md", "third-party inventory"
            )
        )
        sbom = _sbom_inventory(archive)
    if sbom != inventory:
        raise NativeBinaryAuditError(
            "wheel SBOM does not match its locked third-party inventory"
        )
    return NativeBinaryInfo(
        path=wheel.resolve(),
        version=version,
        platform=platform,
    )


def audit_native_release_set(
    wheels: tuple[Path, ...], *, expected_version: str
) -> tuple[NativeBinaryInfo, ...]:
    """Require one audited wheel for every supported release platform."""

    infos = tuple(
        audit_native_binary(wheel, expected_version=expected_version)
        for wheel in wheels
    )
    by_platform = {info.platform: info for info in infos}
    if len(by_platform) != len(infos):
        raise NativeBinaryAuditError("native release set has duplicate platforms")
    actual = frozenset(by_platform)
    if actual != RELEASE_PLATFORMS:
        missing = sorted(RELEASE_PLATFORMS - actual)
        extra = sorted(actual - RELEASE_PLATFORMS)
        raise NativeBinaryAuditError(
            f"native release platform set mismatch; missing={missing}, extra={extra}"
        )
    return tuple(by_platform[name] for name in sorted(by_platform))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path, nargs="+")
    parser.add_argument("--expected-version")
    parser.add_argument("--require-release-platforms", action="store_true")
    arguments = parser.parse_args()
    wheels = tuple(path.resolve() for path in arguments.wheel)
    try:
        if arguments.require_release_platforms:
            if arguments.expected_version is None:
                parser.error("--require-release-platforms requires --expected-version")
            infos = audit_native_release_set(
                wheels, expected_version=arguments.expected_version
            )
        else:
            infos = tuple(
                audit_native_binary(path, expected_version=arguments.expected_version)
                for path in wheels
            )
    except (NativeBinaryAuditError, OSError) as exc:
        print(f"native binary audit: error: {exc}", file=sys.stderr)
        return 2
    platforms = ", ".join(info.platform for info in infos)
    print(f"native runtime binary audit passed: {platforms}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
