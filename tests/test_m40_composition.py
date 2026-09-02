import unittest
from dataclasses import replace
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

from zlang import compile_source
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.ir import expressions as ir_expr
from zlang.backend.clash import ClashEmissionError, emit, emit_artifact
from zlang.opt.lowering import lower
from zlang.toolchain import find_clash_executable, generate_verilog, lint_with_verilator


class CompositionM40Tests(unittest.TestCase):
    def test_instance_arrays_emit_after_compile_time_indexed_binding(self):
        module = analyze(parse(
            "module Child { in x:u8 out y:u8 y=x } "
            "module Top { in values:vec<2,u8> out y:u8 inst c[2]:Child "
            "generate(i in 0..2) { c[i].x=values[i] } y=c[1].y }"
        ))
        text = emit(module)
        self.assertIn("zlang_instance_c_0_", text)
        self.assertIn("zlang_instance_c_1_", text)

    def test_locals_are_ordered_and_typed(self):
        module = analyze(parse("module A { in a:u8 out y:u8 x:u8 = a y = x }"))
        self.assertEqual([item.name for item in module.locals], ["x"])
        self.assertEqual(module.assignments[0].expression.type.width, 8)

    def test_parameterized_width(self):
        module = analyze(parse(
            "module A<AW=16> { in a:uint<AW> out y:uint<AW> y = a }"
        ))
        self.assertEqual(module.ports[0].type.width, 16)
        self.assertEqual(module.parameters[0][0], "AW")

    def test_runtime_index_is_checked(self):
        module = analyze(parse(
            "module A { in v:vec<4,u8> in i:u2 out y:u8 y = v[i] }"
        ))
        self.assertIsInstance(module.assignments[0].expression, ir_expr.RuntimeIndex)

    def test_runtime_index_range_rejected(self):
        with self.assertRaises(SemanticError):
            analyze(parse("module A { in v:vec<4,u8> in i:u3 out y:u8 y = v[i] }"))

    def test_struct_constructor(self):
        module = analyze(parse(
            "struct P { x:u8 y:u8 } module A { in a:u8 out o:P o = P { x=a y=0 } }"
        ))
        self.assertIsInstance(module.assignments[0].expression, ir_expr.StructConstruct)

    def test_multiple_modules_and_instance_identity(self):
        module = analyze(parse(
            "module Child { in a:u8 out y:u8 y=a } "
            "module Top { in a:u8 out y:u8 inst c:Child c.a=a y=c.y }"
        ))
        self.assertEqual(module.name, "Top")
        self.assertEqual(module.instances[0].module, "Child")

    def test_instance_bindings_and_output_refs_are_typed(self):
        module = analyze(parse(
            "module Child { in a:u8 out y:u8 y=a } "
            "module Top { in a:u8 out y:u8 inst c:Child c.a=a y=c.y }"
        ))
        self.assertEqual(module.instance_bindings[0].port, "a")
        self.assertIsInstance(module.assignments[0].expression, ir_expr.InstanceOutputRef)

    def test_concise_instance_bindings_and_specialization(self):
        source = (
            "module Child<AW=8> { in x:uint<AW> out y:uint<AW> y=x } "
            "module Top<AW=8> { in x:uint<AW> out y:uint<AW> "
            "inst c:Child<AW> { x } y=c.y }"
        )
        module = analyze(parse(source))
        self.assertEqual(module.instances[0].specializations[0].name, "AW")
        self.assertEqual(module.instances[0].specializations[0].value, 8)
        self.assertEqual(module.instance_bindings[0].port, "x")
        self.assertEqual(module.instance_bindings[0].expression.name, "x")

    def test_inherited_clock_reset_is_preserved(self):
        source = (
            "module Child { in en:bit out y:u8 reg q:u8=0 "
            "rule step when en { q <- q } y=q } "
            "module Top { clock clk reset rst in en:bit out y:u8 "
            "inst c:Child { en } y=c.y }"
        )
        module = analyze(parse(source))
        self.assertEqual(module.children[0].clock, "clk")
        self.assertEqual(module.children[0].reset, "rst")
        self.assertEqual(module.elaborated_instances[0].clock, "clk")

    def test_stateful_child_without_domain_is_diagnostic(self):
        with self.assertRaisesRegex(SemanticError, "requires a clock and reset"):
            analyze(parse("module Child { out y:u8 reg q:u8=0 y=q }"))

    def test_inline_binding_diagnostics(self):
        duplicate = (
            "module C { in x:u8 out y:u8 y=x } "
            "module T { in x:u8 out y:u8 inst c:C { x } c.x=x y=c.y }"
        )
        with self.assertRaisesRegex(SemanticError, "assigned more than once"):
            analyze(parse(duplicate))
        missing = (
            "module C { in x:u8 out y:u8 y=x } "
            "module T { out y:u8 inst c:C { x } y=c.y }"
        )
        with self.assertRaisesRegex(SemanticError, "unknown (?:name|input) 'x'"):
            analyze(parse(missing))

    def test_direct_buffered_connection_normalizes_to_hierarchical_ir(self):
        source = (
            "module E { out req:rv<u8> req.valid=1 req.payload=1 } "
            "module T { clock clk reset rst out mem:rv<u8> inst e:E "
            "connect e.req -> mem { buffer 2 } }"
        )
        module = analyze(parse(source))
        self.assertEqual(module.hierarchical_connections[0].buffer_depth, 2)

    def test_concise_and_verbose_forms_have_equal_ir_identity(self):
        verbose = (
            "module C<AW=8> { in x:uint<AW> out y:uint<AW> y=x } "
            "module T<AW=8> { in x:uint<AW> out y:uint<AW> "
            "inst c:C<AW=AW> c.x=x y=c.y }"
        )
        concise = (
            "module C<AW=8> { in x:uint<AW> out y:uint<AW> y=x } "
            "module T<AW=8> { in x:uint<AW> out y:uint<AW> "
            "inst c:C<AW> { x } y=c.y }"
        )
        verbose_ir = analyze(parse(verbose))
        concise_ir = analyze(parse(concise))
        self.assertEqual(verbose_ir, concise_ir)
        verbose_canonical = lower(verbose_ir)
        concise_canonical = lower(concise_ir)
        self.assertEqual(
            tuple(replace(node, origins=()) for node in verbose_canonical.expressions),
            tuple(replace(node, origins=()) for node in concise_canonical.expressions),
        )

    def test_explicit_fifo_wrapper_and_direct_buffer_emit_fifo_state(self):
        direct = (
            "module E { out req:rv<u8> req.valid=1 req.payload=1 } "
            "module T { clock clk reset rst out mem:rv<u8> inst e:E "
            "connect e.req -> mem { buffer 2 } }"
        )
        wrapper = (
            "module E { out req:rv<u8> req.valid=1 req.payload=1 } "
            "module F { clock clk reset rst in rx:rv<u8> out tx:rv<u8> "
            "connect rx -> tx { buffer 2 } } "
            "module T { clock clk reset rst out mem:rv<u8> inst e:E inst f:F "
            "connect e.req -> f.rx connect f.tx -> mem }"
        )
        direct_text = compile_source(direct, top="T").clash
        wrapper_text = compile_source(wrapper, top="T").clash
        self.assertIn("buffer_count", direct_text)
        self.assertIn("protocol_f", wrapper_text)

    def test_sequential_child_elaboration_propagates_domain(self):
        module = analyze(parse(
            "module Child { clock c reset r in en:bit out y:u8 reg q:u8 = 0 "
            "rule step when en { q <- truncate<8>(q + 1) } y = q } "
            "module Top { clock c reset r in en:bit out y:u8 inst x:Child "
            "x.en=en y=x.y }"
        ))
        self.assertEqual(module.elaborated_instances[0].clock, "c")
        self.assertEqual(module.elaborated_instances[0].reset, "r")
        self.assertTrue(module.children[0].is_sequential)

    def test_hierarchical_ready_valid_endpoints_are_typed(self):
        module = analyze(parse(
            "module P { clock c reset r out tx:rv<u8> tx.payload=1 tx.valid=1 } "
            "module C { clock c reset r in rx:rv<u8> out y:u8 rx.ready=1 y=rx.payload } "
            "module Top { clock c reset r out y:u8 inst p:P inst c:C "
            "connect p.tx -> c.rx y=c.y }"
        ))
        self.assertEqual(len(module.hierarchical_connections), 1)
        endpoint = module.hierarchical_connections[0].source
        self.assertEqual(endpoint.protocol.value, "ready_valid")
        self.assertEqual(endpoint.payload_type.width, 8)

    def test_protocol_example_emits_preserved_hierarchy(self):
        source = Path("examples/hierarchical_protocol_m40.zhl").read_text()
        module = analyze(parse(source))
        self.assertEqual(len(module.hierarchical_connections), 2)
        result = compile_source(source, top="ProtocolTop")
        self.assertIn("protocol_requestFifo", result.clash)
        self.assertIn("fifo_rx = producer_tx", result.clash)
        self.assertIn("producer_tx_ready = fifo_rx_ready", result.clash)
        self.assertIn("fifo_tx_ready = consumer_rx_ready", result.clash)

    def test_protocol_manifest_publishes_physical_handshake_bindings(self):
        source = Path("examples/hierarchical_protocol_m40.zhl").read_text()
        module = analyze(parse(source))
        artifact = emit_artifact(module, selected_ir_identity="protocol-top-v1")
        ids = {item.semantic_signal_id for item in artifact.bindings}
        self.assertIn("endpoint:producer.tx.valid", ids)
        self.assertIn("endpoint:producer.tx.payload", ids)
        self.assertIn("endpoint:fifo.rx.ready", ids)
        self.assertIn("endpoint:consumer.rx.ready", ids)

    def test_mixed_stateful_child_preserves_scalar_and_protocol_ports(self):
        source = """
        struct Req { addr:u8 data:u8 write:bit }
        module Engine {
          clock clk reset rst
          in start:bit in base:u8
          out busy:bit out req:rv<Req>
          reg count:u8 = 0
          rule step when start { count <- truncate<8>(count + 1) }
          busy = start
          req.payload = Req { addr=base data=0 write=1 }
          req.valid = start
        }
        module Sink {
          clock clk reset rst
          in rx:rv<Req> out seen:bit
          rx.ready = 1
          seen = rx.valid
        }
        module Top {
          clock clk reset rst
          in start:bit in base:u8 out busy:bit
          inst e:Engine inst s:Sink
          e.start=start e.base=base
          connect e.req -> s.rx
          busy=e.busy
        }
        """
        result = compile_source(source, top="Top")
        self.assertIn("protocol_engine", result.clash)
        self.assertIn("Signal ZLangSystem EngineComponentInput -> Signal ZLangSystem EngineComponentOutput", result.clash)
        self.assertIn("protocol_engine = mealy protocol_engineTransition", result.clash)
        self.assertIn("{-# NOINLINE protocol_engine #-}", result.clash)
        self.assertIn("e_component_input = EngineComponentInput <$> parent_start", result.clash)
        self.assertNotIn("protocol_engine mixed_start", result.clash)
        transition = result.clash.split("protocol_engineTransition ::", 1)[1].split(
            "protocol_engine ::", 1
        )[0]
        self.assertNotIn("parent_", transition)

    def test_simple_dma_uses_closed_mixed_child_and_request_fifo(self):
        source = Path("examples/simple_dma_m40.zhl").read_text()
        result = compile_source(source, top="SimpleDMA")
        self.assertIn("protocol_transferEngine = mealy", result.clash)
        self.assertIn("engine_component_input = TransferEngineComponentInput", result.clash)
        self.assertIn("engine_mem_request_memory_mem_request_buffer_count", result.clash)
        self.assertIn("engine_mem_request_ready_bit", result.clash)
        self.assertIn("memory_mem_request = ZLangReadyValidForward", result.clash)
        transition = result.clash.split("protocol_transferEngineTransition ::", 1)[1].split(
            "protocol_transferEngine ::", 1
        )[0]
        self.assertNotIn("parent_", transition)

    @unittest.skipUnless(find_clash_executable() and shutil.which("verilator"),
                         "Clash and Verilator are required")
    def test_protocol_hierarchy_runs_in_verilator(self):
        source = Path("examples/hierarchical_protocol_m40.zhl").read_text()
        result = compile_source(source, top="ProtocolTop")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = generate_verilog(result.clash, "ProtocolTop", root / "rtl",
                                     find_clash_executable())
            lint_with_verilator(files, "ProtocolTop")
            harness = root / "protocol_test.cpp"
            harness.write_text(
                '#include "VProtocolTop.h"\n'
                'static void tick(VProtocolTop& d) { d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval(); }\n'
                'int main() { VProtocolTop d; d.rst=1; for(int i=0;i<3;i++) tick(d); d.rst=0; for(int i=0;i<4;i++) tick(d); return d.seen == 7 ? 0 : 1; }\n'
            )
            obj = root / "obj"
            env = os.environ.copy(); env["CCACHE_DISABLE"] = "1"
            subprocess.run([
                shutil.which("verilator"), "--cc", "--exe", "--build",
                "--top-module", "ProtocolTop", "--Mdir", str(obj),
                "-o", "protocol_sim", *(str(f) for f in files), str(harness)
            ], check=True, cwd=Path.cwd(), env=env)
            subprocess.run([str(obj / "protocol_sim")], check=True)

    @unittest.skipUnless(find_clash_executable() and shutil.which("verilator"),
                         "Clash and Verilator are required")
    def test_simple_dma_request_fifo_runs_in_verilator(self):
        source = Path("examples/simple_dma_m40.zhl").read_text()
        result = compile_source(source, top="SimpleDMA")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = generate_verilog(result.clash, "SimpleDMA", root / "rtl",
                                     find_clash_executable())
            self.assertTrue(any("protocol_transferEngine" in path.name for path in files))
            lint_with_verilator(files, "SimpleDMA")
            harness = root / "dma_test.cpp"
            harness.write_text(
                '#include "VSimpleDMA.h"\n'
                'static void tick(VSimpleDMA& d) { d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval(); }\n'
                'int main() { VSimpleDMA d; d.base=10; d.data=0x5a; d.start=0; d.accept=0; '
                'd.rst=1; tick(d); tick(d); d.rst=0; tick(d); if(d.busy) return 1; '
                'd.start=1; tick(d); if(!d.busy) return 2; '
                'tick(d); d.accept=1; tick(d); if(!d.busy) return 3; '
                'd.start=0; tick(d); if(d.busy) return 4; return 0; }\n'
            )
            obj = root / "obj"
            env = os.environ.copy(); env["CCACHE_DISABLE"] = "1"
            subprocess.run([
                shutil.which("verilator"), "--cc", "--exe", "--build",
                "--top-module", "SimpleDMA", "--Mdir", str(obj),
                "-o", "dma_sim", *(str(f) for f in files), str(harness)
            ], check=True, cwd=Path.cwd(), env=env)
            subprocess.run([str(obj / "dma_sim")], check=True)

    def test_top_selection_preserves_shared_structs(self):
        result = compile_source(
            "struct P { x:u8 } module Leaf { in x:u8 out y:P y=P { x=x } }",
            top="Leaf",
        )
        self.assertIn("data P", result.clash)


if __name__ == "__main__":
    unittest.main()
