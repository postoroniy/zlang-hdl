import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zlang.backend.manifest import BackendArtifact, publish_artifact
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.stdlib import (
    available_stdlib_modules,
    load_stdlib_source,
    resolve_stdlib,
    track_resolved_stdlib_source_paths,
)


class StdlibResolverTests(unittest.TestCase):
    def test_discovers_every_recursive_library_family(self):
        available = available_stdlib_modules()
        self.assertTrue({
            "std.math.complex",
            "std.math.complex_fixed_18_16",
            "std.stream.core",
            "std.stream.serialization",
            "std.stream.complex_fixed",
            "std.dsp.fft",
            "std.storage",
            "std.storage.core",
            "std.coding",
            "std.coding.core",
        }.issubset(available))
        for module in available:
            with self.subTest(module=module):
                self.assertEqual(load_stdlib_source(module).path, module)

    def test_complex_core_has_no_stream_or_bus_dependency(self):
        core = resolve_stdlib(("std.math.complex",))
        self.assertEqual(tuple(item.path for item in core), ("std.math.complex",))
        fixed = resolve_stdlib(("std.math.fixed",))
        self.assertEqual(tuple(item.path for item in fixed), ("std.math.fixed",))
        profile = resolve_stdlib(("std.stream.complex_fixed",))
        self.assertEqual(
            tuple(item.path for item in profile),
            (
                "std.math.complex",
                "std.stream.complex_fixed",
            ),
        )
        self.assertEqual(
            tuple(item.path for item in resolve_stdlib(("std.storage",))),
            ("std.storage.core", "std.storage"),
        )
        self.assertEqual(
            tuple(item.path for item in resolve_stdlib(("std.coding",))),
            ("std.coding.core", "std.coding"),
        )

    def test_discovers_modules_and_resolves_transitive_dependencies(self):
        self.assertIn("std.bus.reg", available_stdlib_modules())
        resolved = resolve_stdlib(("std.bus.axi_lite",))
        self.assertEqual([item.path for item in resolved], ["std.bus.reg", "std.bus.axi_lite"])
        module = analyze(parse(
            "import std.bus.axi_lite module Top { clock clk reset rst "
            "interface axi:AXI4Lite<32,32>.slave @clk }"
        ))
        self.assertEqual(module.library_imports, ("std.bus.axi_lite", "std.bus.reg"))
        self.assertEqual(tuple(path for path, _ in module.library_dependencies),
                         ("std.bus.reg", "std.bus.axi_lite"))

    def test_cycle_is_reported_with_dependency_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "test").mkdir()
            (root / "test" / "a.zl").write_text("import std.test.b module A {}")
            (root / "test" / "b.zl").write_text("import std.test.a module B {}")
            with patch("zlang.stdlib._ROOTS", (root,)):
                with self.assertRaisesRegex(ValueError, r"std\.test\.a -> std\.test\.b -> std\.test\.a"):
                    resolve_stdlib(("std.test.a",))

    def test_identical_duplicate_roots_are_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = base / "first"
            second = base / "second"
            (first / "test").mkdir(parents=True)
            (second / "test").mkdir(parents=True)
            source = "module Shared { in x:bit out y:bit y=x }\n"
            (first / "test" / "shared.zl").write_text(source)
            (second / "test" / "shared.zl").write_text(source)
            with patch("zlang.stdlib._ROOTS", (first, second)):
                with track_resolved_stdlib_source_paths() as consulted:
                    item = load_stdlib_source("std.test.shared")
                self.assertEqual(item.source_path, (first / "test" / "shared.zl").resolve())
                self.assertEqual(
                    consulted,
                    {
                        (first / "test" / "shared.zl").resolve(),
                        (second / "test" / "shared.zl").resolve(),
                    },
                )
                self.assertEqual(available_stdlib_modules(), ("std.test.shared",))

    def test_conflicting_duplicate_roots_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = base / "checkout"
            second = base / "installed"
            (first / "test").mkdir(parents=True)
            (second / "test").mkdir(parents=True)
            (first / "test" / "shared.zl").write_text("module Checkout {}\n")
            (second / "test" / "shared.zl").write_text("module Installed {}\n")
            with patch("zlang.stdlib._ROOTS", (first, second)):
                with self.assertRaisesRegex(
                    ValueError,
                    r"conflicting duplicate standard library module 'std\.test\.shared'.*checkout.*installed",
                ):
                    load_stdlib_source("std.test.shared")
                with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
                    available_stdlib_modules()

    def test_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "root"
            outside = base / "outside.zl"
            (root / "test").mkdir(parents=True)
            outside.write_text("module Outside {}\n")
            (root / "test" / "outside.zl").symlink_to(outside)
            with patch("zlang.stdlib._ROOTS", (root,)):
                with self.assertRaisesRegex(ValueError, "escapes root"):
                    load_stdlib_source("std.test.outside")

    def test_invalid_and_unknown_paths_have_distinct_diagnostics(self):
        with self.assertRaisesRegex(ValueError, "invalid standard library module path"):
            resolve_stdlib(("std._private.module",))
        with self.assertRaisesRegex(ValueError, "unknown standard library module"):
            resolve_stdlib(("std.bus.not_present",))
        with self.assertRaisesRegex(ValueError, "external imports are unsupported"):
            resolve_stdlib(("vendor.bus",))

    def test_semantic_duplicate_direct_import_remains_an_error(self):
        with self.assertRaisesRegex(SemanticError, "duplicate import 'std.bus.reg'"):
            analyze(parse("import std.bus.reg import std.bus.reg module Top {}"))

    def test_manifest_round_trip_preserves_dependency_hashes(self):
        module = analyze(parse("import std.bus.reg module Top {}"))
        artifact = publish_artifact(module, "module Top; endmodule", backend="systemverilog",
                                    selected_ir_identity="selected")
        restored = BackendArtifact.from_json(artifact.to_json())
        self.assertEqual(restored.library_dependencies, module.library_dependencies)


if __name__ == "__main__":
    unittest.main()
