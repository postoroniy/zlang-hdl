"""Static VSIX acceptance; runnable with Python's standard library alone.

Usage: python tests/editor/test_vscode_package.py /path/to/zlang-hdl-0.1.0.vsix
The normal tests construct archives from source; they never run npm or VS Code.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import stat
import sys
import tempfile
import unittest
import warnings
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[2]
EXT = ROOT / "editors" / "vscode" / "zlang-hdl"
PINS = {
    "@vscode/vsce": "3.9.2",
    "vscode-oniguruma": "2.0.1",
    "vscode-textmate": "9.3.2",
}
ASSETS = {
    "extension/LICENSE.txt": ROOT / "LICENSE",
    "extension/NOTICE": ROOT / "NOTICE",
    "extension/changelog.md": EXT / "CHANGELOG.md",
    "extension/language-configuration.json": EXT / "language-configuration.json",
    "extension/package.json": EXT / "package.json",
    "extension/readme.md": EXT / "README.md",
    "extension/recommended-settings.json": EXT / "recommended-settings.json",
    "extension/snippets/zlang-hdl.json": EXT / "snippets" / "zlang-hdl.json",
    "extension/syntaxes/zlang.tmLanguage.json": EXT / "syntaxes" / "zlang.tmLanguage.json",
}
INVENTORY = frozenset(ASSETS) | {"extension.vsixmanifest", "[Content_Types].xml"}
RUNTIME_FIELDS = frozenset({
    "main", "browser", "activationEvents", "dependencies", "optionalDependencies",
    "extensionDependencies", "extensionPack", "enabledApiProposals",
})
VSIX_NS = "http://schemas.microsoft.com/developer/vsx-schema/2011"
CONTENT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
MANIFEST_ASSETS = {
    "Microsoft.VisualStudio.Code.Manifest": "extension/package.json",
    "Microsoft.VisualStudio.Services.Content.Details": "extension/readme.md",
    "Microsoft.VisualStudio.Services.Content.Changelog": "extension/changelog.md",
    "Microsoft.VisualStudio.Services.Content.License": "extension/LICENSE.txt",
}
CONTENT_TYPES = {
    ".json": "application/json", ".md": "text/markdown", ".txt": "text/plain",
    ".vsixmanifest": "text/xml",
}


class VSIXAuditError(ValueError):
    """The archive or its authoritative source failed the static-only gate."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise VSIXAuditError(message)


def _json(data: bytes) -> dict:
    def unique_pairs(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for name, value in pairs:
            _require(name not in result, f"duplicate JSON key: {name}")
            result[name] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VSIXAuditError(f"invalid JSON: {error}") from error
    _require(isinstance(value, dict), "JSON must be an object")
    return value


def validate_metadata(package: dict) -> None:
    _require(not RUNTIME_FIELDS.intersection(package), "runtime fields are forbidden")
    _require(
        (package.get("publisher"), package.get("name"), package.get("version"))
        == ("postoroniy", "zlang-hdl", "0.1.0"),
        "unexpected extension identity or version",
    )
    _require(package.get("license") == "Apache-2.0", "incorrect license metadata")
    _require(package.get("engines") == {"vscode": "^1.85.0"}, "incorrect VS Code engine")
    _require(package.get("devDependencies") == PINS, "dev dependencies must be pinned")
    _require(package.get("contributes") == {
        "languages": [{
            "id": "zlang-hdl", "aliases": ["ZLang HDL", "ZLang"],
            "extensions": [".zhl"], "configuration": "./language-configuration.json",
        }],
        "grammars": [{
            "language": "zlang-hdl", "scopeName": "source.zlang",
            "path": "./syntaxes/zlang.tmLanguage.json",
        }],
        "snippets": [{"language": "zlang-hdl", "path": "./snippets/zlang-hdl.json"}],
    }, "incorrect static language/grammar/snippet registration")
    _require(package.get("capabilities") == {
        "untrustedWorkspaces": {"supported": True}, "virtualWorkspaces": True,
    }, "incorrect static workspace capabilities")


def validate_lock(package: dict, lock: dict) -> None:
    _require(lock.get("lockfileVersion") == 3, "expected npm lockfile version 3")
    _require(
        (lock.get("name"), lock.get("version")) == ("zlang-hdl", "0.1.0"),
        "lockfile identity differs from extension",
    )
    packages = lock.get("packages")
    _require(isinstance(packages, dict) and "" in packages, "missing locked packages")
    root = packages[""]
    _require(isinstance(root, dict), "invalid lock root")
    for key in ("name", "version", "license", "devDependencies", "engines"):
        _require(root.get(key) == package.get(key), f"lock root differs for {key}")
    for name, version in PINS.items():
        node = packages.get(f"node_modules/{name}", {})
        _require(node.get("version") == version, f"unlocked direct dependency: {name}")
    for name, node in packages.items():
        if not name:
            continue
        _require(name.startswith("node_modules/"), f"unexpected lock package: {name}")
        _require(isinstance(node, dict) and node.get("dev") is True,
                 f"dependency is not dev-only: {name}")
        _require(not node.get("link"), f"linked dependency: {name}")
        _require(isinstance(node.get("version"), str) and bool(re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", node["version"]
        )), f"unversioned dependency: {name}")
        _require(isinstance(node.get("resolved"), str) and node["resolved"].startswith(
            "https://registry.npmjs.org/"
        ), f"non-registry dependency: {name}")
        _require(isinstance(node.get("integrity"), str) and bool(re.fullmatch(
            r"sha512-[A-Za-z0-9+/]+={0,2}", node["integrity"]
        )), f"dependency lacks integrity: {name}")


def _xml(data: bytes, label: str) -> ET.Element:
    _require(b"<!DOCTYPE" not in data.upper() and b"<!ENTITY" not in data.upper(),
             f"XML declarations are forbidden: {label}")
    try:
        return ET.fromstring(data)
    except ET.ParseError as error:
        raise VSIXAuditError(f"invalid XML in {label}: {error}") from error


def _validate_xml(members: dict[str, bytes], package: dict) -> None:
    root = _xml(members["extension.vsixmanifest"], "VSIX manifest")
    ns = {"v": VSIX_NS}
    _require(root.tag == f"{{{VSIX_NS}}}PackageManifest"
             and root.get("Version") == "2.0.0", "incorrect VSIX manifest root")
    identities = root.findall("v:Metadata/v:Identity", ns)
    _require(len(identities) == 1 and identities[0].attrib == {
        "Language": "en-US", "Id": "zlang-hdl", "Version": "0.1.0",
        "Publisher": "postoroniy",
    }, "incorrect VSIX XML identity")
    for name, expected in (
        ("License", "extension/LICENSE.txt"), ("DisplayName", package["displayName"]),
    ):
        values = root.findall(f"v:Metadata/v:{name}", ns)
        _require(len(values) == 1 and values[0].text == expected,
                 f"incorrect VSIX XML {name}")
    properties = root.findall("v:Metadata/v:Properties/v:Property", ns)
    by_id = {entry.get("Id"): entry.get("Value") for entry in properties}
    _require(len(properties) == len(by_id), "duplicate VSIX property")
    for name, expected in {
        "Engine": "^1.85.0", "ExtensionDependencies": "", "ExtensionPack": "",
        "EnabledApiProposals": "",
    }.items():
        _require(by_id.get(f"Microsoft.VisualStudio.Code.{name}") == expected,
                 f"incorrect VSIX XML {name}")
    targets = root.findall("v:Installation/v:InstallationTarget", ns)
    _require(len(targets) == 1 and targets[0].attrib == {"Id": "Microsoft.VisualStudio.Code"},
             "incorrect VSIX installation target")
    dependencies = root.findall("v:Dependencies", ns)
    _require(len(dependencies) == 1 and len(dependencies[0]) == 0,
             "VSIX dependencies are forbidden")
    assets = root.findall("v:Assets/v:Asset", ns)
    _require(len(assets) == len(MANIFEST_ASSETS) and {
        item.get("Type"): item.get("Path") for item in assets
    } == MANIFEST_ASSETS and all(item.get("Addressable") == "true" for item in assets),
        "incorrect VSIX asset declarations")
    types = _xml(members["[Content_Types].xml"], "content types")
    _require(types.tag == f"{{{CONTENT_NS}}}Types" and len(types) == len(CONTENT_TYPES)
             and all(item.tag == f"{{{CONTENT_NS}}}Default" for item in types)
             and {item.get("Extension"): item.get("ContentType") for item in types}
             == CONTENT_TYPES, "incorrect VSIX content types")


def audit_vsix(path: Path) -> dict:
    """Audit without extraction, installation, credentials, or network access."""
    _require(path.stat().st_size <= 2_000_000, "VSIX exceeds static package size bound")
    with ZipFile(path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        _require(len(names) == len(set(names)), "duplicate ZIP entry")
        for entry in entries:
            name = entry.filename
            _require(name == entry.orig_filename and "\\" not in name and "\x00" not in name
                     and not re.match(r"^[A-Za-z]:", name)
                     and all(part not in {"", ".", ".."} for part in name.split("/")),
                     f"unsafe archive path: {name!r}")
            _require(not entry.is_dir() and stat.S_IFMT(entry.external_attr >> 16)
                     in {0, stat.S_IFREG}, f"non-regular ZIP entry: {name}")
            _require(not entry.flag_bits & 1, f"encrypted ZIP entry: {name}")
            _require(entry.file_size <= 1_000_000, f"oversized ZIP entry: {name}")
        _require(set(names) == INVENTORY, "unexpected VSIX inventory")
        _require(sum(entry.file_size for entry in entries) <= 2_000_000,
                 "expanded VSIX exceeds size bound")
        members = {name: archive.read(name) for name in names}

    package = _json(members["extension/package.json"])
    validate_metadata(package)
    source_package = _json((EXT / "package.json").read_bytes())
    validate_metadata(source_package)
    validate_lock(source_package, _json((EXT / "package-lock.json").read_bytes()))
    grammar = _json(members["extension/syntaxes/zlang.tmLanguage.json"])
    _require(grammar.get("scopeName") == "source.zlang", "incorrect TextMate scope")
    _validate_xml(members, package)
    for name, source in ASSETS.items():
        _require(members[name] == source.read_bytes(), f"packaged bytes differ from source: {name}")
    return {
        "extension_id": "postoroniy.zlang-hdl", "version": "0.1.0",
        "file_count": len(members), "files": sorted(members),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _source_members() -> dict[str, bytes]:
    """Source-derived test fixture, independent of npm and a prebuilt archive."""
    members = {name: source.read_bytes() for name, source in ASSETS.items()}
    manifest = ET.Element(f"{{{VSIX_NS}}}PackageManifest", Version="2.0.0")
    metadata = ET.SubElement(manifest, f"{{{VSIX_NS}}}Metadata")
    ET.SubElement(metadata, f"{{{VSIX_NS}}}Identity", Language="en-US", Id="zlang-hdl",
                  Version="0.1.0", Publisher="postoroniy")
    ET.SubElement(metadata, f"{{{VSIX_NS}}}DisplayName").text = "ZLang HDL"
    ET.SubElement(metadata, f"{{{VSIX_NS}}}License").text = "extension/LICENSE.txt"
    properties = ET.SubElement(metadata, f"{{{VSIX_NS}}}Properties")
    for name, value in {
        "Engine": "^1.85.0", "ExtensionDependencies": "", "ExtensionPack": "",
        "EnabledApiProposals": "",
    }.items():
        ET.SubElement(properties, f"{{{VSIX_NS}}}Property",
                      Id=f"Microsoft.VisualStudio.Code.{name}", Value=value)
    installation = ET.SubElement(manifest, f"{{{VSIX_NS}}}Installation")
    ET.SubElement(installation, f"{{{VSIX_NS}}}InstallationTarget", Id="Microsoft.VisualStudio.Code")
    ET.SubElement(manifest, f"{{{VSIX_NS}}}Dependencies")
    assets = ET.SubElement(manifest, f"{{{VSIX_NS}}}Assets")
    for kind, path in MANIFEST_ASSETS.items():
        ET.SubElement(assets, f"{{{VSIX_NS}}}Asset", Type=kind, Path=path, Addressable="true")
    members["extension.vsixmanifest"] = ET.tostring(manifest)
    types = ET.Element(f"{{{CONTENT_NS}}}Types")
    for extension, content_type in CONTENT_TYPES.items():
        ET.SubElement(types, f"{{{CONTENT_NS}}}Default", Extension=extension, ContentType=content_type)
    members["[Content_Types].xml"] = ET.tostring(types)
    return members


class VSCodePackageTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="zlang-vsix-audit-test-")
        self.addCleanup(temporary.cleanup)
        self.archive_path = Path(temporary.name) / "test.vsix"

    def archive(self, members: dict[str, bytes] | list[tuple[str | ZipInfo, bytes]]) -> Path:
        entries = list(members.items()) if isinstance(members, dict) else members
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)  # Deliberate duplicate-entry test.
            with ZipFile(self.archive_path, "w") as archive:
                for name, data in entries:
                    archive.writestr(name, data)
        return self.archive_path

    def test_source_metadata_and_lock_are_static_and_pinned(self) -> None:
        package = _json((EXT / "package.json").read_bytes())
        lock = _json((EXT / "package-lock.json").read_bytes())
        validate_metadata(package)
        validate_lock(package, lock)
        self.assertEqual(len(INVENTORY), 11)
        for key, value in (("version", "3.9.1"), ("dev", False), ("integrity", "")):
            with self.subTest(key=key):
                broken = copy.deepcopy(lock)
                broken["packages"]["node_modules/@vscode/vsce"][key] = value
                with self.assertRaises(VSIXAuditError):
                    validate_lock(package, broken)

    def test_source_derived_archive_is_accepted_and_hashed(self) -> None:
        path = self.archive(_source_members())
        report = audit_vsix(path)
        self.assertEqual(report["extension_id"], "postoroniy.zlang-hdl")
        self.assertEqual(report["version"], "0.1.0")
        self.assertEqual(report["file_count"], 11)
        self.assertEqual(report["files"], sorted(INVENTORY))
        self.assertEqual(report["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_archive_rejects_every_runtime_metadata_field(self) -> None:
        for field in sorted(RUNTIME_FIELDS):
            with self.subTest(field=field):
                members = _source_members()
                package = _json(members["extension/package.json"])
                package[field] = "runtime.js"
                members["extension/package.json"] = json.dumps(package).encode()
                with self.assertRaisesRegex(VSIXAuditError, "runtime fields"):
                    audit_vsix(self.archive(members))

    def test_archive_rejects_changed_identity_registration_and_scope(self) -> None:
        mutations = (
            ("publisher", "someone-else"), ("version", "0.1.1"),
            ("license", "MIT"), ("contributes", {}),
        )
        for key, value in mutations:
            with self.subTest(key=key):
                members = _source_members()
                package = _json(members["extension/package.json"])
                package[key] = value
                members["extension/package.json"] = json.dumps(package).encode()
                with self.assertRaises(VSIXAuditError):
                    audit_vsix(self.archive(members))
        members = _source_members()
        grammar = _json(members["extension/syntaxes/zlang.tmLanguage.json"])
        grammar["scopeName"] = "source.other"
        members["extension/syntaxes/zlang.tmLanguage.json"] = json.dumps(grammar).encode()
        with self.assertRaisesRegex(VSIXAuditError, "TextMate scope"):
            audit_vsix(self.archive(members))

    def test_archive_rejects_license_notice_and_source_byte_drift(self) -> None:
        for name in ASSETS:
            with self.subTest(asset=name):
                members = _source_members()
                members[name] += b"\n"
                with self.assertRaisesRegex(VSIXAuditError, "bytes differ from source"):
                    audit_vsix(self.archive(members))

    def test_archive_rejects_missing_extra_duplicate_and_unsafe_entries(self) -> None:
        members = _source_members()
        missing = dict(members)
        del missing["extension/LICENSE.txt"]
        with self.assertRaisesRegex(VSIXAuditError, "inventory"):
            audit_vsix(self.archive(missing))
        for name in (
            "extension/node_modules/runtime.js", "extension/.env", "extension/test.js",
            "../escape", "/absolute", "extension/../escape", "extension\\escape", "C:/escape",
        ):
            with self.subTest(name=name):
                with self.assertRaises(VSIXAuditError):
                    audit_vsix(self.archive(list(members.items()) + [(name, b"unexpected")]))
        with self.assertRaisesRegex(VSIXAuditError, "duplicate ZIP"):
            audit_vsix(self.archive(list(members.items()) + [("extension/NOTICE", b"duplicate")]))
        symlink = ZipInfo("extension/LICENSE.txt")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(VSIXAuditError, "non-regular"):
            audit_vsix(self.archive(list(missing.items()) + [(symlink, b"../../LICENSE")]))

    def test_archive_rejects_corrupt_xml_identity_license_and_entities(self) -> None:
        for old, new, message in (
            (b'Publisher="postoroniy"', b'Publisher="other"', "XML identity"),
            (b'Version="0.1.0"', b'Version="0.1.1"', "XML identity"),
            (b"extension/LICENSE.txt", b"extension/NOTICE", "XML License"),
        ):
            with self.subTest(replacement=new):
                members = _source_members()
                members["extension.vsixmanifest"] = members["extension.vsixmanifest"].replace(old, new)
                with self.assertRaisesRegex(VSIXAuditError, message):
                    audit_vsix(self.archive(members))
        members = _source_members()
        members["extension.vsixmanifest"] = b'<!DOCTYPE x [<!ENTITY y "z">]><x/>'
        with self.assertRaisesRegex(VSIXAuditError, "XML declarations"):
            audit_vsix(self.archive(members))


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("Usage: python tests/editor/test_vscode_package.py PATH.vsix", file=sys.stderr)
        return 2
    try:
        report = audit_vsix(Path(arguments[0]))
    except (VSIXAuditError, BadZipFile, OSError, RuntimeError) as error:
        print(f"VSIX audit failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
