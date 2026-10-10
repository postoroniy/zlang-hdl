from dataclasses import FrozenInstanceError
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from zlang.formal_routes import (
    ABC_PDR_ROUTE,
    AIGER_AVY_ROUTE,
    BTOR_BTORMC_ROUTE,
    BTOR_PONO_ROUTE,
    SMTBMC_ROUTE,
    formal_engine_route,
    public_formal_route_identities,
    qualification_formal_route_identities,
)
from zlang.ir.formal import FormalError, ProofMode
from zlang.verification_bundle import VerificationRunConfig
from zlang.verification_bundle import VerificationBundleError


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "formal_solver_matrix_tool", ROOT / "tools/formal_solver_matrix.py"
)
assert SPEC is not None and SPEC.loader is not None
formal_solver_matrix = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = formal_solver_matrix
SPEC.loader.exec_module(formal_solver_matrix)

ENGINE_SPEC = importlib.util.spec_from_file_location(
    "formal_engine_matrix_tool", ROOT / "tools/formal_engine_matrix.py"
)
assert ENGINE_SPEC is not None and ENGINE_SPEC.loader is not None
formal_engine_matrix = importlib.util.module_from_spec(ENGINE_SPEC)
sys.modules[ENGINE_SPEC.name] = formal_engine_matrix
ENGINE_SPEC.loader.exec_module(formal_engine_matrix)


def test_smtbmc_route_is_immutable_and_owns_the_solver_tool_closure() -> None:
    assert formal_engine_route("smtbmc") is SMTBMC_ROUTE
    assert SMTBMC_ROUTE.engine_line(solver="bitwuzla") == "smtbmc bitwuzla"
    assert SMTBMC_ROUTE.tool_names(solver="bitwuzla") == (
        "yosys",
        "sby",
        "yosys-smtbmc",
        "bitwuzla",
    )
    with pytest.raises(FrozenInstanceError):
        SMTBMC_ROUTE.identity = "changed"
    with pytest.raises(FormalError, match="unsupported formal execution route"):
        formal_engine_route("guessed")


def test_qualification_routes_are_mode_bounded_and_not_public() -> None:
    assert public_formal_route_identities() == ("smtbmc",)
    assert qualification_formal_route_identities() == (
        "abc-pdr",
        "aiger-avy",
        "btor-pono",
        "btor-btormc",
    )
    assert ABC_PDR_ROUTE.engine_line(solver="pdr") == "abc pdr"
    assert AIGER_AVY_ROUTE.engine_line(solver="avy") == "aiger avy"
    assert BTOR_PONO_ROUTE.engine_line(solver="pono") == "btor pono"
    assert BTOR_BTORMC_ROUTE.engine_line(solver="btormc") == "btor btormc"
    ABC_PDR_ROUTE.validate_request(solver="pdr", mode="prove")
    BTOR_BTORMC_ROUTE.validate_request(solver="btormc", mode="bmc")
    with pytest.raises(FormalError, match="does not support mode 'cover'"):
        BTOR_BTORMC_ROUTE.validate_request(solver="btormc", mode="cover")
    with pytest.raises(FormalError, match="does not support mode 'bmc'"):
        ABC_PDR_ROUTE.validate_request(solver="pdr", mode="bmc")
    with pytest.raises(FormalError, match="requires solver 'pono'"):
        BTOR_PONO_ROUTE.validate_request(solver="z3", mode="bmc")


def test_verification_config_serializes_exact_route_and_rejects_old_shape() -> None:
    config = VerificationRunConfig(
        mode=ProofMode.PROVE,
        solver="boolector",
        route="smtbmc",
        depth=9,
    )
    data = config.to_data()
    assert data["route"] == "smtbmc"
    assert VerificationRunConfig.from_data(data) == config

    del data["route"]
    with pytest.raises(VerificationBundleError, match="missing field 'route'"):
        VerificationRunConfig.from_data(data)


def test_solver_matrix_reuses_one_bundle_and_retains_independent_runs(
    monkeypatch, tmp_path: Path,
) -> None:
    loaded = SimpleNamespace(
        manifest=SimpleNamespace(
            bundle_identity="verification-bundle:" + "a" * 64,
            computed_identity="verification-bundle:" + "b" * 64,
        )
    )
    calls: list[tuple[object, VerificationRunConfig, Path]] = []

    def run(bundle, *, config, work_directory):
        calls.append((bundle, config, work_directory))
        result = SimpleNamespace(
            property_id="p",
            kind="safety",
            status="proven",
            mode="prove",
            depth=8,
            counterexample=None,
        )
        return SimpleNamespace(
            outcome="passed",
            run_identity="verification-run:" + config.solver * 8,
            results=(result,),
            tool_versions=((config.solver, "version"),),
        )

    monkeypatch.setattr(formal_solver_matrix, "load_verification_bundle", lambda _: loaded)
    monkeypatch.setattr(formal_solver_matrix, "run_verification_bundle_staged", run)
    payload = formal_solver_matrix.replay_solver_matrix(
        tmp_path / "bundle",
        required_solvers=("z3", "boolector", "bitwuzla"),
        corroborating_solvers=("yices", "cvc5"),
        mode=ProofMode.PROVE,
        depth=8,
        work_root=tmp_path / "work",
    )

    assert payload["agreement"] is True
    assert [item["role"] for item in payload["replays"]] == [
        "required",
        "required",
        "required",
        "corroborating",
        "corroborating",
    ]
    assert len({id(bundle) for bundle, _, _ in calls}) == 1
    assert {config.route for _, config, _ in calls} == {"smtbmc"}
    assert all(item["counterexample_vector"] == [] for item in payload["replays"])
    assert [path.name for _, _, path in calls] == [
        "z3",
        "boolector",
        "bitwuzla",
        "yices",
        "cvc5",
        ]


def test_solver_matrix_requires_semantic_counterexample_agreement(
    monkeypatch, tmp_path: Path,
) -> None:
    loaded = SimpleNamespace(
        manifest=SimpleNamespace(
            bundle_identity="verification-bundle:" + "a" * 64,
            computed_identity="verification-bundle:" + "b" * 64,
        )
    )

    def run(_bundle, *, config, work_directory):
        del work_directory
        counterexample = SimpleNamespace(
            cycle=11 if config.solver == "z3" else 12,
            values=(("count", "10"),),
        )
        result = SimpleNamespace(
            property_id="p",
            kind="safety",
            status="failed",
            mode="bmc",
            depth=16,
            counterexample=counterexample,
        )
        return SimpleNamespace(
            outcome="failed",
            run_identity="verification-run:" + config.solver * 8,
            results=(result,),
            tool_versions=((config.solver, "version"),),
        )

    monkeypatch.setattr(formal_solver_matrix, "load_verification_bundle", lambda _: loaded)
    monkeypatch.setattr(formal_solver_matrix, "run_verification_bundle_staged", run)
    payload = formal_solver_matrix.replay_solver_matrix(
        tmp_path / "bundle",
        required_solvers=("z3", "boolector"),
        mode=ProofMode.BMC,
        depth=16,
        work_root=tmp_path / "work",
    )

    assert payload["agreement"] is False


def test_solver_matrix_rejects_duplicate_or_empty_required_sets(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one required"):
        formal_solver_matrix.replay_solver_matrix(
            tmp_path / "bundle", required_solvers=(), work_root=tmp_path / "work"
        )
    with pytest.raises(ValueError, match="unique"):
        formal_solver_matrix.replay_solver_matrix(
            tmp_path / "bundle",
            required_solvers=("z3", "z3"),
            work_root=tmp_path / "work",
        )


def test_engine_matrix_keeps_unqualified_avy_visible_but_non_authoritative(
    monkeypatch, tmp_path: Path,
) -> None:
    loaded = SimpleNamespace(
        manifest=SimpleNamespace(
            bundle_identity="verification-bundle:" + "a" * 64,
            computed_identity="verification-bundle:" + "b" * 64,
        )
    )

    def run(_bundle, *, config, work_directory, job_kinds):
        del work_directory
        assert job_kinds == frozenset({"safety"})
        status = "unknown" if config.route == "aiger-avy" else (
            "proven" if config.mode is ProofMode.PROVE else "bounded_pass"
        )
        return SimpleNamespace(
            outcome="incomplete" if status == "unknown" else "passed",
            run_identity="verification-run:" + config.route * 4,
            results=(SimpleNamespace(
                property_id="p",
                status=status,
                counterexample=None,
            ),),
            tool_versions=((config.solver, "version"),),
        )

    monkeypatch.setattr(formal_engine_matrix, "load_verification_bundle", lambda _: loaded)
    monkeypatch.setattr(formal_engine_matrix, "run_verification_bundle", run)
    payload = formal_engine_matrix.qualify_formal_engines(
        tmp_path / "bundle",
        expected="passed",
        depth=8,
        timeout_seconds=10,
        jobs=1,
        work_root=tmp_path / "work",
    )

    assert payload["consistent"] is True
    assert payload["required_routes"] == [
        "smtbmc",
        "abc-pdr",
        "btor-pono",
        "btor-btormc",
    ]
    avy = next(
        item for item in payload["replays"] if item["route"] == "aiger-avy"
    )
    assert avy["required"] is False
    assert avy["outcome"] == "incomplete"


def test_engine_matrix_requires_each_required_counterexample_to_be_attributed(
    monkeypatch, tmp_path: Path,
) -> None:
    loaded = SimpleNamespace(
        manifest=SimpleNamespace(
            bundle_identity="verification-bundle:" + "a" * 64,
            computed_identity="verification-bundle:" + "b" * 64,
        )
    )

    def run(_bundle, *, config, work_directory, job_kinds):
        del work_directory, job_kinds
        values = () if config.route == "btor-pono" else (("register:x", "1"),)
        return SimpleNamespace(
            outcome="failed",
            run_identity="verification-run:" + config.route * 4,
            results=(SimpleNamespace(
                property_id="p",
                status="failed",
                counterexample=SimpleNamespace(cycle=3, values=values),
            ),),
            tool_versions=((config.solver, "version"),),
        )

    monkeypatch.setattr(formal_engine_matrix, "load_verification_bundle", lambda _: loaded)
    monkeypatch.setattr(formal_engine_matrix, "run_verification_bundle", run)
    payload = formal_engine_matrix.qualify_formal_engines(
        tmp_path / "bundle",
        expected="failed",
        depth=8,
        timeout_seconds=10,
        jobs=1,
        work_root=tmp_path / "work",
    )

    assert payload["consistent"] is False
