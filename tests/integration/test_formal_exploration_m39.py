from concurrent.futures import ThreadPoolExecutor
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from threading import Barrier
from unittest.mock import patch

from zlang.common import stable_digest
from zlang.formal_exploration import (
    FormalExplorationConfig, FormalExplorationError, FormalPolicy,
    gate_candidates,
)
from zlang.formal import run_verilog_formal
from zlang.equivalence import formal_tools_available
from zlang.formal_candidate import M36DirectSystemVerilogCandidateVerifier
from zlang.candidate_sites import candidate_formal_record_sites
from zlang.ir.equivalence import EquivalenceCounterexample
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.ir.cdc import ClockDomain
from zlang.compiler import compile_source


def space(names):
    candidates = tuple(SimpleNamespace(implementation_identity=name,
                                       semantic_identity="root", timing_relation=None)
                       for name in names)
    evaluations = tuple(SimpleNamespace(candidate=item, legal=True,
                                        objective_key=(index,))
                        for index, item in enumerate(candidates))
    return candidates, evaluations


def _bound_property(candidate):
    return f"m36.test.{candidate.implementation_identity}"


def _m39_result_entries(directory):
    return tuple((Path(directory) / "M39" / "results").glob("*.json"))


class BoundVerifier:
    """Explicit identity-bound seam for non-tool orchestration tests."""

    formal_route = "M36_direct_systemverilog"

    def __init__(self, callback):
        self.callback = callback

    def _identity(self, candidate):
        implementation = getattr(candidate, "artifact_hash", None)
        if not isinstance(implementation, str) or len(implementation) != 64:
            implementation = "b" * 64
        return {
            "property_identity": _bound_property(candidate),
            "reference_artifact_hash": "a" * 64,
            "implementation_artifact_hash": implementation,
            "artifact_hash": implementation,
            "harness_hash": "c" * 64,
            "backend_identity": "d" * 64,
            "assumptions_identity": "e" * 64,
        }

    def cache_identity(self, candidate, config):
        return self._identity(candidate)

    def __call__(self, candidate, config):
        result = dict(self.callback(candidate, config))
        identity = self._identity(candidate)
        result.setdefault("mode", (
            ProofMode.PROVE
            if config.policy is FormalPolicy.REQUIRED_PROVEN
            else ProofMode.BMC
        ))
        result.setdefault("depth", config.bmc_depth)
        result.setdefault("engine", config.engine)
        result.setdefault("solver", config.solver)
        result.setdefault("backend", "direct_systemverilog")
        for key, value in identity.items():
            result.setdefault(key, value)
        status = FormalStatus(result.get("status", FormalStatus.UNKNOWN))
        if status is FormalStatus.FAILED and result.get("counterexample") is None:
            result["counterexample"] = EquivalenceCounterexample(
                identity["property_identity"],
                failure_cycle=1,
                values=(("mutation", "1'b1"),),
                raw_trace="deliberate test counterexample",
            )
        return result


def bound(callback):
    return BoundVerifier(callback)


class M39FormalExplorationTests(unittest.TestCase):

    def test_off_does_not_execute_and_preserves_rank(self):
        candidates, evaluations = space(("cheap", "expensive"))
        called = []
        result = gate_candidates(candidates, evaluations,
                                 FormalExplorationConfig(),
                                 lambda c, config: called.append(c) or {})
        self.assertEqual(tuple(result.eligible), candidates)
        self.assertFalse(called)
        self.assertEqual([r.cache_state for r in result.records], ["not-run", "not-run"])

    def test_available_with_proof_and_without_solver(self):
        candidates, evaluations = space(("cheap", "second"))
        result = gate_candidates(candidates, evaluations,
                                 FormalExplorationConfig(FormalPolicy.AVAILABLE),
                                 bound(lambda c, config: {
                                     "status": FormalStatus.BOUNDED_PASS,
                                     "mode": ProofMode.BMC,
                                 }))
        self.assertEqual(tuple(result.eligible), candidates)
        result = gate_candidates(candidates, evaluations,
                                 FormalExplorationConfig(FormalPolicy.AVAILABLE), None)
        self.assertEqual(tuple(result.eligible), candidates)
        self.assertEqual(result.records[0].status, FormalStatus.SKIPPED)
        self.assertEqual(result.records[0].cache_state, "not-run")

    def test_unbound_injected_success_is_unknown_but_available_is_advisory(self):
        candidates, evaluations = space(("cheap",))
        advisory = gate_candidates(
            candidates,
            evaluations,
            FormalExplorationConfig(FormalPolicy.AVAILABLE),
            lambda *_: {
                "status": FormalStatus.BOUNDED_PASS,
                "mode": ProofMode.BMC,
            },
        )
        record = advisory.records[0]
        self.assertEqual(record.status, FormalStatus.UNKNOWN)
        self.assertEqual(record.cache_state, "invalid")
        self.assertTrue(record.eligible)
        self.assertIn("does not alter static eligibility", record.reason)
        self.assertIn("bound cache identity", record.proof_reason)
        with self.assertRaisesRegex(ValueError, "invalid formal verifier"):
            compile_source(
                "module UnsafeInjection { in a:u8 out y:u8 "
                "y=implement { a ^ 0 intent { minimize lut } } }",
                formal_policy=FormalPolicy.REQUIRED_BMC,
                formal_verifier=lambda *_: {
                    "status": FormalStatus.BOUNDED_PASS,
                    "mode": ProofMode.BMC,
                },
            )

    def test_failed_result_without_counterexample_is_invalid(self):
        candidates, evaluations = space(("broken",))
        identity = bound(lambda *_: {}).cache_identity

        class MissingCounterexample:
            formal_route = "M36_direct_systemverilog"
            cache_identity = staticmethod(identity)

            def __call__(self, candidate, config):
                return {
                    "status": FormalStatus.FAILED,
                    "mode": ProofMode.BMC,
                    "depth": config.bmc_depth,
                    "engine": config.engine,
                    "solver": config.solver,
                    "backend": "direct_systemverilog",
                    **identity(candidate, config),
                }

        result = gate_candidates(
            candidates,
            evaluations,
            FormalExplorationConfig(FormalPolicy.AVAILABLE),
            MissingCounterexample(),
        )
        self.assertEqual(result.records[0].status, FormalStatus.UNKNOWN)
        self.assertTrue(result.records[0].eligible)
        self.assertIn("counterexample", result.records[0].proof_reason)

    def test_non_failed_result_with_counterexample_is_invalid_and_never_cached(self):
        candidates, evaluations = space(("candidate",))
        counterexample = EquivalenceCounterexample(
            _bound_property(candidates[0]),
            failure_cycle=2,
            values=(("unexpected", "1'b1"),),
        )
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            verifier = bound(
                lambda candidate, config: calls.append(candidate) or {
                    "status": FormalStatus.BOUNDED_PASS,
                    "mode": ProofMode.BMC,
                    "counterexample": counterexample,
                }
            )
            config = FormalExplorationConfig(
                FormalPolicy.AVAILABLE,
                cache_directory=Path(directory),
            )

            first = gate_candidates(candidates, evaluations, config, verifier)
            second = gate_candidates(candidates, evaluations, config, verifier)

            self.assertEqual(len(calls), 2)
            self.assertTrue(first.eligible and second.eligible)
            for result in (first, second):
                record = result.records[0]
                self.assertEqual(record.status, FormalStatus.UNKNOWN)
                self.assertEqual(record.cache_state, "invalid")
                self.assertIn("exactly one counterexample", record.proof_reason)
            self.assertFalse(tuple(Path(directory).rglob("*.json")))

    def test_available_executes_only_static_best_candidate(self):
        candidates, evaluations = space(("cheap", "second", "third"))
        calls = []
        result = gate_candidates(
            candidates,
            evaluations,
            FormalExplorationConfig(FormalPolicy.AVAILABLE),
            bound(lambda candidate, config: calls.append(candidate) or {
                "status": FormalStatus.FAILED,
                "mode": ProofMode.BMC,
            }),
        )
        self.assertEqual(calls, [candidates[0]])
        self.assertEqual(result.eligible, candidates)
        self.assertTrue(result.records[0].eligible)
        self.assertIn("advisory", result.records[0].reason)
        self.assertEqual(
            [item.cache_state for item in result.records],
            ["executed", "not-run", "not-run"],
        )


    def test_required_bmc_accepts_bounded_and_rejects_bad_statuses(self):
        candidates, evaluations = space(("cheap", "second"))
        result = gate_candidates(candidates, evaluations,
                                 FormalExplorationConfig(FormalPolicy.REQUIRED_BMC),
                                 bound(lambda c, config: {
                                     "status": FormalStatus.BOUNDED_PASS,
                                     "mode": ProofMode.BMC,
                                 }))
        self.assertEqual(result.eligible[0], candidates[0])
        for status in (FormalStatus.FAILED, FormalStatus.UNKNOWN, FormalStatus.SKIPPED):
            with self.assertRaises(FormalExplorationError):
                gate_candidates(candidates, evaluations,
                                FormalExplorationConfig(FormalPolicy.REQUIRED_BMC),
                                bound(lambda c, config, status=status: {
                                    "status": status, "mode": ProofMode.BMC,
                                }))

    def test_required_unknown_stops_without_runtime_based_fallback(self):
        candidates, evaluations = space(("cheap", "second"))
        calls = []
        with self.assertRaisesRegex(FormalExplorationError, "inconclusive") as raised:
            gate_candidates(
                candidates,
                evaluations,
                FormalExplorationConfig(FormalPolicy.REQUIRED_BMC),
                bound(lambda candidate, config: calls.append(candidate) or {
                    "status": FormalStatus.UNKNOWN,
                    "mode": ProofMode.BMC,
                    "reason": "solver timeout",
                }),
            )
        self.assertEqual(calls, [candidates[0]])
        self.assertEqual(len(raised.exception.records), 1)
        self.assertEqual(raised.exception.records[0].rank, 1)
        self.assertEqual(raised.exception.records[0].status, FormalStatus.UNKNOWN)
        self.assertIn("attempted formal stages", str(raised.exception))

    def test_required_proven_records_bmc_then_prove_and_only_prove_is_eligible(self):
        candidates, evaluations = space(("cheap", "second"))
        calls = []

        def staged(candidate, config):
            calls.append((candidate.implementation_identity, config.policy))
            if config.policy is FormalPolicy.REQUIRED_BMC:
                return {
                    "status": FormalStatus.BOUNDED_PASS,
                    "mode": ProofMode.BMC,
                }
            return {
                "status": FormalStatus.PROVEN,
                "mode": ProofMode.PROVE,
            }

        result = gate_candidates(candidates, evaluations,
                                 FormalExplorationConfig(
                                     FormalPolicy.REQUIRED_PROVEN,
                                     max_formal_candidates=1,
                                 ),
                                 bound(staged))
        self.assertEqual(result.eligible[0], candidates[0])
        self.assertEqual(calls, [
            ("cheap", FormalPolicy.REQUIRED_BMC),
            ("cheap", FormalPolicy.REQUIRED_PROVEN),
        ])
        self.assertEqual(
            [(item.mode, item.status, item.eligible) for item in result.records],
            [
                (ProofMode.BMC, FormalStatus.BOUNDED_PASS, False),
                (ProofMode.PROVE, FormalStatus.PROVEN, True),
            ],
        )
        with self.assertRaises(FormalExplorationError) as raised:
            gate_candidates(candidates, evaluations,
                            FormalExplorationConfig(FormalPolicy.REQUIRED_PROVEN),
                            bound(lambda c, config: {
                                "status": FormalStatus.BOUNDED_PASS,
                                "mode": ProofMode.BMC,
                            }))
        self.assertEqual(
            [item.mode for item in raised.exception.records],
            [ProofMode.BMC, ProofMode.PROVE],
        )

    def test_cheapest_failure_selects_second_and_all_fail(self):
        candidates, evaluations = space(("cheap", "second"))
        result = gate_candidates(candidates, evaluations,
                                 FormalExplorationConfig(FormalPolicy.REQUIRED_BMC),
                                 bound(lambda c, config: {
                                     "status": FormalStatus.FAILED if c is candidates[0] else FormalStatus.BOUNDED_PASS,
                                     "mode": ProofMode.BMC,
                                 }))
        self.assertEqual(result.eligible, (candidates[1],))
        self.assertEqual([item.rank for item in result.records], [1, 2])
        with self.assertRaises(FormalExplorationError):
            gate_candidates(candidates, evaluations,
                            FormalExplorationConfig(FormalPolicy.REQUIRED_BMC),
                            bound(lambda c, config: {
                                "status": FormalStatus.FAILED,
                                "mode": ProofMode.BMC,
                            }))

    def test_required_proven_skips_bmc_failure_in_exact_rank_order(self):
        candidates, evaluations = space(("cheap", "second", "third"))
        calls = []

        def staged(candidate, config):
            calls.append((candidate.implementation_identity, config.policy))
            if candidate is candidates[0]:
                return {
                    "status": FormalStatus.FAILED,
                    "mode": ProofMode.BMC,
                }
            if config.policy is FormalPolicy.REQUIRED_BMC:
                return {
                    "status": FormalStatus.BOUNDED_PASS,
                    "mode": ProofMode.BMC,
                }
            return {
                "status": FormalStatus.PROVEN,
                "mode": ProofMode.PROVE,
            }

        result = gate_candidates(
            candidates,
            evaluations,
            FormalExplorationConfig(FormalPolicy.REQUIRED_PROVEN),
            bound(staged),
        )
        self.assertEqual(result.eligible, (candidates[1],))
        self.assertEqual(calls, [
            ("cheap", FormalPolicy.REQUIRED_BMC),
            ("second", FormalPolicy.REQUIRED_BMC),
            ("second", FormalPolicy.REQUIRED_PROVEN),
        ])
        self.assertEqual(
            [(item.rank, item.mode, item.status) for item in result.records],
            [
                (1, ProofMode.BMC, FormalStatus.FAILED),
                (2, ProofMode.BMC, FormalStatus.BOUNDED_PASS),
                (2, ProofMode.PROVE, FormalStatus.PROVEN),
            ],
        )

    def test_required_proven_stops_on_unknown_prove_with_attempted_records(self):
        candidates, evaluations = space(("cheap", "second"))
        calls = []

        def staged(candidate, config):
            calls.append((candidate.implementation_identity, config.policy))
            if config.policy is FormalPolicy.REQUIRED_BMC:
                return {
                    "status": FormalStatus.BOUNDED_PASS,
                    "mode": ProofMode.BMC,
                }
            return {
                "status": FormalStatus.UNKNOWN,
                "mode": ProofMode.PROVE,
                "reason": "prove timeout",
            }

        with self.assertRaisesRegex(FormalExplorationError, "prove timeout") as raised:
            gate_candidates(
                candidates,
                evaluations,
                FormalExplorationConfig(FormalPolicy.REQUIRED_PROVEN),
                bound(staged),
            )
        self.assertEqual(calls, [
            ("cheap", FormalPolicy.REQUIRED_BMC),
            ("cheap", FormalPolicy.REQUIRED_PROVEN),
        ])
        self.assertEqual(
            [(item.mode, item.status) for item in raised.exception.records],
            [
                (ProofMode.BMC, FormalStatus.BOUNDED_PASS),
                (ProofMode.PROVE, FormalStatus.UNKNOWN),
            ],
        )

    def test_required_proven_caches_bmc_and_prove_as_distinct_stages(self):
        candidates, evaluations = space(("cheap",))
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            def staged(candidate, config):
                calls.append(config.policy)
                if config.policy is FormalPolicy.REQUIRED_BMC:
                    return {
                        "status": FormalStatus.BOUNDED_PASS,
                        "mode": ProofMode.BMC,
                    }
                return {
                    "status": FormalStatus.PROVEN,
                    "mode": ProofMode.PROVE,
                }

            config = FormalExplorationConfig(
                FormalPolicy.REQUIRED_PROVEN,
                cache_directory=Path(directory),
            )
            verifier = bound(staged)
            first = gate_candidates(candidates, evaluations, config, verifier)
            cached = gate_candidates(candidates, evaluations, config, verifier)
            self.assertEqual(calls, [
                FormalPolicy.REQUIRED_BMC,
                FormalPolicy.REQUIRED_PROVEN,
            ])
            self.assertEqual(
                [item.cache_state for item in first.records],
                ["executed", "executed"],
            )
            self.assertEqual(
                [item.cache_state for item in cached.records],
                ["hit", "hit"],
            )
            self.assertEqual(len(_m39_result_entries(directory)), 2)

    def test_required_proven_staging_is_shared_by_implement_regions(self):
        cases = (
            (
                "implement",
                "module E { in a:u8 out y:u8 "
                "y=implement { a ^ 0 intent { minimize lut } } }",
                lambda result: result.exploration_results[0].formal_records,
                2,
            ),
            (
                "implement_with_pipeline_catalog",
                Path("examples/implementation_intent.zhl").read_text(),
                lambda result: result.exploration_results[0].formal_records,
                2,
            ),
        )
        for label, source, records_of, expected_call_count in cases:
            with self.subTest(entry_point=label):
                calls = []

                def staged(candidate, config):
                    calls.append(config.policy)
                    if config.policy is FormalPolicy.REQUIRED_BMC:
                        return {
                            "status": FormalStatus.BOUNDED_PASS,
                            "mode": ProofMode.BMC,
                        }
                    return {
                        "status": FormalStatus.PROVEN,
                        "mode": ProofMode.PROVE,
                    }

                result = compile_source(
                    source,
                    formal_policy=FormalPolicy.REQUIRED_PROVEN,
                    formal_verifier=bound(staged),
                )
                records = records_of(result)
                self.assertEqual(
                    calls,
                    [
                        FormalPolicy.REQUIRED_BMC,
                        FormalPolicy.REQUIRED_PROVEN,
                    ] * (expected_call_count // 2),
                )
                self.assertEqual(
                    [(item.mode, item.status) for item in records],
                    [
                        (ProofMode.BMC, FormalStatus.BOUNDED_PASS),
                        (ProofMode.PROVE, FormalStatus.PROVEN),
                    ],
                )

    def test_cache_hit_and_invalidation(self):
        candidates, evaluations = space(("cheap",))
        with tempfile.TemporaryDirectory() as directory:
            config = FormalExplorationConfig(FormalPolicy.REQUIRED_BMC, cache_directory=Path(directory))
            calls = []
            verifier = bound(lambda c, cfg: calls.append(c) or {
                "status": FormalStatus.BOUNDED_PASS,
                "mode": ProofMode.BMC,
            })
            gate_candidates(candidates, evaluations, config, verifier)
            result = gate_candidates(candidates, evaluations, config, verifier)
            self.assertEqual(len(calls), 1)
            self.assertEqual(result.records[0].cache_state, "hit")
            deeper = FormalExplorationConfig(FormalPolicy.REQUIRED_BMC, bmc_depth=33, cache_directory=Path(directory))
            gate_candidates(candidates, evaluations, deeper, verifier)
            self.assertEqual(len(calls), 2)
            changed = SimpleNamespace(
                implementation_identity="cheap",
                semantic_identity="root",
                timing_relation=None,
                artifact_hash="9" * 64,
                harness_hash="h",
            )
            changed_eval = (SimpleNamespace(candidate=changed, legal=True, objective_key=(0,)),)
            gate_candidates((changed,), changed_eval, deeper, verifier)
            self.assertEqual(len(calls), 3)

    def test_unhashed_legacy_flat_cache_entry_is_a_safe_miss(self):
        candidates, evaluations = space(("cheap",))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = FormalExplorationConfig(
                FormalPolicy.REQUIRED_BMC,
                cache_directory=root,
            )
            calls = []
            verifier = bound(lambda candidate, cfg: calls.append(candidate) or {
                "status": FormalStatus.BOUNDED_PASS,
                "mode": ProofMode.BMC,
            })
            gate_candidates(candidates, evaluations, config, verifier)
            canonical = next(iter(_m39_result_entries(root)))
            legacy = root / canonical.name
            canonical.replace(legacy)
            legacy_payload = json.loads(legacy.read_text(encoding="utf-8"))
            legacy_payload.pop("result_hash")
            legacy.write_text(
                json.dumps(legacy_payload),
                encoding="utf-8",
            )

            reruns = []
            cached = gate_candidates(
                candidates,
                evaluations,
                config,
                bound(lambda candidate, cfg: reruns.append(candidate) or {
                    "status": FormalStatus.BOUNDED_PASS,
                    "mode": ProofMode.BMC,
                }),
            )

            self.assertEqual(len(calls), 1)
            self.assertEqual(len(reruns), 1)
            self.assertTrue(
                cached.records[0].cache_state.startswith("corrupt-ignored")
            )
            self.assertTrue(legacy.is_file())
            self.assertTrue(canonical.exists())

    def test_unknown_and_skipped_results_are_never_cached(self):
        candidates, evaluations = space(("cheap",))
        for status in (FormalStatus.UNKNOWN, FormalStatus.SKIPPED):
            with (
                self.subTest(status=status.value),
                tempfile.TemporaryDirectory() as directory,
            ):
                calls = []
                verifier = bound(lambda candidate, config: calls.append(candidate) or {
                    "status": status,
                    "mode": ProofMode.BMC,
                    "reason": "transient formal route result",
                })
                config = FormalExplorationConfig(
                    FormalPolicy.AVAILABLE,
                    cache_directory=Path(directory),
                )
                first = gate_candidates(candidates, evaluations, config, verifier)
                second = gate_candidates(candidates, evaluations, config, verifier)
                self.assertEqual(len(calls), 2)
                self.assertTrue(first.eligible and second.eligible)
                self.assertEqual(first.records[0].cache_state, "executed")
                self.assertEqual(second.records[0].cache_state, "executed")
                self.assertFalse(tuple(Path(directory).rglob("*.json")))

    def test_cache_hit_preserves_full_failed_proof_evidence(self):
        candidates, evaluations = space(("broken",))
        with tempfile.TemporaryDirectory() as directory:
            config = FormalExplorationConfig(
                FormalPolicy.AVAILABLE,
                bmc_depth=9,
                max_formal_candidates=1,
                cache_directory=Path(directory),
            )
            calls = []
            counterexample = EquivalenceCounterexample(
                _bound_property(candidates[0]), failure_cycle=3, sample_cycle=2,
                values=(("x", "4'h2"),),
                raw_trace="counterexample trace",
            )

            def verify_failed(candidate, cfg):
                calls.append(candidate)
                return {
                    "status": FormalStatus.FAILED,
                    "mode": ProofMode.BMC,
                    "depth": 9,
                    "engine": "sby",
                    "solver": "z3",
                    "backend": "direct_systemverilog",
                    "reason": "deliberate mutation",
                    "counterexample": counterexample,
                }

            verifier = bound(verify_failed)

            first = gate_candidates(candidates, evaluations, config, verifier)
            wider_budget = FormalExplorationConfig(
                FormalPolicy.AVAILABLE,
                bmc_depth=9,
                max_formal_candidates=7,
                cache_directory=Path(directory),
            )
            cached = gate_candidates(
                candidates, evaluations, wider_budget, verifier
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(first.records[0].cache_state, "executed")
            record = cached.records[0]
            self.assertEqual(record.cache_state, "hit")
            self.assertEqual(record.depth, 9)
            self.assertEqual((record.engine, record.solver), ("sby", "z3"))
            self.assertEqual((record.backend, record.artifact_hash), ("direct_systemverilog", "b" * 64))
            self.assertEqual(record.counterexample, counterexample)
            self.assertEqual(record.proof_reason, "deliberate mutation")

    def test_corrupt_cache_is_diagnosed_ignored_and_replaced(self):
        candidates, evaluations = space(("candidate",))
        with tempfile.TemporaryDirectory() as directory:
            config = FormalExplorationConfig(
                FormalPolicy.REQUIRED_BMC, cache_directory=Path(directory)
            )
            calls = []
            verifier = bound(lambda c, cfg: calls.append(c) or {
                "status": FormalStatus.BOUNDED_PASS,
                "mode": ProofMode.BMC,
            })
            gate_candidates(candidates, evaluations, config, verifier)
            cache_path = next(iter(_m39_result_entries(directory)))
            cache_path.write_text("{not-json", encoding="utf-8")
            repaired = gate_candidates(candidates, evaluations, config, verifier)
            self.assertEqual(len(calls), 2)
            self.assertTrue(
                repaired.records[0].cache_state.startswith("corrupt-ignored")
            )
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            payload["result"]["result_schema"] = "obsolete-result-schema"
            cache_path.write_text(json.dumps(payload), encoding="utf-8")
            schema_repaired = gate_candidates(
                candidates, evaluations, config, verifier
            )
            self.assertEqual(len(calls), 3)
            self.assertTrue(
                schema_repaired.records[0].cache_state.startswith(
                    "corrupt-ignored"
                )
            )
            cached = gate_candidates(candidates, evaluations, config, verifier)
            self.assertEqual(len(calls), 3)
            self.assertEqual(cached.records[0].cache_state, "hit")

    def test_cache_rejects_counterexample_attached_to_non_failed_status(self):
        candidates, evaluations = space(("candidate",))
        with tempfile.TemporaryDirectory() as directory:
            config = FormalExplorationConfig(
                FormalPolicy.AVAILABLE,
                cache_directory=Path(directory),
            )
            initial_calls = []
            gate_candidates(
                candidates,
                evaluations,
                config,
                bound(lambda candidate, cfg: initial_calls.append(candidate) or {
                    "status": FormalStatus.FAILED,
                    "mode": ProofMode.BMC,
                }),
            )
            cache_path = next(iter(_m39_result_entries(directory)))
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            payload["result"]["status"] = FormalStatus.BOUNDED_PASS.value
            payload["result_hash"] = stable_digest(payload["result"])
            cache_path.write_text(json.dumps(payload), encoding="utf-8")

            repair_calls = []
            repaired = gate_candidates(
                candidates,
                evaluations,
                config,
                bound(lambda candidate, cfg: repair_calls.append(candidate) or {
                    "status": FormalStatus.BOUNDED_PASS,
                    "mode": ProofMode.BMC,
                }),
            )

            self.assertEqual(len(initial_calls), 1)
            self.assertEqual(len(repair_calls), 1)
            self.assertTrue(
                repaired.records[0].cache_state.startswith("corrupt-ignored")
            )
            self.assertIn(
                "exactly one counterexample", repaired.records[0].cache_state
            )
            cached = gate_candidates(
                candidates,
                evaluations,
                config,
                bound(lambda *_: self.fail("repaired cache was not reused")),
            )
            self.assertEqual(cached.records[0].cache_state, "hit")

    def test_route_artifact_identity_rejects_stale_cached_result(self):
        candidates, evaluations = space(("candidate",))
        with tempfile.TemporaryDirectory() as directory:
            config = FormalExplorationConfig(
                FormalPolicy.REQUIRED_BMC, cache_directory=Path(directory)
            )
            calls = []

            class Verifier:
                def cache_identity(self, candidate, cfg):
                    return {
                        "property_identity": _bound_property(candidate),
                        "artifact_hash": "d" * 64,
                        "implementation_artifact_hash": "d" * 64,
                        "reference_artifact_hash": "a" * 64,
                        "harness_hash": "e" * 64,
                        "assumptions_identity": "f" * 64,
                        "backend_identity": "1" * 64,
                    }

                def __call__(self, candidate, cfg):
                    calls.append(candidate)
                    return {
                        "status": FormalStatus.BOUNDED_PASS,
                        "mode": ProofMode.BMC,
                        "backend": "direct_systemverilog",
                        "artifact_hash": "d" * 64,
                    }

            verifier = Verifier()
            gate_candidates(candidates, evaluations, config, verifier)
            cache_path = next(iter(_m39_result_entries(directory)))
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            payload["result"]["artifact_hash"] = "0" * 64
            cache_path.write_text(json.dumps(payload), encoding="utf-8")
            repaired = gate_candidates(candidates, evaluations, config, verifier)
            self.assertEqual(len(calls), 2)
            self.assertTrue(
                repaired.records[0].cache_state.startswith("corrupt-ignored")
            )

    def test_concurrent_cache_publication_never_exposes_partial_json(self):
        candidates, evaluations = space(("candidate",))
        with tempfile.TemporaryDirectory() as directory:
            config = FormalExplorationConfig(
                FormalPolicy.REQUIRED_BMC, cache_directory=Path(directory)
            )
            barrier = Barrier(2)

            def verify_concurrently(candidate, cfg):
                barrier.wait(timeout=5)
                return {
                    "status": FormalStatus.BOUNDED_PASS,
                    "mode": ProofMode.BMC,
                    "engine": "sby",
                    "solver": "z3",
                }

            verifier = bound(verify_concurrently)

            with patch(
                "zlang.formal_exploration.tool_versions",
                return_value=(("sby", "test"), ("z3", "test")),
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = tuple(executor.map(
                        lambda _: gate_candidates(
                            candidates, evaluations, config, verifier
                        ),
                        range(2),
                    ))
                cached = gate_candidates(
                    candidates, evaluations, config,
                    bound(lambda *_: self.fail(
                        "valid concurrent cache was not reused"
                    )),
                )
            self.assertEqual(len(results), 2)
            self.assertTrue(all(result.eligible for result in results))
            self.assertEqual(cached.records[0].cache_state, "hit")

    def test_deterministic_order_budget_and_unsupported_route(self):
        candidates, evaluations = space(("a", "b", "c"))
        config = FormalExplorationConfig(FormalPolicy.REQUIRED_BMC, max_formal_candidates=1)
        with self.assertRaisesRegex(FormalExplorationError, "budget"):
            gate_candidates(candidates, evaluations, config,
                            bound(lambda c, cfg: {
                                "status": FormalStatus.FAILED,
                                "mode": ProofMode.BMC,
                            }))
        with self.assertRaises(FormalExplorationError):
            gate_candidates(candidates, evaluations, FormalExplorationConfig(FormalPolicy.REQUIRED_BMC), None)
        advisory = gate_candidates(candidates, evaluations,
                                   FormalExplorationConfig(FormalPolicy.AVAILABLE), None)
        self.assertEqual(tuple(r.rank for r in advisory.records), (1, 2, 3))

    def test_real_solver_mutation_status(self):
        candidates, evaluations = space(("mutation",))
        source = "module mut(input [3:0] a); wire [3:0] y = a - 1; always @* assert(y == a + 1); endmodule\n"
        def verify_mutation(candidate, config):
            result = run_verilog_formal(
                source, top="mut", property_id=_bound_property(candidate),
                                        mode=ProofMode.BMC, depth=config.bmc_depth)
            counterexample = None
            if result.counterexample is not None:
                counterexample = EquivalenceCounterexample(
                    _bound_property(candidate),
                    failure_cycle=result.counterexample.cycle,
                    values=result.counterexample.values,
                    raw_trace=result.counterexample.raw_trace,
                )
            return {
                "status": result.status,
                "mode": result.mode,
                "counterexample": counterexample,
            }
        with self.assertRaisesRegex(FormalExplorationError, "no candidate"):
            gate_candidates(candidates, evaluations,
                            FormalExplorationConfig(FormalPolicy.REQUIRED_BMC, bmc_depth=4),
                            bound(verify_mutation))

    def test_combined_space_is_lazy_and_does_not_need_direct_sv(self):
        source = ("module E { clock clk reset rst in a:vec<4,u3> in b:vec<4,u3> out y:u8 "
                  "y=implement { dot(a,b) intent { latency >= 1 dsp <= 4 minimize lut } } }")
        calls = []
        def verify_selected(candidate, config):
            calls.append(candidate.implementation_identity)
            return {"status": FormalStatus.BOUNDED_PASS, "mode": ProofMode.BMC,
                    "backend": "direct_systemverilog"}
        result = compile_source(source, formal_policy=FormalPolicy.REQUIRED_BMC,
                                formal_verifier=bound(verify_selected))
        exploration = result.exploration_results[0]
        self.assertGreaterEqual(len(exploration.generated_candidates), 4)
        # ``implement`` owns one unified M39 site.  Planner pipeline metadata
        # is mirrored from that proof and must not trigger a second route.
        self.assertEqual(len(calls), 1)
        self.assertEqual(exploration.formal_records[0].cache_state, "executed")
        self.assertIn("formal candidate", result.exploration_report)






    def test_selected_second_pipeline_record_is_the_only_catalog_evidence(self):
        source = """
        module RankedTimedImplement {
          clock clk
          reset rst
          in a:u2
          in b:u2
          in c:u2
          in d:u2
          in e:u2
          in f:u2
          in g:u2
          in h:u2
          out y:u7
          y = implement {
            a*b+c*d+e*f+g*h
            intent { latency >= 1 ii == 1 dsp <= 4 fmax >= 100 minimize lut }
          }
        }
        """
        calls = []

        def fail_first(candidate, _config):
            calls.append(candidate.implementation_identity)
            return {
                "status": (
                    FormalStatus.FAILED
                    if len(calls) == 1 else FormalStatus.BOUNDED_PASS
                )
            }

        result = compile_source(
            source,
            formal_policy=FormalPolicy.REQUIRED_BMC,
            formal_max_candidates=4,
            formal_verifier=bound(fail_first),
        )
        exploration = result.exploration_results[0]
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [(item.rank, item.candidate_identity, item.status)
             for item in exploration.formal_records],
            [
                (1, calls[0], FormalStatus.FAILED),
                (2, calls[1], FormalStatus.BOUNDED_PASS),
            ],
        )
        catalog_records = result.ir.pipeline_explorations[0].formal_records
        self.assertEqual(len(catalog_records), 1)
        self.assertEqual(catalog_records[0].candidate_identity, calls[1])
        self.assertNotIn(calls[0], result.pipeline_report)
        self.assertIn(f"candidate_identity={calls[1]}", result.pipeline_report)




if __name__ == "__main__":
    unittest.main()
