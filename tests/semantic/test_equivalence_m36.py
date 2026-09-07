import unittest
from unittest.mock import patch

from zlang.compiler import compile_source
from zlang.equivalence import (
    artifact_hash, emit_miter, emit_miter_with_metadata, emit_reference_model,
    make_equivalence_property, publish_bindings, run_equivalence_formal,
    unavailable_result,
)
from zlang.ir import (
    BindingMap, BindingSide, EquivalenceBinding, EquivalenceError,
    ClockDomain,
    EquivalenceMode, EquivalenceRelation, EquivalenceStatus, InputRef,
    Constant, Pipeline, ResetReleaseMode, SIntType, SignalRole, UIntType,
)
from zlang.ir.cdc import ClockEdge, PowerUpPolicy, ResetMode, ResetPolarity
from zlang.ir.formal import Counterexample, FormalResult, FormalStatus, ProofMode
from zlang.timing import TimingInfo


class EquivalenceM36Tests(unittest.TestCase):
    def setUp(self):
        self.u8 = UIntType(8)
        self.x = InputRef("x", self.u8)

    def bindings(self, *, selected="source", ref_hash="ref", impl_hash="impl", output="y"):
        entries = []
        for side, backend, module, path, artifact in (
            (BindingSide.REFERENCE, "zlang-reference", "Ref", "y", ref_hash),
            (BindingSide.IMPLEMENTATION, "clash", "Impl", "y", impl_hash),
        ):
            entries.append(EquivalenceBinding(2, side, "x", selected, module, "x", 8, "unsigned", SignalRole.INPUT, "clk", "rst", backend, artifact))
            entries.append(EquivalenceBinding(2, side, "y", selected, module, path, 8, "unsigned", SignalRole.OUTPUT, "clk", "rst", backend, artifact))
            entries.append(EquivalenceBinding(2, side, "clock", selected, module, "clk", 1, "bit", SignalRole.CLOCK, "clk", "rst", backend, artifact))
            entries.append(EquivalenceBinding(2, side, "reset", selected, module, "rst", 1, "bit", SignalRole.RESET, "clk", "rst", backend, artifact))
        return BindingMap(tuple(entries))

    def test_same_cycle_reference_model_and_miter_are_deterministic(self):
        prop = make_equivalence_property(self.x, self.x, candidate_class="m27", reference_root="r", implementation_root="i", inputs=("x",), reference_output="y", implementation_output="y")
        reference = emit_reference_model("Ref", "y", self.u8, (("x", self.u8),), self.x)
        first = emit_miter(prop, self.bindings(), reference_module="Ref", implementation_module="Impl")
        self.assertEqual(first, emit_miter(prop, self.bindings(), reference_module="Ref", implementation_module="Impl"))
        self.assertIn("always_comb assert", first)
        self.assertIn("assign y = x", reference)

    def test_reference_model_renders_negative_signed_literal_legally(self):
        reference = emit_reference_model(
            "SignedLiteralReference",
            "y",
            SIntType(8),
            (),
            Constant(-1, SIntType(8)),
        )

        self.assertIn("assign y = -8'sd1;", reference)
        self.assertNotIn("8'sd-1", reference)

    def test_reference_model_materializes_shared_generic_call(self):
        compilation = compile_source("""
            fn identity<type T>(x : T) { x }
            module GenericReference {
                in x : u8
                out y : u8
                y = identity(x)
            }
        """, include_clash=False)
        module = compilation.ir
        assignment = module.assignments[0]

        reference = emit_reference_model(
            "RefGeneric",
            "y",
            assignment.expression.type,
            (("x", self.u8),),
            assignment.expression,
            callable_definitions=(
                *module.functions,
                *module.callable_definitions,
            ),
        )

        self.assertIn("assign y = x", reference)
        self.assertNotIn("identity", reference)

    def test_reference_model_materializes_compact_functional_reduction(self):
        compilation = compile_source("""
            module RegionReference {
                in x : vec<32,u8>
                out y : u13
                y = sum(generate(i in 0..32) x[i])
            }
        """, include_clash=False)
        module = compilation.ir
        assignment = module.assignments[0]

        reference = emit_reference_model(
            "RefRegion",
            "y",
            assignment.expression.type,
            (("x", module.ports[0].type),),
            assignment.expression,
            callable_definitions=module.callable_definitions,
        )

        self.assertNotIn("FunctionalRegion", reference)
        self.assertEqual(reference.count(" + "), 31)

    def test_m29_and_m32_classes_are_same_cycle(self):
        for candidate_class in ("m29", "m32", "value"):
            prop = make_equivalence_property(self.x, self.x, candidate_class=candidate_class, reference_root="r", implementation_root=candidate_class, inputs=("x",))
            self.assertEqual(prop.relation_kind, EquivalenceRelation.SAME_CYCLE_VALUE)

    def test_pipeline_delta_one_and_multi_stage_use_fill_history(self):
        for stages in (1, 3):
            implementation = Pipeline(stages, self.x, 1, self.u8)
            prop = make_equivalence_property(self.x, implementation, candidate_class="m31", reference_root="r", implementation_root=f"p{stages}", inputs=("x",), reference_output="y", implementation_output="y", reference_timing=TimingInfo(0, 1, "clk", "rst"), implementation_timing=TimingInfo(stages, 1, "clk", "rst"))
            self.assertEqual(prop.relation_kind, EquivalenceRelation.FIXED_LATENCY_VALUE)
            self.assertEqual(prop.latency_delta, stages)
            text = emit_miter(prop, self.bindings(selected=f"p{stages}"), reference_module="Ref", implementation_module="Impl")
            self.assertIn("sample_valid", text)
            self.assertIn("if (reset)", text)

    def test_fixed_latency_bmc_requires_a_reachable_comparison_window(self):
        implementation = Pipeline(2, self.x, 1, self.u8)
        prop = make_equivalence_property(
            self.x,
            implementation,
            candidate_class="m31",
            reference_root="r",
            implementation_root="p2",
            reference_timing=TimingInfo(0, 1, "clk", "rst"),
            implementation_timing=TimingInfo(2, 1, "clk", "rst"),
        )
        self.assertEqual(prop.comparison_window.fill_cycles, 2)
        self.assertEqual(prop.comparison_window.first_comparison_cycle, 4)
        self.assertEqual(prop.comparison_window.minimum_bmc_depth, 6)

        with patch("zlang.formal.run_verilog_formal") as solver:
            shallow = run_equivalence_formal(
                prop, "unused", top="m36", depth=5
            )
        solver.assert_not_called()
        self.assertEqual(shallow.status, EquivalenceStatus.UNKNOWN)
        self.assertIn("comparison_window_unreached", shallow.reason or "")

        for depth in (6, 7):
            with self.subTest(depth=depth), patch(
                "zlang.formal.run_verilog_formal",
                return_value=FormalResult(
                    prop.id,
                    FormalStatus.BOUNDED_PASS,
                    ProofMode.BMC,
                    "sby",
                    "z3",
                    depth,
                ),
            ) as solver:
                result = run_equivalence_formal(
                    prop, "module m36; endmodule", top="m36", depth=depth
                )
            solver.assert_called_once()
            self.assertEqual(result.status, EquivalenceStatus.BOUNDED_PASS)

        with patch(
            "zlang.formal.run_verilog_formal",
            return_value=FormalResult(
                prop.id,
                FormalStatus.PROVEN,
                ProofMode.PROVE,
                "sby",
                "z3",
                1,
            ),
        ) as solver:
            proven = run_equivalence_formal(
                prop,
                "module m36; endmodule",
                top="m36",
                mode=EquivalenceMode.PROVE,
                depth=1,
            )
        solver.assert_called_once()
        self.assertEqual(proven.status, EquivalenceStatus.PROVEN)

    def test_fixed_latency_miter_uses_exact_raw_async_domain(self):
        implementation = Pipeline(2, self.x, 1, self.u8)
        domain = ClockDomain(
            "clk",
            "rst_n",
            edge=ClockEdge.FALLING,
            reset_mode=ResetMode.ASYNCHRONOUS,
            reset_polarity=ResetPolarity.ACTIVE_LOW,
        )
        prop = make_equivalence_property(
            self.x,
            implementation,
            candidate_class="m31",
            reference_root="r",
            implementation_root="p2-async-low",
            inputs=("x",),
            reference_output="y",
            implementation_output="y",
            reference_timing=TimingInfo(0, 1, "clk", "rst_n"),
            implementation_timing=TimingInfo(2, 1, "clk", "rst_n"),
            clock_domain_contract=domain,
        )

        text = emit_miter(
            prop,
            self.bindings(selected="p2-async-low"),
            reference_module="Ref",
            implementation_module="Impl",
        )

        self.assertIn("initial begin", text)
        self.assertIn("assume(!reset);", text)
        self.assertIn("assign zlang_formal_reset_active = !reset;", text)
        self.assertIn(
            "always_ff @(negedge clock or negedge reset) begin", text
        )
        # The raw external assertion is intentionally the first async branch;
        # this is the exact form Yosys recognizes as an async-reset flop.
        self.assertIn("if (!reset) begin", text)
        self.assertIn(
            "always_ff @(negedge clock) if (!zlang_formal_reset_active",
            text,
        )
        self.assertEqual(prop.comparison_window.reset_release_cycles, 0)

    def test_synchronized_release_delays_window_not_sample_attribution(self):
        implementation = Pipeline(2, self.x, 1, self.u8)
        domain = ClockDomain(
            "clk",
            "arst",
            reset_mode=ResetMode.ASYNCHRONOUS,
            reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
            reset_release_cycles=2,
        )
        prop = make_equivalence_property(
            self.x,
            implementation,
            candidate_class="m31",
            reference_root="r",
            implementation_root="p2-safe-async",
            inputs=("x",),
            reference_output="y",
            implementation_output="y",
            reference_timing=TimingInfo(0, 1, "clk", "arst"),
            implementation_timing=TimingInfo(2, 1, "clk", "arst"),
            clock_domain_contract=domain,
        )
        self.assertEqual(prop.comparison_window.fill_cycles, 2)
        self.assertEqual(prop.comparison_window.reset_release_cycles, 2)
        self.assertEqual(prop.comparison_window.first_comparison_cycle, 6)
        self.assertEqual(prop.comparison_window.minimum_bmc_depth, 8)

        text = emit_miter(
            prop,
            self.bindings(selected="p2-safe-async"),
            reference_module="Ref",
            implementation_module="Impl",
        )
        self.assertIn('(* ASYNC_REG = "TRUE" *)', text)
        self.assertIn("always @(posedge clock or posedge reset)", text)
        self.assertIn("assign zlang_formal_reset_active", text)

        with patch("zlang.formal.run_verilog_formal") as solver:
            shallow = run_equivalence_formal(
                prop, text, top="m36", depth=7
            )
        solver.assert_not_called()
        self.assertEqual(shallow.status, EquivalenceStatus.UNKNOWN)
        self.assertIn("2 reset-release cycle(s)", shallow.reason or "")

        proof = FormalResult(
            prop.id,
            FormalStatus.FAILED,
            ProofMode.BMC,
            "sby",
            "z3",
            8,
            counterexample=Counterexample(prop.id, cycle=7, raw_trace="trace"),
        )
        with patch(
            "zlang.formal.run_verilog_formal", return_value=proof
        ) as solver:
            result = run_equivalence_formal(
                prop, text, top="m36", depth=8
            )
        assert result.counterexample is not None
        # Reset release shifts the absolute witness window, but the originating
        # sample relation remains the exact two-cycle implementation latency.
        self.assertEqual(result.counterexample.sample_cycle, 5)
        trace_bindings = solver.call_args.kwargs["trace_bindings"]
        reset_binding = next(
            item for item in trace_bindings if item.semantic_signal_id == "reset"
        )
        self.assertEqual(reset_binding.rtl_name, "zlang_formal_reset_active")

    def test_timed_trace_uses_collision_safe_names_from_the_emitted_miter(self):
        implementation = Pipeline(2, self.x, 1, self.u8)
        domain = ClockDomain(
            "clk",
            "arst_n",
            edge=ClockEdge.FALLING,
            reset_mode=ResetMode.ASYNCHRONOUS,
            reset_polarity=ResetPolarity.ACTIVE_LOW,
            reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
            reset_release_cycles=2,
        )
        prop = make_equivalence_property(
            self.x,
            implementation,
            candidate_class="m31",
            reference_root="r",
            implementation_root="p2-collision",
            inputs=("x", "release_collision"),
            reference_output="y",
            implementation_output="y",
            reference_timing=TimingInfo(0, 1, "clk", "arst_n"),
            implementation_timing=TimingInfo(2, 1, "clk", "arst_n"),
            clock_domain_contract=domain,
        )
        entries = []
        for side, backend, module, artifact in (
            (BindingSide.REFERENCE, "zlang-reference", "Ref", "ref"),
            (BindingSide.IMPLEMENTATION, "clash", "Impl", "impl"),
        ):
            input_paths = (
                "zlang_formal_reset_active",
                "zlang_formal_reset_release",
            ) if side is BindingSide.REFERENCE else ("impl_x", "impl_release")
            for semantic, path in zip(prop.inputs, input_paths, strict=True):
                entries.append(EquivalenceBinding(
                    2, side, semantic, "p2-collision", module, path, 8,
                    "unsigned", SignalRole.INPUT, "clk", "arst_n", backend,
                    artifact,
                ))
            entries.extend((
                EquivalenceBinding(
                    2, side, "y", "p2-collision", module, "y", 8,
                    "unsigned", SignalRole.OUTPUT, "clk", "arst_n", backend,
                    artifact,
                ),
                EquivalenceBinding(
                    2, side, "clock", "p2-collision", module, "clk", 1,
                    "bit", SignalRole.CLOCK, "clk", "arst_n", backend,
                    artifact,
                ),
                EquivalenceBinding(
                    2, side, "reset", "p2-collision", module, "arst_n", 1,
                    "bit", SignalRole.RESET, "clk", "arst_n", backend,
                    artifact,
                ),
            ))
        emission = emit_miter_with_metadata(
            prop,
            BindingMap(tuple(entries)),
            reference_module="Ref",
            implementation_module="Impl",
        )

        assert emission.trace_metadata.reset is not None
        self.assertNotEqual(
            emission.trace_metadata.reset, "zlang_formal_reset_active"
        )
        self.assertIn(
            f"wire {emission.trace_metadata.reset};", emission.source
        )
        self.assertRegex(
            emission.source,
            r"reg \[1:0\] zlang_formal_reset_release_[0-9a-f]{8};",
        )

        proof = FormalResult(
            prop.id,
            FormalStatus.FAILED,
            ProofMode.BMC,
            "sby",
            "z3",
            8,
            counterexample=Counterexample(
                prop.id,
                cycle=7,
                values=(("reset", "1"),),
                raw_trace="trace",
            ),
        )
        with patch(
            "zlang.formal.run_verilog_formal", return_value=proof
        ) as solver:
            result = run_equivalence_formal(
                prop,
                emission.source,
                top="m36_collision",
                depth=8,
                trace_metadata=emission.trace_metadata,
            )
        reset_binding = next(
            item for item in solver.call_args.kwargs["trace_bindings"]
            if item.semantic_signal_id == "reset"
        )
        self.assertEqual(
            reset_binding.rtl_name, emission.trace_metadata.reset
        )
        assert result.counterexample is not None
        self.assertEqual(result.counterexample.values, (("reset", "1"),))

    def test_exact_domain_metadata_rejects_mismatch_and_power_up(self):
        implementation = Pipeline(1, self.x, 1, self.u8)
        with self.assertRaisesRegex(EquivalenceError, "does not match"):
            make_equivalence_property(
                self.x,
                implementation,
                candidate_class="m31",
                reference_root="r",
                implementation_root="bad-domain",
                reference_timing=TimingInfo(0, 1, "clk", "rst"),
                implementation_timing=TimingInfo(1, 1, "clk", "rst"),
                clock_domain_contract=ClockDomain("other", "rst"),
            )
        with self.assertRaisesRegex(EquivalenceError, "power_up"):
            make_equivalence_property(
                self.x,
                implementation,
                candidate_class="m31",
                reference_root="r",
                implementation_root="power-up",
                reference_timing=TimingInfo(0, 1, "clk", "rst"),
                implementation_timing=TimingInfo(1, 1, "clk", "rst"),
                clock_domain_contract=ClockDomain(
                    "clk", "rst", power_up=PowerUpPolicy.RESET
                ),
            )

    def test_binding_publication_accepts_exact_nondefault_domain(self):
        module = compile_source(
            """
            module AsyncBindings {
                clock clk
                async reset arst_n @clk { polarity active_low }
                in x : u8
                out y : u8
                y = x
            }
            """,
            include_clash=False,
        ).ir
        names = {"port:x": "x", "port:y": "y", "clock": "clk", "reset": "arst_n"}
        published = publish_bindings(
            module,
            side=BindingSide.IMPLEMENTATION,
            selected_ir_identity="async-bindings",
            backend="direct_sv",
            artifact_hash_value="artifact",
            rtl_names=names,
        )
        self.assertEqual(
            tuple(item.semantic_signal_id for item in published),
            ("port:x", "port:y", "clock", "reset"),
        )

    def test_binding_publication_rejects_protocol_top_port_bases(self):
        module = compile_source(
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
        names = {
            "port:rx": "rx",
            "port:tx": "tx",
            "port:rx.payload": "rx_payload",
            "port:rx.valid": "rx_valid",
            "port:rx.ready": "rx_ready",
            "port:tx.payload": "tx_payload",
            "port:tx.valid": "tx_valid",
            "port:tx.ready": "tx_ready",
        }

        with self.assertRaisesRegex(
            EquivalenceError,
            r"protocol-valued top ports: rx \(ready_valid\), tx \(ready_valid\)",
        ):
            publish_bindings(
                module,
                side=BindingSide.IMPLEMENTATION,
                selected_ir_identity="rv-passthrough",
                backend="direct_systemverilog",
                artifact_hash_value="artifact",
                rtl_names=names,
            )

    def test_binding_publication_rejects_scalar_only_aggregate_protocol(self):
        module = compile_source(
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
        self.assertTrue(module.aggregate_protocol_endpoints)
        self.assertTrue(all(port.protocol.value == "wire" for port in module.ports))
        with self.assertRaisesRegex(
            EquivalenceError, "aggregate protocol endpoints or connections"
        ):
            publish_bindings(
                module,
                side=BindingSide.IMPLEMENTATION,
                selected_ir_identity="aggregate-value",
                backend="direct_systemverilog",
                artifact_hash_value="artifact",
                rtl_names={"port:bus__value": "bus__value", "port:y": "y"},
            )

    def test_failed_equivalence_preserves_decoded_cycle_window_and_values(self):
        implementation = Pipeline(2, self.x, 1, self.u8)
        prop = make_equivalence_property(
            self.x,
            implementation,
            candidate_class="m31",
            reference_root="r",
            implementation_root="p2",
            reference_timing=TimingInfo(0, 1, "clk", "rst"),
            implementation_timing=TimingInfo(2, 1, "clk", "rst"),
        )
        proof = FormalResult(
            prop.id,
            FormalStatus.FAILED,
            ProofMode.BMC,
            "sby",
            "z3",
            6,
            counterexample=Counterexample(
                prop.id,
                cycle=5,
                values=(("reference_output", "7"), ("implementation_output", "8")),
                raw_trace="trace",
            ),
        )
        with patch(
            "zlang.formal.run_verilog_formal", return_value=proof
        ) as solver:
            result = run_equivalence_formal(
                prop, "module m36; endmodule", top="m36", depth=6
            )

        assert result.counterexample is not None
        self.assertEqual(result.counterexample.failure_cycle, 5)
        self.assertEqual(result.counterexample.sample_cycle, 3)
        self.assertEqual(result.counterexample.values, proof.counterexample.values)
        kwargs = solver.call_args.kwargs
        self.assertEqual(kwargs["comparison_window"], prop.comparison_window)
        self.assertEqual(
            tuple(binding.semantic_signal_id for binding in kwargs["trace_bindings"]),
            (
                "reference_output",
                "implementation_output",
                "reset",
                "comparison_valid",
            ),
        )

    def test_pipeline_rejects_ii_clock_reset_and_negative_latency(self):
        implementation = Pipeline(1, self.x, 1, self.u8)
        with self.assertRaisesRegex(EquivalenceError, "II=1"):
            make_equivalence_property(self.x, implementation, candidate_class="m31", reference_root="r", implementation_root="i", reference_timing=TimingInfo(0, 2, "clk", "rst"), implementation_timing=TimingInfo(1, 1, "clk", "rst"))
        with self.assertRaisesRegex(EquivalenceError, "matching clock/reset"):
            make_equivalence_property(self.x, implementation, candidate_class="m31", reference_root="r", implementation_root="i", reference_timing=TimingInfo(0, 1, "a", "rst"), implementation_timing=TimingInfo(1, 1, "b", "rst"))
        with self.assertRaisesRegex(EquivalenceError, "precede"):
            make_equivalence_property(implementation, self.x, candidate_class="m31", reference_root="r", implementation_root="i", reference_timing=TimingInfo(1, 1, "clk", "rst"), implementation_timing=TimingInfo(0, 1, "clk", "rst"))

    def test_binding_width_signedness_artifact_duplicate_and_missing_diagnostics(self):
        with self.assertRaisesRegex(EquivalenceError, "type/role mismatch"):
            bad = list(self.bindings().entries)
            bad[next(index for index, item in enumerate(bad) if item.side is BindingSide.IMPLEMENTATION and item.semantic_signal_id == "y")] = EquivalenceBinding(2, BindingSide.IMPLEMENTATION, "y", "source", "Impl", "y", 7, "unsigned", SignalRole.OUTPUT, "clk", "rst", "clash", "impl")
            BindingMap(tuple(bad)).validate()
        with self.assertRaisesRegex(EquivalenceError, "type"):
            from zlang.ir.types import SIntType
            make_equivalence_property(self.x, InputRef("x", SIntType(8)), candidate_class="m29", reference_root="r", implementation_root="i")
        with self.assertRaisesRegex(EquivalenceError, "duplicate"):
            item = self.bindings().entries[0]
            BindingMap((item, item)).validate()
        with self.assertRaisesRegex(EquivalenceError, "artifact mismatch"):
            bad = list(self.bindings(ref_hash="same", impl_hash="same").entries)
            BindingMap(tuple(bad)).validate()

    def test_result_statuses_and_missing_solver(self):
        prop = make_equivalence_property(self.x, self.x, candidate_class="m27", reference_root="r", implementation_root="i")
        result = unavailable_result(prop, backend="clash", reason="solver unavailable", mode=EquivalenceMode.BMC, depth=10)
        self.assertEqual(result.status, EquivalenceStatus.SKIPPED)
        from zlang.ir.equivalence import classify_equivalence
        bounded = classify_equivalence(property_id="p", mode=EquivalenceMode.BMC, outcome="pass", relation_kind=EquivalenceRelation.SAME_CYCLE_VALUE, latency_delta=0, backend="clash", reference_hash="r", implementation_hash="i", binding_map_version=2, candidate_identity="i", depth=8)
        self.assertEqual(bounded.status, EquivalenceStatus.BOUNDED_PASS)
        proven = classify_equivalence(property_id="p", mode=EquivalenceMode.PROVE, outcome="pass", relation_kind=EquivalenceRelation.SAME_CYCLE_VALUE, latency_delta=0, backend="clash", reference_hash="r", implementation_hash="i", binding_map_version=2, candidate_identity="i", depth=8)
        self.assertEqual(proven.status, EquivalenceStatus.PROVEN)


if __name__ == "__main__":
    unittest.main()
