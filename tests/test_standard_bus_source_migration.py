import hashlib
import unittest
from pathlib import Path

from zlang.parser import parse
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
                Path("stdlib/bus/reg.zhl").read_bytes()
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

    def test_registered_csr_target_is_an_ordinary_imported_module(self):
        ir = analyze(parse(
            "import std.bus.reg module Top { clock clk reset rst "
            "target:RegisteredCSRTarget<32,32> { csr_rdata=0 csr_ready=0 } }"
        ))
        self.assertEqual(ir.children[0].name, "RegisteredCSRTarget")
        self.assertEqual(ir.children[0].source_identity, "std.bus.reg")

    def test_snapshot_is_protocol_neutral_typed_state(self):
        ir = analyze(parse(
            "import std.storage.core fn reset_bit()->bit { 0 == 0 } "
            "module Top { clock clk reset rst "
            "inst snapshot:StorageSnapshot<T=bit,reset_value=fn reset_bit> { "
            "input=(0 == 0) capture=(0 == 0) } }"
        ))
        child = ir.children[0]
        self.assertEqual(child.name, "StorageSnapshot")
        self.assertEqual([register.name for register in child.registers], ["captured"])

    def test_snapshot_input_may_use_a_typed_immutable_parent_local(self):
        ir = analyze(parse(
            "import std.storage.core "
            "struct Command { value:u8 } "
            "fn empty_command()->Command { Command { value=0 } } "
            "module Top { clock clk reset rst in x:u8 in fire:bit out y:u8 "
            "payload:Command=Command { value=x } "
            "inst snapshot:StorageSnapshot<T=Command,reset_value=fn empty_command> { "
            "input=payload capture=fire } y=snapshot.output.value }"
        ))
        self.assertEqual([item.name for item in ir.locals], ["payload"])
        input_binding = next(
            item for item in ir.instance_bindings
            if item.instance == "snapshot" and item.port == "input"
        )
        self.assertEqual(input_binding.expression.type, ir.locals[0].type)

    def test_snapshot_input_still_rejects_a_genuinely_unknown_name(self):
        with self.assertRaisesRegex(Exception, "unknown input 'missing_payload'"):
            analyze(parse(
                "import std.storage.core "
                "struct Command { value:u8 } "
                "fn empty_command()->Command { Command { value=0 } } "
                "module Top { clock clk reset rst in fire:bit "
                "inst snapshot:StorageSnapshot<T=Command,reset_value=fn empty_command> { "
                "input=missing_payload capture=fire } }"
            ))


if __name__ == "__main__":
    unittest.main()
