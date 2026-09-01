from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.cli import main
from zlang.compiler import compile_source
from zlang.toolchain import generate_verilog


ROOT = Path(__file__).resolve().parents[2]
VERILATOR = shutil.which("verilator")


class VerilatorIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(
        CLASH_EXECUTABLE and VERILATOR,
        "Clash and Verilator are required",
    )
    def test_cli_generates_persistent_verilog_and_lints_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "Add.hs"
            verilog_directory = root / "rtl"
            with redirect_stdout(io.StringIO()):
                status = main(
                    [
                        str(ROOT / "examples/add.zl"),
                        "-o",
                        str(output),
                        "--verilog-dir",
                        str(verilog_directory),
                        "--verilator-lint",
                    ]
                )
            self.assertEqual(status, 0)
            self.assertTrue(output.is_file())
            self.assertEqual(
                [path.name for path in verilog_directory.rglob("*.v")],
                ["zlang_core_Add.v"],
            )
            self.assertTrue((verilog_directory / "Add.sv").is_file())

    @unittest.skipUnless(
        CLASH_EXECUTABLE and VERILATOR,
        "Clash and Verilator are required",
    )
    def test_generated_add_runs_in_verilator(self) -> None:
        source = (ROOT / "examples/add.zl").read_text()
        result = compile_source(source)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            verilog_files = generate_verilog(
                result.clash,
                result.ir.name,
                root / "rtl",
                CLASH_EXECUTABLE,
            )
            harness = root / "add_test.cpp"
            harness.write_text(
                "#include \"VAdd.h\"\n"
                "int main() {\n"
                "  VAdd dut;\n"
                "  dut.a = 255; dut.b = 255; dut.eval();\n"
                "  if (dut.y != 510) return 1;\n"
                "  dut.a = 7; dut.b = 5; dut.eval();\n"
                "  return dut.y == 12 ? 0 : 2;\n"
                "}\n"
            )
            object_directory = root / "obj"
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            subprocess.run(
                [
                    VERILATOR,
                    "--cc",
                    "--exe",
                    "--build",
                    "--top-module",
                    "Add",
                    "--Mdir",
                    str(object_directory),
                    "-o",
                    "add_sim",
                    *(str(path) for path in verilog_files),
                    str(harness),
                ],
                check=True,
                cwd=ROOT,
                env=environment,
            )
            subprocess.run([str(object_directory / "add_sim")], check=True)

    @unittest.skipUnless(
        CLASH_EXECUTABLE and VERILATOR,
        "Clash and Verilator are required",
    )
    def test_buffered_ready_valid_runs_in_verilator(self) -> None:
        result = compile_source((ROOT / "examples/rv_buffer.zl").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            verilog_files = generate_verilog(
                result.clash,
                result.ir.name,
                root / "rtl",
                CLASH_EXECUTABLE,
            )
            harness = root / "buffer_test.cpp"
            harness.write_text(
                "#include \"VRvBuffer.h\"\n"
                "static void tick(VRvBuffer& dut) {\n"
                "  dut.clk = 0; dut.eval();\n"
                "  dut.clk = 1; dut.eval();\n"
                "  dut.clk = 0; dut.eval();\n"
                "}\n"
                "int main() {\n"
                "  VRvBuffer dut;\n"
                "  dut.rst = 1; dut.rx_valid = 0; dut.rx_payload = 0;\n"
                "  dut.tx_ready = 0; tick(dut);\n"
                "  if (dut.rx_ready || dut.tx_valid) return 1;\n"
                "  dut.rst = 0; dut.rx_valid = 1; dut.rx_payload = 11;\n"
                "  dut.eval(); if (!dut.rx_ready || dut.tx_valid) return 2;\n"
                "  tick(dut);\n"
                "  dut.rx_payload = 22; dut.eval();\n"
                "  if (!dut.rx_ready || !dut.tx_valid || dut.tx_payload != 11) return 3;\n"
                "  tick(dut); dut.eval();\n"
                "  if (dut.rx_ready || !dut.tx_valid || dut.tx_payload != 11) return 4;\n"
                "  dut.rx_valid = 0; dut.tx_ready = 1; tick(dut); dut.eval();\n"
                "  if (!dut.tx_valid || dut.tx_payload != 22) return 5;\n"
                "  tick(dut); dut.eval();\n"
                "  return dut.tx_valid ? 6 : 0;\n"
                "}\n"
            )
            object_directory = root / "obj"
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            subprocess.run(
                [
                    VERILATOR,
                    "--cc",
                    "--exe",
                    "--build",
                    "--top-module",
                    "RvBuffer",
                    "--Mdir",
                    str(object_directory),
                    "-o",
                    "buffer_sim",
                    *(str(path) for path in verilog_files),
                    str(harness),
                ],
                check=True,
                cwd=ROOT,
                env=environment,
            )
            subprocess.run([str(object_directory / "buffer_sim")], check=True)

    @unittest.skipUnless(
        CLASH_EXECUTABLE and VERILATOR,
        "Clash and Verilator are required",
    )
    def test_csr_decode_and_access_policies_run_in_verilator(self) -> None:
        result = compile_source((ROOT / "examples/control_csr.zl").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            verilog_files = generate_verilog(
                result.clash,
                result.ir.name,
                root / "rtl",
                CLASH_EXECUTABLE,
            )
            harness = root / "csr_test.cpp"
            harness.write_text(
                "#include \"VControlCsr.h\"\n"
                "static void tick(VControlCsr& dut) {\n"
                "  dut.clk = 0; dut.eval(); dut.clk = 1; dut.eval();\n"
                "  dut.clk = 0; dut.eval();\n"
                "}\n"
                "int main() {\n"
                "  VControlCsr dut; dut.addr = 0; dut.write = 0;\n"
                "  dut.wdata = 0; dut.read = 0; dut.rst = 1; tick(dut);\n"
                "  dut.rst = 0; dut.addr = 0x40000004; dut.read = 1; dut.eval();\n"
                "  if (!dut.ready || dut.rdata != 3) return 1;\n"
                "  dut.read = 0; dut.write = 1; dut.addr = 0x40000000;\n"
                "  dut.wdata = 0xbb; tick(dut);\n"
                "  dut.write = 0; dut.read = 1; dut.eval();\n"
                "  if (!dut.ready || dut.rdata != 0xb) return 2;\n"
                "  dut.read = 0; dut.write = 1; dut.addr = 0x40000004;\n"
                "  dut.wdata = 2; tick(dut);\n"
                "  dut.write = 0; dut.read = 1; dut.eval();\n"
                "  if (!dut.ready || dut.rdata != 1) return 3;\n"
                "  dut.addr = 0x40000008; dut.eval();\n"
                "  return (!dut.ready && dut.rdata == 0) ? 0 : 4;\n"
                "}\n"
            )
            object_directory = root / "obj"
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            subprocess.run(
                [
                    VERILATOR,
                    "--cc",
                    "--exe",
                    "--build",
                    "--top-module",
                    "ControlCsr",
                    "--Mdir",
                    str(object_directory),
                    "-o",
                    "csr_sim",
                    *(str(path) for path in verilog_files),
                    str(harness),
                ],
                check=True,
                cwd=ROOT,
                env=environment,
            )
            subprocess.run([str(object_directory / "csr_sim")], check=True)

    @unittest.skipUnless(
        CLASH_EXECUTABLE and VERILATOR,
        "Clash and Verilator are required",
    )
    def test_hardware_connected_csr_runs_in_verilator(self) -> None:
        result = compile_source((ROOT / "examples/engine_csr.zl").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            verilog_files = generate_verilog(
                result.clash,
                result.ir.name,
                root / "rtl",
                CLASH_EXECUTABLE,
            )
            harness = root / "engine_csr_test.cpp"
            harness.write_text(
                "#include \"VEngineCsr.h\"\n"
                "static void tick(VEngineCsr& d) { d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval(); }\n"
                "int main() {\n"
                " VEngineCsr d; d.addr=0; d.write=0; d.wdata=0; d.read=0;\n"
                " d.engine_busy=0; d.engine_error=0; d.rst=1; tick(d); d.rst=0;\n"
                " d.engine_error=1; tick(d); d.engine_error=0; d.addr=0x50000004; d.read=1; d.eval();\n"
                " if (d.rdata != 2) return 1;\n"
                " d.read=0; d.write=1; d.wdata=2; d.engine_error=1; tick(d);\n"
                " d.write=0; d.engine_error=0; d.read=1; d.eval(); if (d.rdata != 2) return 2;\n"
                " d.read=0; d.write=1; d.wdata=2; tick(d); d.write=0; d.read=1; d.eval();\n"
                " if (d.rdata != 0) return 3;\n"
                " d.engine_busy=1; d.eval(); if (d.rdata != 1) return 4;\n"
                " d.read=0; d.write=1; d.addr=0x50000000; d.wdata=1; tick(d);\n"
                " if (!d.engine_start) return 5; d.write=0; tick(d);\n"
                " return d.engine_start ? 6 : 0;\n"
                "}\n"
            )
            object_directory = root / "obj"
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            subprocess.run(
                [
                    VERILATOR,
                    "--cc",
                    "--exe",
                    "--build",
                    "--top-module",
                    "EngineCsr",
                    "--Mdir",
                    str(object_directory),
                    "-o",
                    "engine_csr_sim",
                    *(str(path) for path in verilog_files),
                    str(harness),
                ],
                check=True,
                cwd=ROOT,
                env=environment,
            )
            subprocess.run(
                [str(object_directory / "engine_csr_sim")], check=True
            )

    @unittest.skipUnless(
        CLASH_EXECUTABLE and VERILATOR,
        "Clash and Verilator are required",
    )
    def test_prioritized_rules_run_in_verilator(self) -> None:
        result = compile_source((ROOT / "examples/rule_counter.zl").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            verilog_files = generate_verilog(
                result.clash, result.ir.name, root / "rtl", CLASH_EXECUTABLE
            )
            harness = root / "rules_test.cpp"
            harness.write_text(
                "#include \"VRuleCounter.h\"\n"
                "static void tick(VRuleCounter& d) { d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval(); }\n"
                "int main() { VRuleCounter d; d.increment=0; d.clear=0; d.rst=1; tick(d); d.rst=0;\n"
                " d.increment=1; tick(d); if (d.count_out != 1) return 1;\n"
                " tick(d); if (d.count_out != 2) return 2;\n"
                " d.clear=1; tick(d); if (d.count_out != 0) return 3;\n"
                " d.clear=0; tick(d); return d.count_out == 1 ? 0 : 4; }\n"
            )
            object_directory = root / "obj"
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            subprocess.run(
                [
                    VERILATOR,
                    "--cc",
                    "--exe",
                    "--build",
                    "--top-module",
                    "RuleCounter",
                    "--Mdir",
                    str(object_directory),
                    "-o",
                    "rules_sim",
                    *(str(path) for path in verilog_files),
                    str(harness),
                ],
                check=True,
                cwd=ROOT,
                env=environment,
            )
            subprocess.run([str(object_directory / "rules_sim")], check=True)

    @unittest.skipUnless(
        CLASH_EXECUTABLE and VERILATOR,
        "Clash and Verilator are required",
    )
    def test_rule_output_action_runs_in_verilator(self) -> None:
        result = compile_source((ROOT / "examples/rule_action.zl").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            verilog_files = generate_verilog(
                result.clash, result.ir.name, root / "rtl", CLASH_EXECUTABLE
            )
            harness = root / "rule_action_test.cpp"
            harness.write_text(
                "#include \"VRuleAction.h\"\n"
                "static void tick(VRuleAction& d) { d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval(); }\n"
                "int main() { VRuleAction d; d.enable=1; d.rst=1; d.eval();\n"
                " if (d.fired) return 1; tick(d); d.rst=0; d.eval();\n"
                " if (!d.fired) return 2; tick(d); d.enable=0; d.eval();\n"
                " return d.fired ? 3 : 0; }\n"
            )
            object_directory = root / "obj"
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            subprocess.run(
                [
                    VERILATOR,
                    "--cc",
                    "--exe",
                    "--build",
                    "--top-module",
                    "RuleAction",
                    "--Mdir",
                    str(object_directory),
                    "-o",
                    "rule_action_sim",
                    *(str(path) for path in verilog_files),
                    str(harness),
                ],
                check=True,
                cwd=ROOT,
                env=environment,
            )
            subprocess.run(
                [str(object_directory / "rule_action_sim")], check=True
            )

    @unittest.skipUnless(
        CLASH_EXECUTABLE and VERILATOR,
        "Clash and Verilator are required",
    )
    def test_declared_fifo_runs_in_verilator(self) -> None:
        result = compile_source((ROOT / "examples/fifo_bridge.zl").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            verilog_files = generate_verilog(
                result.clash, result.ir.name, root / "rtl", CLASH_EXECUTABLE
            )
            harness = root / "fifo_test.cpp"
            harness.write_text(
                "#include \"VFifoBridge.h\"\n"
                "static void tick(VFifoBridge& d) { d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval(); }\n"
                "int main() { VFifoBridge d; d.rx_payload=0; d.rx_valid=0; d.tx_ready=0; d.rst=1; tick(d);\n"
                " if (d.rx_ready || d.tx_valid) return 1; d.rst=0; d.rx_valid=1; d.rx_payload=11; tick(d);\n"
                " if (!d.tx_valid || d.tx_payload != 11) return 2; d.rx_payload=22; tick(d);\n"
                " if (d.tx_payload != 11 || !d.rx_ready) return 3; d.rx_payload=33; tick(d);\n"
                " d.rx_payload=44; tick(d); if (d.rx_ready || d.tx_payload != 11) return 4;\n"
                " d.tx_ready=1; d.rx_payload=55; d.eval(); if (!d.rx_ready || d.tx_payload != 11) return 5; tick(d);\n"
                " if (d.tx_payload != 22) return 6; d.rx_valid=0; tick(d);\n"
                " if (d.tx_payload != 33) return 7; tick(d); if (d.tx_payload != 44) return 8;\n"
                " tick(d); if (d.tx_payload != 55 || !d.tx_valid) return 9; tick(d);\n"
                " return d.tx_valid ? 10 : 0; }\n"
            )
            object_directory = root / "obj"
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            subprocess.run(
                [
                    VERILATOR,
                    "--cc",
                    "--exe",
                    "--build",
                    "--top-module",
                    "FifoBridge",
                    "--Mdir",
                    str(object_directory),
                    "-o",
                    "fifo_sim",
                    *(str(path) for path in verilog_files),
                    str(harness),
                ],
                check=True,
                cwd=ROOT,
                env=environment,
            )
            subprocess.run([str(object_directory / "fifo_sim")], check=True)

    @unittest.skipUnless(
        CLASH_EXECUTABLE and VERILATOR,
        "Clash and Verilator are required",
    )
    def test_synchronous_memory_runs_in_verilator(self) -> None:
        result = compile_source((ROOT / "examples/sync_memory.zl").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            verilog_files = generate_verilog(
                result.clash, result.ir.name, root / "rtl", CLASH_EXECUTABLE
            )
            harness = root / "memory_test.cpp"
            harness.write_text(
                "#include \"VSyncMemory.h\"\n"
                "static void tick(VSyncMemory& d) { d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval(); }\n"
                "int main() { VSyncMemory d; d.read_address=0; d.write_enable=0; d.write_address=0; d.write_data=0; d.rst=1; tick(d);\n"
                " if (d.read_data != 0) return 1; d.rst=0; d.read_address=1; d.write_enable=1; d.write_address=1; d.write_data=9; tick(d);\n"
                " if (d.read_data != 0) return 2; d.write_enable=0; tick(d);\n"
                " if (d.read_data != 9) return 3; d.read_address=0; tick(d);\n"
                " if (d.read_data != 0) return 4; d.rst=1; tick(d);\n"
                " return d.read_data == 0 ? 0 : 5; }\n"
            )
            object_directory = root / "obj"
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            subprocess.run(
                [
                    VERILATOR,
                    "--cc",
                    "--exe",
                    "--build",
                    "-Wno-WIDTHTRUNC",
                    "--top-module",
                    "SyncMemory",
                    "--Mdir",
                    str(object_directory),
                    "-o",
                    "memory_sim",
                    *(str(path) for path in verilog_files),
                    str(harness),
                ],
                check=True,
                cwd=ROOT,
                env=environment,
            )
            subprocess.run([str(object_directory / "memory_sim")], check=True)


if __name__ == "__main__":
    unittest.main()
