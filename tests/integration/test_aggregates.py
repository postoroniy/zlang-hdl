from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.toolchain import CLASH_ENVIRONMENT, CLASH_EXECUTABLE
from zlang.compiler import compile_source
from zlang.simulate import SimulationError, simulate


ROOT = Path(__file__).resolve().parents[2]


class AggregateIntegrationTests(unittest.TestCase):
    def test_fir_behavior(self) -> None:
        module = compile_source((ROOT / "examples/fir2.zhl").read_text()).ir
        self.assertEqual(
            simulate(module, samples=[2, 3], coefficients=(4, 5)),
            {"y": 23},
        )
        self.assertEqual(
            simulate(module, samples=[255, 255], coefficients=[255, 255]),
            {"y": 130050},
        )

    def test_struct_field_behavior_and_shape_validation(self) -> None:
        module = compile_source((ROOT / "examples/packet_data.zhl").read_text()).ir
        packet = {"data": 0x1234, "last": 1, "vc": 2}
        self.assertEqual(simulate(module, packet=packet), {"y": 0x1234})
        with self.assertRaisesRegex(SimulationError, "does not fit Packet"):
            simulate(module, packet={"data": 1, "last": 0})
        with self.assertRaisesRegex(SimulationError, "does not fit Packet"):
            simulate(module, packet={"data": 1, "last": 0, "vc": 4})

    def test_vector_shape_validation(self) -> None:
        module = compile_source((ROOT / "examples/fir2.zhl").read_text()).ir
        with self.assertRaisesRegex(SimulationError, "does not fit vec<2,u8>"):
            simulate(module, samples=[1], coefficients=[2, 3])

    @unittest.skipUnless(CLASH_EXECUTABLE, "Clash executable is not available")
    def test_milestone_three_examples_compile_to_verilog(self) -> None:
        for source_name in ("FIR2.hs", "PacketData.hs"):
            with self.subTest(source=source_name), tempfile.TemporaryDirectory() as temporary:
                subprocess.run(
                    [
                        CLASH_EXECUTABLE,
                        "--verilog",
                        str(ROOT / "examples/generated" / source_name),
                        "-outputdir",
                        temporary,
                    ],
                    check=True,
                    cwd=ROOT,
                    env=CLASH_ENVIRONMENT,
                )


if __name__ == "__main__":
    unittest.main()
