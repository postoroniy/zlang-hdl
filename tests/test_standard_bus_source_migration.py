import hashlib
import unittest
from pathlib import Path

from zlang.parser import ParseError, parse
from zlang.semantic import analyze
from zlang.stdlib import stdlib_source


class StandardBusSourceMigrationTests(unittest.TestCase):
    def test_import_loads_real_source_with_identity_and_hash(self):
        source_module, identity, digest = stdlib_source("std.bus.reg")
        self.assertEqual(identity, "std.bus.reg")
        self.assertEqual(source_module.source_identity, identity)
        self.assertEqual(
            digest,
            hashlib.sha256(
                Path("stdlib/bus/reg.zl").read_bytes()
            ).hexdigest(),
        )
        ir = analyze(parse(
            "import std.bus.reg module Top { clock clk reset rst "
            "inst csr:RegBusCSRTarget<32,32> }"
        ))
        self.assertEqual(ir.protocol_schemas[0].library_path, "std.bus.reg")
        self.assertEqual(ir.children[0].source_identity, "std.bus.reg")
        self.assertTrue(ir.children[0].source_hash)

    def test_parameterized_endpoint_is_parsed(self):
        module = parse(
            "protocol Tiny<AW=32> { role a role b "
            "channel x:rv<u8> a -> b } "
            "module Top { interface p : Tiny<AW=32>.a }"
        )
        self.assertEqual(module.aggregate_interfaces[0].protocol, "Tiny")


if __name__ == "__main__":
    unittest.main()
