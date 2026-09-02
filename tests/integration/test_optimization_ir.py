from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from zlang.backend.clash import emit
from zlang.backend.systemverilog import emit_contracts
from zlang.cli import main
from zlang.compiler import compile_source
from zlang.costs import extract_estimated_costs
from zlang.csr import emit_csr_json, emit_csr_markdown
from zlang.opt import lower, restore
from zlang.parser import parse
from zlang.semantic import analyze
from zlang.simulate import simulate, simulate_cycles


ROOT = Path(__file__).resolve().parents[2]


class CanonicalOptimizationIntegrationTests(unittest.TestCase):
    def test_every_checked_in_example_round_trips_and_preserves_artifacts(self) -> None:
        for path in sorted((ROOT / "examples").glob("*.zhl")):
            with self.subTest(example=path.name):
                source = path.read_text()
                semantic = analyze(parse(source))
                canonical = lower(semantic)
                restored = restore(canonical)

                self.assertEqual(restored, semantic)
                restored_for_backend = extract_estimated_costs(restored).module
                semantic_for_backend = extract_estimated_costs(semantic).module
                self.assertEqual(
                    emit(restored_for_backend), emit(semantic_for_backend)
                )
                self.assertEqual(emit_contracts(restored), emit_contracts(semantic))
                if semantic.csr_blocks:
                    self.assertEqual(
                        emit_csr_markdown(restored), emit_csr_markdown(semantic)
                    )
                    self.assertEqual(emit_csr_json(restored), emit_csr_json(semantic))

    def test_value_sequential_protocol_and_architecture_behavior_is_unchanged(self) -> None:
        add_source = (ROOT / "examples/add.zhl").read_text()
        add_semantic = analyze(parse(add_source))
        add_restored = restore(lower(add_semantic))
        self.assertEqual(
            simulate(add_restored, a=255, b=255),
            simulate(add_semantic, a=255, b=255),
        )

        counter_source = (ROOT / "examples/counter.zhl").read_text()
        counter_semantic = analyze(parse(counter_source))
        counter_restored = restore(lower(counter_semantic))
        cycles = [{}, {}, {}, {}]
        resets = [True, False, False, False]
        self.assertEqual(
            simulate_cycles(counter_restored, cycles, resets),
            simulate_cycles(counter_semantic, cycles, resets),
        )

        protocol_source = (ROOT / "examples/rv_passthrough.zhl").read_text()
        protocol_semantic = analyze(parse(protocol_source))
        protocol_restored = restore(lower(protocol_semantic))
        protocol_inputs = {
            "rx": {"payload": 42, "valid": 1},
            "tx": {"ready": 0},
        }
        self.assertEqual(
            simulate(protocol_restored, **protocol_inputs),
            simulate(protocol_semantic, **protocol_inputs),
        )

        fifo_source = (ROOT / "examples/fifo_bridge.zhl").read_text()
        fifo_semantic = analyze(parse(fifo_source))
        fifo_restored = restore(lower(fifo_semantic))
        fifo_cycles = [
            {"rx": {"payload": 0, "valid": 0}, "tx": {"ready": 0}},
            {"rx": {"payload": 7, "valid": 1}, "tx": {"ready": 0}},
            {"rx": {"payload": 0, "valid": 0}, "tx": {"ready": 1}},
        ]
        self.assertEqual(
            simulate_cycles(fifo_restored, fifo_cycles, [True, False, False]),
            simulate_cycles(fifo_semantic, fifo_cycles, [True, False, False]),
        )

    def test_cli_writes_canonical_ir_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "Add.opt"
            with redirect_stdout(io.StringIO()):
                status = main(
                    [
                        str(ROOT / "examples/add.zhl"),
                        "--optimization-ir",
                        str(output),
                    ]
                )
            self.assertEqual(status, 0)
            self.assertEqual(
                output.read_text(),
                (ROOT / "examples/generated/Add.opt").read_text(),
            )


if __name__ == "__main__":
    unittest.main()
