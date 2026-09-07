import hashlib
import re
import unittest
from dataclasses import replace
from unittest.mock import patch

from zlang.compiler import compile_source
from zlang.backend.manifest import (
    BackendArtifact,
    MANIFEST_VERSION,
    PHYSICAL_DOMAIN_MANIFEST_VERSION,
    PhysicalDomainManifest,
)
from zlang.cross_backend import (
    emit_cross_backend_miter,
    emit_cross_backend_miter_with_metadata,
    run_cross_backend_formal,
    validate_module_route,
    validate_artifacts,
)
from zlang.ir.cdc import (
    ClockDomain,
    ClockEdge,
    PowerUpPolicy,
    ResetMode,
    ResetPolarity,
    ResetReleaseMode,
)
from zlang.ir.cross_backend import CrossBackendError, CrossBackendMode, CrossBackendProperty, CrossBackendRelation, CrossBackendStatus
from zlang.ir.equivalence import BindingSide, EquivalenceBinding, SignalRole
from zlang.ir.formal import Counterexample, FormalResult, FormalStatus, ProofMode
from zlang.ir.types import UIntType


def artifact(backend, module, expression, *, identity="selected:alu", output="y"):
    text = f"module {module}(input [3:0] x, output [3:0] {output}); assign {output} = {expression}; endmodule\n"
    digest = hashlib.sha256(text.encode()).hexdigest()
    bindings = (
        EquivalenceBinding(2, BindingSide.IMPLEMENTATION, "port:x", identity, module, "x", 4, "unsigned", SignalRole.INPUT, None, None, backend, digest),
        EquivalenceBinding(2, BindingSide.IMPLEMENTATION, "port:y", identity, module, output, 4, "unsigned", SignalRole.OUTPUT, None, None, backend, digest),
    )
    return BackendArtifact(backend, module, identity, digest, text, bindings)


def timed_artifact(backend, module, expression, *, identity="selected:pipe", output="y"):
    text = (f"module {module}(input clock, input reset, input [3:0] x, output [3:0] {output});\n"
            f"  reg [3:0] q; always @(posedge clock) if (reset) q <= 0; else q <= {expression};\n"
            f"  assign {output} = q; endmodule\n")
    digest = hashlib.sha256(text.encode()).hexdigest()
    bindings = (
        EquivalenceBinding(2, BindingSide.IMPLEMENTATION, "port:x", identity, module, "x", 4, "unsigned", SignalRole.INPUT, "clk", "rst", backend, digest),
        EquivalenceBinding(2, BindingSide.IMPLEMENTATION, "port:y", identity, module, output, 4, "unsigned", SignalRole.OUTPUT, "clk", "rst", backend, digest),
        EquivalenceBinding(2, BindingSide.IMPLEMENTATION, "clock", identity, module, "clock", 1, "bit", SignalRole.CLOCK, "clk", "rst", backend, digest),
        EquivalenceBinding(2, BindingSide.IMPLEMENTATION, "reset", identity, module, "reset", 1, "bit", SignalRole.RESET, "clk", "rst", backend, digest),
    )
    return BackendArtifact(backend, module, identity, digest, text, bindings)


def physical_timed_artifact(
    backend,
    module,
    expression,
    domain,
    *,
    identity="selected:physical-pipe",
    output="y",
):
    clock_edge = "posedge" if domain.edge is ClockEdge.RISING else "negedge"
    reset_asserted = (
        "reset"
        if domain.reset_polarity is ResetPolarity.ACTIVE_HIGH else "!reset"
    )
    reset_event = (
        "posedge reset"
        if domain.reset_polarity is ResetPolarity.ACTIVE_HIGH
        else "negedge reset"
    )
    event = clock_edge + " clock"
    if domain.reset_mode is ResetMode.ASYNCHRONOUS:
        event += " or " + reset_event
    text = (
        f"module {module}(input clock, input reset, input [3:0] x, "
        f"output [3:0] {output});\n"
        f"  reg [3:0] q; always @({event}) if ({reset_asserted}) q <= 0; "
        f"else q <= {expression};\n"
        f"  assign {output} = q; endmodule\n"
    )
    digest = hashlib.sha256(text.encode()).hexdigest()
    version = PHYSICAL_DOMAIN_MANIFEST_VERSION
    bindings = (
        EquivalenceBinding(
            version, BindingSide.IMPLEMENTATION, "port:x", identity, module,
            "x", 4, "unsigned", SignalRole.INPUT, domain.clock,
            domain.reset, backend, digest,
        ),
        EquivalenceBinding(
            version, BindingSide.IMPLEMENTATION, "port:y", identity, module,
            output, 4, "unsigned", SignalRole.OUTPUT, domain.clock,
            domain.reset, backend, digest,
        ),
        EquivalenceBinding(
            version, BindingSide.IMPLEMENTATION, "clock", identity, module,
            "clock", 1, "bit", SignalRole.CLOCK, domain.clock,
            domain.reset, backend, digest,
        ),
        EquivalenceBinding(
            version, BindingSide.IMPLEMENTATION, "reset", identity, module,
            "reset", 1, "bit", SignalRole.RESET, domain.clock,
            domain.reset, backend, digest,
        ),
    )
    physical = PhysicalDomainManifest.publish(
        domain,
        rtl_module=module,
        rtl_clock_path="clock",
        rtl_reset_path="reset",
    )
    return BackendArtifact(
        backend, module, identity, digest, text, bindings,
        manifest_version=version,
        physical_domains=(physical,),
    )


def physical_value_artifact(
    backend,
    module,
    expression,
    domain,
    *,
    identity="selected:physical-value",
    output="y",
):
    base = physical_timed_artifact(
        backend, module, expression, domain, identity=identity, output=output
    )
    text = (
        f"module {module}(input clock, input reset, input [3:0] x, "
        f"output [3:0] {output}); assign {output} = {expression}; endmodule\n"
    )
    digest = hashlib.sha256(text.encode()).hexdigest()
    return replace(
        base,
        text=text,
        artifact_hash=digest,
        bindings=tuple(
            replace(binding, artifact_hash=digest)
            for binding in base.bindings
        ),
    )


def multi_artifact(backend, module, y_expression, flag_expression, *,
                   identity="selected:multi", y_output="y", flag_output="flag"):
    text = (
        f"module {module}(input [3:0] x, output [3:0] {y_output}, "
        f"output [3:0] {flag_output}); "
        f"assign {y_output} = {y_expression}; "
        f"assign {flag_output} = {flag_expression}; endmodule\n"
    )
    digest = hashlib.sha256(text.encode()).hexdigest()
    bindings = (
        EquivalenceBinding(2, BindingSide.IMPLEMENTATION, "port:x", identity,
                           module, "x", 4, "unsigned", SignalRole.INPUT,
                           None, None, backend, digest),
        EquivalenceBinding(2, BindingSide.IMPLEMENTATION, "port:y", identity,
                           module, y_output, 4, "unsigned", SignalRole.OUTPUT,
                           None, None, backend, digest),
        EquivalenceBinding(2, BindingSide.IMPLEMENTATION, "port:flag", identity,
                           module, flag_output, 4, "unsigned", SignalRole.OUTPUT,
                           None, None, backend, digest),
    )
    return BackendArtifact(backend, module, identity, digest, text, bindings)


class M38Tests(unittest.TestCase):
    def setUp(self):
        self.left = artifact("clash", "ClashALU", "x + 1", output="y")
        self.right = artifact("direct_systemverilog", "SvALU", "x + 1", output="out")
        self.property = CrossBackendProperty("m38.alu", CrossBackendRelation.SAME_CYCLE_VALUE,
                                             "selected:alu", ("port:y",), None, None, 0, 0)

    def test_module_route_rejects_protocol_ports_and_keeps_scalar_wires(self):
        protocol = compile_source(
            """
            module RvPassthrough {
                in rx : rv<u8>
                out tx : rv<u8>
                tx.payload = rx.payload
                tx.valid = rx.valid
                rx.ready = tx.ready
            }
            """,
            include_clash=False,
        ).ir
        with self.assertRaisesRegex(
            CrossBackendError,
            r"protocol-valued top ports: rx \(ready_valid\), tx \(ready_valid\)",
        ):
            validate_module_route(protocol)

        scalar = compile_source(
            "module ScalarWire { in x:u8 out y:u8 y=x }",
            include_clash=False,
        ).ir
        validate_module_route(scalar)

    def test_module_route_rejects_scalar_only_aggregate_protocol(self):
        aggregate = compile_source(
            """
            protocol ScalarBus {
                role producer role consumer
                member value : u8 producer -> consumer
            }
            module AggregateValue {
                interface bus : ScalarBus.consumer
                out y : u8
                y = bus.value
            }
            """,
            include_clash=False,
        ).ir
        self.assertTrue(aggregate.aggregate_protocol_endpoints)
        self.assertTrue(all(port.protocol.value == "wire" for port in aggregate.ports))
        with self.assertRaisesRegex(
            CrossBackendError, "aggregate protocol endpoints or connections"
        ):
            validate_module_route(aggregate)

    def test_alignment_uses_semantic_id_not_rtl_name_and_is_deterministic(self):
        first = emit_cross_backend_miter(self.property, self.left, self.right, inputs=("port:x",))
        self.assertEqual(first, emit_cross_backend_miter(self.property, self.left, self.right, inputs=("port:x",)))
        self.assertIn(".y(left_0)", first)
        self.assertIn(".out(right_0)", first)

    def test_multi_observable_miter_uses_one_dut_pair_and_labeled_assertions(self):
        left = multi_artifact("clash", "ClashMulti", "x + 1", "x ^ 4'hf")
        right = multi_artifact(
            "direct_systemverilog", "SvMulti", "x + 1", "x ^ 4'hf",
            y_output="result", flag_output="status",
        )
        prop = CrossBackendProperty(
            "m38.multi", CrossBackendRelation.SAME_CYCLE_VALUE,
            "selected:multi", ("port:y", "port:flag"), None, None, 0, 0,
        )
        miter = emit_cross_backend_miter(prop, left, right, inputs=("port:x",))
        self.assertEqual(miter.count("ClashMulti left_i("), 1)
        self.assertEqual(miter.count("SvMulti right_i("), 1)
        self.assertNotIn("left_0_i", miter)
        self.assertIn(".y(left_0)", miter)
        self.assertIn(".flag(left_1)", miter)
        self.assertIn(".result(right_0)", miter)
        self.assertIn(".status(right_1)", miter)
        tokens = re.findall(r"assertion (m38_observable_\S+)", miter)
        self.assertEqual(len(tokens), 2)
        self.assertEqual(len(set(tokens)), 2)

    def test_multi_observable_failure_attribution_uses_decoded_values(self):
        left = multi_artifact("clash", "ClashMulti", "x + 1", "x ^ 4'hf")
        right = multi_artifact(
            "direct_systemverilog", "SvMulti", "x + 1", "x ^ 4'he",
            y_output="result", flag_output="status",
        )
        prop = CrossBackendProperty(
            "m38.multi", CrossBackendRelation.SAME_CYCLE_VALUE,
            "selected:multi", ("port:y", "port:flag"), None, None, 0, 0,
        )
        failed_proof = FormalResult(
            prop.id, FormalStatus.FAILED, ProofMode.BMC, "sby", "z3", 4,
            counterexample=Counterexample(
                prop.id,
                values=(
                    ("left:port:y", "4'h2"),
                    ("right:port:y", "4'h2"),
                    ("left:port:flag", "4'hf"),
                    ("right:port:flag", "4'he"),
                ),
                raw_trace="untrusted solver prose",
            ),
        )
        with patch("zlang.formal.run_verilog_formal", return_value=failed_proof):
            result = run_cross_backend_formal(
                prop, left, right, inputs=("port:x",), depth=4
            )
        self.assertEqual(result.observable_signal_id, "port:flag")
        self.assertIsNotNone(result.counterexample)
        self.assertEqual(result.counterexample.semantic_signal_id, "port:flag")
        self.assertEqual(result.counterexample.left_rtl_path, "flag")
        self.assertEqual(result.counterexample.right_rtl_path, "status")

        ambiguous = FormalResult(
            prop.id, FormalStatus.FAILED, ProofMode.BMC, "sby", "z3", 4,
            counterexample=Counterexample(
                prop.id,
                values=(
                    ("left:port:y", "4'h1"),
                    ("right:port:y", "4'h2"),
                    ("left:port:flag", "4'hf"),
                    ("right:port:flag", "4'he"),
                ),
            ),
        )
        with patch("zlang.formal.run_verilog_formal", return_value=ambiguous):
            result = run_cross_backend_formal(
                prop, left, right, inputs=("port:x",), depth=4
            )
        self.assertIsNone(result.observable_signal_id)
        self.assertIsNone(result.counterexample.semantic_signal_id)

        unresolved = FormalResult(
            prop.id, FormalStatus.FAILED, ProofMode.BMC, "sby", "z3", 4,
            counterexample=Counterexample(
                prop.id,
                raw_trace="failed assertion m38_observable_untrusted",
            ),
        )
        with patch("zlang.formal.run_verilog_formal", return_value=unresolved):
            result = run_cross_backend_formal(
                prop, left, right, inputs=("port:x",), depth=4
            )
        self.assertIsNone(result.observable_signal_id)
        self.assertIsNone(result.counterexample.semantic_signal_id)

    def test_single_observable_failure_without_decoded_values_is_unattributed(self):
        failed_proof = FormalResult(
            self.property.id,
            FormalStatus.FAILED,
            ProofMode.BMC,
            "sby",
            "z3",
            4,
            counterexample=Counterexample(
                self.property.id,
                raw_trace="failed assertion text is not a semantic trace",
            ),
        )
        with patch("zlang.formal.run_verilog_formal", return_value=failed_proof):
            result = run_cross_backend_formal(
                self.property, self.left, self.right, inputs=("port:x",), depth=4
            )
        self.assertIsNone(result.observable_signal_id)
        self.assertIsNotNone(result.counterexample)
        self.assertIsNone(result.counterexample.semantic_signal_id)

    def test_real_multi_observable_bmc_checks_one_shared_dut_pair(self):
        left = multi_artifact("clash", "ClashMulti", "x + 1", "x ^ 4'hf")
        right = multi_artifact(
            "direct_systemverilog", "SvMulti", "x + 1", "x ^ 4'hf",
            y_output="result", flag_output="status",
        )
        prop = CrossBackendProperty(
            "m38.multi.real", CrossBackendRelation.SAME_CYCLE_VALUE,
            "selected:multi", ("port:y", "port:flag"), None, None, 0, 0,
        )
        good = run_cross_backend_formal(
            prop, left, right, inputs=("port:x",), depth=4
        )
        if good.status is CrossBackendStatus.SKIPPED:
            return
        self.assertEqual(good.status, CrossBackendStatus.BOUNDED_PASS)

        mutated = multi_artifact(
            "direct_systemverilog", "SvMulti", "x + 1", "x ^ 4'he",
            y_output="result", flag_output="status",
        )
        failed = run_cross_backend_formal(
            prop, left, mutated, inputs=("port:x",), depth=4
        )
        self.assertEqual(failed.status, CrossBackendStatus.FAILED)
        self.assertIsNotNone(failed.counterexample)
        self.assertEqual(failed.observable_signal_id, "port:flag")
        self.assertEqual(failed.counterexample.semantic_signal_id, "port:flag")

    def test_real_bmc_pass_and_mutation_fail_with_metadata(self):
        result = run_cross_backend_formal(self.property, self.left, self.right, inputs=("port:x",), depth=4)
        if result.status is not CrossBackendStatus.SKIPPED:
            self.assertEqual(result.status, CrossBackendStatus.BOUNDED_PASS)
        bad = artifact("direct_systemverilog", "SvALU", "x - 1", output="out")
        failed = run_cross_backend_formal(self.property, self.left, bad, inputs=("port:x",), depth=4)
        if failed.status is not CrossBackendStatus.SKIPPED:
            self.assertEqual(failed.status, CrossBackendStatus.FAILED)
            self.assertIsNotNone(failed.counterexample)
            self.assertEqual(failed.counterexample.left_artifact_hash, self.left.artifact_hash)

    def test_rejects_hash_identity_version_and_observable_mismatches(self):
        tampered = BackendArtifact(self.right.backend, self.right.module, self.right.selected_ir_identity,
                                   "0" * 64, self.right.text, self.right.bindings)
        with self.assertRaisesRegex(CrossBackendError, "hash"):
            validate_artifacts(self.left, tampered, self.property)
        wrong = artifact("direct_systemverilog", "SvALU", "x + 1", identity="other")
        with self.assertRaisesRegex(CrossBackendError, "selected-IR"):
            validate_artifacts(self.left, wrong, self.property)
        missing = artifact("direct_systemverilog", "SvALU", "x + 1")
        missing = BackendArtifact(missing.backend, missing.module, missing.selected_ir_identity,
                                  missing.artifact_hash, missing.text, (missing.bindings[0],))
        with self.assertRaisesRegex(CrossBackendError, "missing observable"):
            validate_artifacts(self.left, missing, self.property)

        legacy_stale = replace(self.right, manifest_version=10)
        with self.assertRaisesRegex(CrossBackendError, "manifest version mismatch"):
            validate_artifacts(self.left, legacy_stale, self.property)

    def test_unavailable_or_empty_physical_binding_fails_closed(self):
        for physical_available, rtl_path in ((False, "y"), (True, "")):
            with self.subTest(
                physical_available=physical_available, rtl_path=rtl_path
            ):
                bindings = tuple(
                    replace(
                        item,
                        physical_available=physical_available,
                        rtl_path=rtl_path,
                    )
                    if item.semantic_signal_id == "port:y"
                    else item
                    for item in self.left.bindings
                )
                unavailable = replace(self.left, bindings=bindings)
                with self.assertRaisesRegex(
                    CrossBackendError,
                    r"physical binding is unavailable.*port:y",
                ):
                    emit_cross_backend_miter(
                        self.property,
                        unavailable,
                        self.right,
                        inputs=("port:x",),
                    )

    def test_fixed_latency_requires_domains_and_equal_selected_latency(self):
        with self.assertRaisesRegex(CrossBackendError, "clock/reset"):
            CrossBackendProperty("m38.t", CrossBackendRelation.FIXED_LATENCY_VALUE,
                                 "selected:alu", ("port:y",), None, None, 0, 1)
        with self.assertRaisesRegex(CrossBackendError, "equal latency"):
            CrossBackendProperty("m38.s", CrossBackendRelation.SAME_CYCLE_VALUE,
                                 "selected:alu", ("port:y",), None, None, 0, 1)
        with self.assertRaisesRegex(CrossBackendError, "equal backend latency"):
            CrossBackendProperty("m38.tdelta", CrossBackendRelation.FIXED_LATENCY_VALUE,
                                 "selected:alu", ("port:y",), "clk", "rst", 1, 2)

    def test_fixed_latency_reset_fill_and_mutation(self):
        left = timed_artifact("clash", "ClashPipe", "x")
        right = timed_artifact("direct_systemverilog", "SvPipe", "x", output="out")
        prop = CrossBackendProperty("m38.pipe", CrossBackendRelation.FIXED_LATENCY_VALUE,
                                    "selected:pipe", ("port:y",), "clk", "rst", 1, 1)
        self.assertEqual(prop.comparison_window.fill_cycles, 1)
        self.assertEqual(prop.comparison_window.minimum_bmc_depth, 5)
        shallow = run_cross_backend_formal(
            prop, left, right, inputs=("port:x",), depth=4
        )
        self.assertEqual(shallow.status, CrossBackendStatus.UNKNOWN)
        self.assertIn("comparison_window_unreached", shallow.reason or "")
        for depth in (5, 6):
            good = run_cross_backend_formal(
                prop, left, right, inputs=("port:x",), depth=depth
            )
            if good.status is not CrossBackendStatus.SKIPPED:
                self.assertEqual(good.status, CrossBackendStatus.BOUNDED_PASS)
        bad = timed_artifact("direct_systemverilog", "SvPipe", "x + 1", output="out")
        failed = run_cross_backend_formal(
            prop, left, bad, inputs=("port:x",), depth=5
        )
        if failed.status is not CrossBackendStatus.SKIPPED:
            self.assertEqual(failed.status, CrossBackendStatus.FAILED)
            self.assertIsNotNone(failed.counterexample)
            assert failed.counterexample is not None
            self.assertIsNotNone(failed.counterexample.cycle)
            assert failed.counterexample.cycle is not None
            self.assertEqual(
                failed.counterexample.sample_cycle,
                failed.counterexample.cycle - prop.comparison_window.fill_cycles,
            )
            values = dict(failed.counterexample.values)
            self.assertEqual(values["reset"], "0")
            self.assertIn("comparison_valid", values)
            self.assertIn("left:port:y", values)
            self.assertIn("right:port:y", values)

    def test_exact_nondefault_domains_drive_miter_edge_polarity_and_release(self):
        domains = (
            ClockDomain(
                "clk", "rst_n", ClockEdge.FALLING,
                ResetMode.SYNCHRONOUS, ResetPolarity.ACTIVE_LOW,
            ),
            ClockDomain(
                "clk", "arst", ClockEdge.RISING,
                ResetMode.ASYNCHRONOUS, ResetPolarity.ACTIVE_HIGH,
            ),
            ClockDomain(
                "clk", "arst_n", ClockEdge.FALLING,
                ResetMode.ASYNCHRONOUS, ResetPolarity.ACTIVE_LOW,
                reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
                reset_release_cycles=2,
            ),
        )
        for index, domain in enumerate(domains):
            with self.subTest(domain=domain):
                left = physical_timed_artifact(
                    "clash", f"ClashPhysical{index}", "x", domain,
                )
                right = physical_timed_artifact(
                    "direct_systemverilog", f"SvPhysical{index}", "x",
                    domain, output="out",
                )
                prop = CrossBackendProperty(
                    f"m38.physical.{index}",
                    CrossBackendRelation.FIXED_LATENCY_VALUE,
                    "selected:physical-pipe",
                    ("port:y",),
                    domain.clock,
                    domain.reset,
                    1,
                    1,
                    clock_domain_contract=domain,
                )
                validate_artifacts(left, right, prop)
                miter = emit_cross_backend_miter(
                    prop, left, right, inputs=("port:x",)
                )
                active_edge = (
                    "posedge" if domain.edge is ClockEdge.RISING else "negedge"
                )
                self.assertIn(f"always @({active_edge} clock", miter)
                self.assertIn(".clock(clock)", miter)
                self.assertIn(".reset(reset)", miter)
                if domain.reset_polarity is ResetPolarity.ACTIVE_LOW:
                    self.assertIn("assume(!reset)", miter)
                    self.assertIn("zlang_formal_reset_active", miter)
                else:
                    self.assertIn("assume(reset)", miter)
                if domain.reset_release_mode is ResetReleaseMode.SYNCHRONIZED:
                    self.assertIn('(* ASYNC_REG = "TRUE" *)', miter)
                    self.assertEqual(prop.comparison_window.reset_release_cycles, 2)
                    self.assertEqual(prop.comparison_window.minimum_bmc_depth, 7)
                else:
                    self.assertEqual(prop.comparison_window.reset_release_cycles, 0)

    def test_private_trace_names_cannot_collide_with_public_miter_inputs(self):
        domain = ClockDomain(
            "clk", "arst_n", ClockEdge.FALLING,
            ResetMode.ASYNCHRONOUS, ResetPolarity.ACTIVE_LOW,
            reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
            reset_release_cycles=2,
        )
        input_semantics = (
            "comparison_valid",
            "zlang_formal_reset_active",
            "zlang_formal_reset_release",
            "left_0",
            "right_0",
        )

        def colliding_artifact(backend: str, module: str, output: str):
            inputs_text = ", ".join(
                f"input [3:0] {name}" for name in input_semantics
            )
            text = (
                f"module {module}(input clock, input reset, {inputs_text}, "
                f"output [3:0] {output});\n"
                f"  reg [3:0] q; always @(negedge clock or negedge reset) "
                f"if (!reset) q <= 0; else q <= comparison_valid;\n"
                f"  assign {output} = q; endmodule\n"
            )
            digest = hashlib.sha256(text.encode()).hexdigest()
            version = PHYSICAL_DOMAIN_MANIFEST_VERSION
            bindings = tuple(
                EquivalenceBinding(
                    version, BindingSide.IMPLEMENTATION, semantic,
                    "selected:collision", module, semantic, 4, "unsigned",
                    SignalRole.INPUT, domain.clock, domain.reset, backend,
                    digest,
                )
                for semantic in input_semantics
            ) + (
                EquivalenceBinding(
                    version, BindingSide.IMPLEMENTATION, "port:y",
                    "selected:collision", module, output, 4, "unsigned",
                    SignalRole.OUTPUT, domain.clock, domain.reset, backend,
                    digest,
                ),
                EquivalenceBinding(
                    version, BindingSide.IMPLEMENTATION, "clock",
                    "selected:collision", module, "clock", 1, "bit",
                    SignalRole.CLOCK, domain.clock, domain.reset, backend,
                    digest,
                ),
                EquivalenceBinding(
                    version, BindingSide.IMPLEMENTATION, "reset",
                    "selected:collision", module, "reset", 1, "bit",
                    SignalRole.RESET, domain.clock, domain.reset, backend,
                    digest,
                ),
            )
            physical = PhysicalDomainManifest.publish(
                domain,
                rtl_module=module,
                rtl_clock_path="clock",
                rtl_reset_path="reset",
            )
            return BackendArtifact(
                backend, module, "selected:collision", digest, text, bindings,
                manifest_version=version,
                physical_domains=(physical,),
            )

        left = colliding_artifact("clash", "ClashCollision", "y")
        right = colliding_artifact(
            "direct_systemverilog", "SvCollision", "out"
        )
        prop = CrossBackendProperty(
            "m38.private-collision",
            CrossBackendRelation.FIXED_LATENCY_VALUE,
            "selected:collision",
            ("port:y",),
            domain.clock,
            domain.reset,
            1,
            1,
            clock_domain_contract=domain,
        )
        emission = emit_cross_backend_miter_with_metadata(
            prop, left, right, inputs=input_semantics,
        )
        metadata = emission.trace_metadata
        assert metadata.reset is not None
        assert metadata.comparison_valid is not None
        self.assertNotEqual(metadata.reset, "zlang_formal_reset_active")
        self.assertNotEqual(metadata.comparison_valid, "comparison_valid")
        self.assertRegex(
            emission.source,
            r"reg \[1:0\] zlang_formal_reset_release_[0-9a-f]{8};",
        )
        left_name, right_name = metadata.observable_names[0][1:]
        self.assertNotEqual(left_name, "left_0")
        self.assertNotEqual(right_name, "right_0")

        proof = FormalResult(
            prop.id,
            FormalStatus.FAILED,
            ProofMode.BMC,
            "sby",
            "z3",
            7,
            counterexample=Counterexample(
                prop.id,
                cycle=6,
                values=(("reset", "1"),),
                raw_trace="trace",
            ),
        )
        with patch(
            "zlang.formal.run_verilog_formal", return_value=proof
        ) as solver:
            result = run_cross_backend_formal(
                prop, left, right, inputs=input_semantics, depth=7,
            )
        trace_names = {
            item.semantic_signal_id: item.rtl_name
            for item in solver.call_args.kwargs["trace_bindings"]
        }
        self.assertEqual(trace_names["reset"], metadata.reset)
        self.assertEqual(
            trace_names["comparison_valid"], metadata.comparison_valid
        )
        self.assertEqual(trace_names["left:port:y"], left_name)
        self.assertEqual(trace_names["right:port:y"], right_name)
        assert result.counterexample is not None
        self.assertEqual(result.counterexample.values, (("reset", "1"),))

    def test_exact_nondefault_same_cycle_value_route(self):
        domain = ClockDomain(
            "clk", "arst_n", ClockEdge.FALLING,
            ResetMode.ASYNCHRONOUS, ResetPolarity.ACTIVE_LOW,
        )
        left = physical_value_artifact(
            "clash", "ClashPhysicalValue", "x + 1", domain,
        )
        right = physical_value_artifact(
            "direct_systemverilog", "SvPhysicalValue", "x + 1", domain,
            output="out",
        )
        prop = CrossBackendProperty(
            "m38.physical.value",
            CrossBackendRelation.SAME_CYCLE_VALUE,
            "selected:physical-value",
            ("port:y",),
            None,
            None,
            0,
            0,
            clock_domain_contract=domain,
        )
        self.assertEqual(prop.clock_domain, "clk")
        self.assertEqual(prop.reset_domain, "arst_n")
        miter = emit_cross_backend_miter(
            prop, left, right, inputs=("port:x",)
        )
        self.assertIn("input logic clock", miter)
        self.assertIn(".reset(reset)", miter)
        self.assertNotIn("comparison_valid", miter)
        good = run_cross_backend_formal(
            prop, left, right, inputs=("port:x",), depth=4
        )
        if good.status is CrossBackendStatus.SKIPPED:
            return
        self.assertEqual(good.status, CrossBackendStatus.BOUNDED_PASS)

    def test_physical_contract_must_be_exact_and_property_owned(self):
        domain = ClockDomain(
            "clk", "arst", reset_mode=ResetMode.ASYNCHRONOUS,
        )
        left = physical_timed_artifact("clash", "ClashPhysical", "x", domain)
        right = physical_timed_artifact(
            "direct_systemverilog", "SvPhysical", "x", domain, output="out"
        )
        missing = CrossBackendProperty(
            "m38.physical.missing", CrossBackendRelation.SAME_CYCLE_VALUE,
            "selected:physical-pipe", ("port:y",), None, None, 0, 0,
        )
        with self.assertRaisesRegex(
            CrossBackendError, "missing the exact physical"
        ):
            validate_artifacts(left, right, missing)

        other = ClockDomain(
            "clk", "arst", ClockEdge.RISING, ResetMode.ASYNCHRONOUS,
            ResetPolarity.ACTIVE_LOW,
        )
        mismatched = replace(
            right,
            physical_domains=(PhysicalDomainManifest.publish(
                other,
                rtl_module=right.module,
                rtl_clock_path="clock",
                rtl_reset_path="reset",
            ),),
        )
        prop = CrossBackendProperty(
            "m38.physical.mismatch",
            CrossBackendRelation.FIXED_LATENCY_VALUE,
            "selected:physical-pipe",
            ("port:y",),
            domain.clock,
            domain.reset,
            1,
            1,
            clock_domain_contract=domain,
        )
        with self.assertRaisesRegex(
            CrossBackendError, "contract disagrees"
        ):
            validate_artifacts(left, mismatched, prop)

        power_up = ClockDomain(
            "clk", "rst", power_up=PowerUpPolicy.RESET,
        )
        with self.assertRaisesRegex(CrossBackendError, "power_up"):
            CrossBackendProperty(
                "m38.physical.power-up",
                CrossBackendRelation.FIXED_LATENCY_VALUE,
                "selected:physical-pipe",
                ("port:y",),
                "clk",
                "rst",
                1,
                1,
                clock_domain_contract=power_up,
            )

    def test_real_async_fixed_latency_pass_and_mutation(self):
        domain = ClockDomain(
            "clk", "arst_n", ClockEdge.FALLING,
            ResetMode.ASYNCHRONOUS, ResetPolarity.ACTIVE_LOW,
            reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
            reset_release_cycles=2,
        )
        left = physical_timed_artifact("clash", "ClashAsync", "x", domain)
        right = physical_timed_artifact(
            "direct_systemverilog", "SvAsync", "x", domain, output="out"
        )
        prop = CrossBackendProperty(
            "m38.async.real",
            CrossBackendRelation.FIXED_LATENCY_VALUE,
            "selected:physical-pipe",
            ("port:y",),
            "clk",
            "arst_n",
            1,
            1,
            clock_domain_contract=domain,
        )
        good = run_cross_backend_formal(
            prop, left, right, inputs=("port:x",), depth=8
        )
        if good.status is CrossBackendStatus.SKIPPED:
            return
        self.assertEqual(good.status, CrossBackendStatus.BOUNDED_PASS)
        self.assertEqual(
            good.manifest_version, PHYSICAL_DOMAIN_MANIFEST_VERSION
        )

        bad = physical_timed_artifact(
            "direct_systemverilog", "SvAsync", "x + 1", domain,
            output="out",
        )
        failed = run_cross_backend_formal(
            prop, left, bad, inputs=("port:x",), depth=8
        )
        self.assertEqual(failed.status, CrossBackendStatus.FAILED)
        self.assertIsNotNone(failed.counterexample)
        values = dict(failed.counterexample.values)
        self.assertEqual(values.get("reset"), "0")
        self.assertIn("physical_reset", values)


if __name__ == "__main__":
    unittest.main()
