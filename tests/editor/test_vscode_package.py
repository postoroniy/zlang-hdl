"""Audited VSIX acceptance for the Community ZLang HDL extension."""

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
PINS = {"@vscode/vsce": "3.9.2", "vscode-oniguruma": "2.0.1", "vscode-textmate": "9.3.2"}
STATIC_ASSETS = {
    "extension/LICENSE.txt": ROOT / "LICENSE",
    "extension/NOTICE": ROOT / "NOTICE",
    "extension/changelog.md": EXT / "CHANGELOG.md",
    "extension/package.json": EXT / "package.json",
    "extension/language-configuration.json": EXT / "language-configuration.json",
    "extension/readme.md": EXT / "README.md",
    "extension/recommended-settings.json": EXT / "recommended-settings.json",
    "extension/snippets/zlang-hdl.json": EXT / "snippets" / "zlang-hdl.json",
    "extension/syntaxes/zlang.tmLanguage.json": EXT / "syntaxes" / "zlang.tmLanguage.json",
    "extension/extension.js": EXT / "extension.js",
}
REQUIRED_RUNTIME = {
    "extension/vendor/node_modules/vscode-languageclient/lib/node/main.js",
    "extension/vendor/node_modules/vscode-jsonrpc/lib/node/main.js",
    "extension/vendor/node_modules/vscode-languageserver-protocol/lib/node/main.js",
    "extension/vendor/node_modules/vscode-languageserver-types/lib/umd/main.js",
    "extension/vendor/node_modules/semver/index.js",
}
VSIX_NS = "http://schemas.microsoft.com/developer/vsx-schema/2011"
CONTENT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
MANIFEST_ASSETS = {
    "Microsoft.VisualStudio.Code.Manifest": "extension/package.json",
    "Microsoft.VisualStudio.Services.Content.Details": "extension/readme.md",
    "Microsoft.VisualStudio.Services.Content.Changelog": "extension/changelog.md",
    "Microsoft.VisualStudio.Services.Content.License": "extension/LICENSE.txt",
}
CONTENT_TYPES = {
    ".bnf": "application/octet-stream", ".cmd": "application/octet-stream",
    ".js": "application/javascript", ".json": "application/json",
    ".md": "text/markdown", ".sh": "application/x-sh", ".ts": "video/mp2t",
    ".txt": "text/plain", ".vsixmanifest": "text/xml", ".yml": "text/yaml",
}


class VSIXAuditError(ValueError):
    """The archive or its authoritative source failed the package gate."""


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


def _contributes() -> dict:
    return {
        "languages": [{
            "id": "zlang-hdl", "aliases": ["ZLang HDL", "ZLang"],
            "extensions": [".zhl"], "configuration": "./language-configuration.json",
        }],
        "grammars": [{
            "language": "zlang-hdl", "scopeName": "source.zlang",
            "path": "./syntaxes/zlang.tmLanguage.json",
        }],
        "snippets": [{"language": "zlang-hdl", "path": "./snippets/zlang-hdl.json"}],
        "configuration": {
            "title": "ZLang HDL",
            "properties": {
                "zlang.lsp.path": {
                    "type": "string", "default": "",
                    "description": (
                        "Direct path or PATH command for the Community zlang-lsp "
                        "executable. Use ${workspaceFolder}/.venv/bin/zlang-lsp "
                        "for a repository checkout."
                    ),
                },
            },
        },
    }


def validate_metadata(package: dict, *, packaged: bool = False) -> None:
    _require(
        (package.get("publisher"), package.get("name"), package.get("version"))
        == ("postoroniy", "zlang-hdl", "0.1.0"),
        "unexpected extension identity or version",
    )
    _require(package.get("license") == "Apache-2.0", "incorrect license metadata")
    _require(package.get("engines") == {"vscode": "^1.85.0"}, "incorrect VS Code engine")
    _require(package.get("main") == "./extension.js", "missing extension entrypoint")
    _require("activationEvents" not in package, "language activation is contributed automatically")
    _require(package.get("dependencies") == {"vscode-languageclient": "^9.0.1"},
             "incorrect runtime dependency")
    _require(package.get("contributes") == _contributes(), "incorrect language registration")
    _require(package.get("capabilities") == {
        "untrustedWorkspaces": {
            "supported": False,
            "description": (
                "The extension starts the configured local zlang-lsp executable "
                "only in a trusted workspace."
            ),
        },
        "virtualWorkspaces": False,
    }, "incorrect workspace capabilities")
    if packaged:
        _require("devDependencies" not in package, "development dependencies shipped")
        _require("files" not in package, "source-only files manifest shipped")
        _require("scripts" not in package, "development scripts shipped")
    else:
        _require(package.get("devDependencies") == PINS, "dev dependencies must be pinned")
        _require("extension.js" in package.get("files", []), "entrypoint missing from source files")


def validate_lock(package: dict, lock: dict) -> None:
    _require(lock.get("lockfileVersion") == 3, "expected npm lockfile version 3")
    _require((lock.get("name"), lock.get("version")) == ("zlang-hdl", "0.1.0"),
             "lockfile identity differs from extension")
    packages = lock.get("packages")
    _require(isinstance(packages, dict) and "" in packages, "missing locked packages")
    root = packages[""]
    _require(isinstance(root, dict), "invalid lock root")
    for key in ("name", "version", "license", "devDependencies", "dependencies", "engines"):
        _require(root.get(key) == package.get(key), f"lock root differs for {key}")
    for name, version in PINS.items():
        node = packages.get(f"node_modules/{name}", {})
        _require(node.get("version") == version, f"unlocked direct dependency: {name}")
    runtime_prefixes = (
        "node_modules/vscode-languageclient", "node_modules/vscode-jsonrpc",
        "node_modules/vscode-languageserver-protocol", "node_modules/vscode-languageserver-types",
        "node_modules/semver",
    )
    for name, node in packages.items():
        if not name:
            continue
        _require(name.startswith("node_modules/") and isinstance(node, dict) and not node.get("link"),
                 f"invalid package record: {name}")
        if name.startswith(runtime_prefixes):
            _require(node.get("dev") is not True, f"runtime dependency is dev-only: {name}")
        else:
            _require(node.get("dev") is True, f"unexpected non-runtime package: {name}")
        _require(isinstance(node.get("version"), str) and bool(re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", node["version"]
        )), f"unversioned dependency: {name}")
        _require(isinstance(node.get("resolved"), str)
                 and node["resolved"].startswith("https://registry.npmjs.org/"),
                 f"non-registry dependency: {name}")
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
        "Language": "en-US", "Id": "zlang-hdl", "Version": "0.1.0", "Publisher": "postoroniy",
    }, "incorrect VSIX XML identity")
    for name, expected in (("License", "extension/LICENSE.txt"),
                           ("DisplayName", package["displayName"])):
        values = root.findall(f"v:Metadata/v:{name}", ns)
        _require(len(values) == 1 and values[0].text == expected, f"incorrect VSIX XML {name}")
    properties = root.findall("v:Metadata/v:Properties/v:Property", ns)
    by_id = {entry.get("Id"): entry.get("Value") for entry in properties}
    _require(len(properties) == len(by_id), "duplicate VSIX property")
    for name, expected in {"Engine": "^1.85.0", "ExtensionDependencies": "",
                           "ExtensionPack": "", "EnabledApiProposals": ""}.items():
        _require(by_id.get(f"Microsoft.VisualStudio.Code.{name}") == expected,
                 f"incorrect VSIX XML {name}")
    targets = root.findall("v:Installation/v:InstallationTarget", ns)
    _require(len(targets) == 1 and targets[0].attrib == {"Id": "Microsoft.VisualStudio.Code"},
             "incorrect VSIX installation target")
    dependencies = root.findall("v:Dependencies", ns)
    _require(len(dependencies) == 1 and len(dependencies[0]) == 0,
             "extension dependencies must remain empty")
    assets = root.findall("v:Assets/v:Asset", ns)
    _require(len(assets) == len(MANIFEST_ASSETS)
             and {item.get("Type"): item.get("Path") for item in assets} == MANIFEST_ASSETS
             and all(item.get("Addressable") == "true" for item in assets),
             "incorrect VSIX asset declarations")
    types = _xml(members["[Content_Types].xml"], "content types")
    _require(types.tag == f"{{{CONTENT_NS}}}Types"
             and {item.get("Extension"): item.get("ContentType") for item in types}
             == CONTENT_TYPES, "incorrect VSIX content types")


def audit_vsix(path: Path) -> dict:
    """Audit without extraction, installation, credentials, or network access."""
    _require(path.stat().st_size <= 2_000_000, "VSIX exceeds package size bound")
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
        _require(set(STATIC_ASSETS) <= set(names), "missing static extension asset")
        _require(REQUIRED_RUNTIME <= set(names), "missing language-client runtime asset")
        _require(not any(name.startswith("extension/node_modules/") for name in names),
                 "unvetted node_modules tree shipped")
        allowed = set(STATIC_ASSETS) | {"extension.vsixmanifest", "[Content_Types].xml"}
        _require(all(name in allowed or name.startswith("extension/vendor/node_modules/")
                     for name in names), "unexpected extension payload")
        _require(sum(entry.file_size for entry in entries) <= 2_000_000,
                 "expanded VSIX exceeds package bound")
        members = {name: archive.read(name) for name in names}

    package = _json(members["extension/package.json"])
    validate_metadata(package, packaged=True)
    source_package = _json((EXT / "package.json").read_bytes())
    validate_metadata(source_package)
    validate_lock(source_package, _json((EXT / "package-lock.json").read_bytes()))
    grammar = _json(members["extension/syntaxes/zlang.tmLanguage.json"])
    _require(grammar.get("scopeName") == "source.zlang", "incorrect TextMate scope")
    _validate_xml(members, package)
    for name, source in STATIC_ASSETS.items():
        if name == "extension/package.json":
            continue
        _require(members[name] == source.read_bytes(), f"packaged bytes differ from source: {name}")
    text = b"\n".join(members[name] for name in names if name.endswith((".js", ".json", ".md")))
    _require(b"/home/slava" not in text and b"zlang-agent" not in text
             and b"Ollama" not in text and b"CUDA" not in text,
             "private path or Enterprise/AI reference shipped")
    return {
        "extension_id": "postoroniy.zlang-hdl", "version": "0.1.0",
        "file_count": len(members), "files": sorted(members),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _source_members() -> dict[str, bytes]:
    """Source-derived fixture with a minimal production runtime tree."""
    members = {name: source.read_bytes() for name, source in STATIC_ASSETS.items()
               if name != "extension/package.json"}
    package = _json((EXT / "package.json").read_bytes())
    for field in ("files", "devDependencies", "scripts"):
        package.pop(field, None)
    members["extension/package.json"] = json.dumps(package, indent=2).encode() + b"\n"
    for name in REQUIRED_RUNTIME:
        members[name] = b"module.exports = {};\n"
    manifest = ET.Element(f"{{{VSIX_NS}}}PackageManifest", Version="2.0.0")
    metadata = ET.SubElement(manifest, f"{{{VSIX_NS}}}Metadata")
    ET.SubElement(metadata, f"{{{VSIX_NS}}}Identity", Language="en-US", Id="zlang-hdl",
                  Version="0.1.0", Publisher="postoroniy")
    ET.SubElement(metadata, f"{{{VSIX_NS}}}DisplayName").text = "ZLang HDL"
    ET.SubElement(metadata, f"{{{VSIX_NS}}}License").text = "extension/LICENSE.txt"
    properties = ET.SubElement(metadata, f"{{{VSIX_NS}}}Properties")
    for name, value in {"Engine": "^1.85.0", "ExtensionDependencies": "",
                        "ExtensionPack": "", "EnabledApiProposals": ""}.items():
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
            warnings.simplefilter("ignore", UserWarning)
            with ZipFile(self.archive_path, "w") as archive:
                for name, data in entries:
                    archive.writestr(name, data)
        return self.archive_path

    def test_source_metadata_and_lock_are_runtime_pinned(self) -> None:
        package = _json((EXT / "package.json").read_bytes())
        lock = _json((EXT / "package-lock.json").read_bytes())
        validate_metadata(package)
        validate_lock(package, lock)
        self.assertIn("vscode-languageclient", package["dependencies"])
        broken = copy.deepcopy(lock)
        broken["packages"]["node_modules/@vscode/vsce"]["version"] = "3.9.1"
        with self.assertRaises(VSIXAuditError):
            validate_lock(package, broken)

    def test_installation_guide_uses_the_registered_lsp_setting(self) -> None:
        guide = (ROOT / "docs" / "installing-toolchain.md").read_text(
            encoding="utf-8"
        )
        properties = _contributes()["configuration"]["properties"]
        self.assertIn('"zlang.lsp.path"', guide)
        self.assertIn("zlang.lsp.path", properties)
        self.assertNotIn("zlang.server.path", guide)

    def test_installation_guide_has_managed_python_wsl_and_verified_tools(self) -> None:
        guide = (ROOT / "docs" / "installing-toolchain.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("uv tool install --python 3.12", guide)
        self.assertIn("uv venv --python 3.12", guide)
        self.assertIn("## Windows through WSL2", guide)
        self.assertIn("**verified versions**", guide)
        for version in ("Verilator | 5.052", "Yosys | 0.69", "Z3 | 4.8.12"):
            self.assertIn(version, guide)
        self.assertNotIn("python3.12", guide)

    def test_source_derived_archive_is_accepted_and_hashed(self) -> None:
        path = self.archive(_source_members())
        report = audit_vsix(path)
        self.assertEqual(report["extension_id"], "postoroniy.zlang-hdl")
        self.assertGreaterEqual(report["file_count"], len(STATIC_ASSETS) + len(REQUIRED_RUNTIME) + 2)
        self.assertEqual(report["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_archive_rejects_changed_identity_registration_and_scope(self) -> None:
        for key, value in (("publisher", "someone-else"), ("version", "0.1.1"),
                           ("license", "MIT"), ("contributes", {})):
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
        for name in STATIC_ASSETS:
            if name == "extension/package.json":
                continue
            with self.subTest(asset=name):
                members = _source_members()
                members[name] += b"\n"
                with self.assertRaisesRegex(VSIXAuditError, "bytes differ from source"):
                    audit_vsix(self.archive(members))

    def test_archive_rejects_missing_extra_duplicate_and_unsafe_entries(self) -> None:
        members = _source_members()
        missing = dict(members)
        del missing["extension/LICENSE.txt"]
        with self.assertRaisesRegex(VSIXAuditError, "missing static"):
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
        for old, new, message in ((b'Publisher="postoroniy"', b'Publisher="other"', "XML identity"),
                                  (b'Version="0.1.0"', b'Version="0.1.1"', "XML identity"),
                                  (b"extension/LICENSE.txt", b"extension/NOTICE", "XML License")):
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
