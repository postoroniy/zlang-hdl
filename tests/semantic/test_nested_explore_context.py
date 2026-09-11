from __future__ import annotations

import pytest

from zlang.compiler import compile_source
from zlang.formal_exploration import FormalPolicy
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.simulate import simulate


def test_scalar_implement_is_module_assignment_only() -> None:
    with pytest.raises(Exception, match="scalar explore was removed"):
        compile_source(
            "fn select(x:u8)->u8 { explore { x } } "
            "module DirectExplore { in x:u8 out y:u8 y=select(x) }",
        )


def test_removed_scalar_explore_does_not_enter_generic_function_context() -> None:
    calls: list[str] = []

    class Verifier:
        formal_route = "M36_direct_systemverilog"

        def cache_identity(self, candidate: object, config: object):
            return {
                "property_identity": "m36.test.nested-explore",
                "reference_artifact_hash": "1" * 64,
                "implementation_artifact_hash": "2" * 64,
                "artifact_hash": "2" * 64,
                "harness_hash": "3" * 64,
                "assumptions_identity": "4" * 64,
                "backend_identity": "5" * 64,
            }

        def __call__(self, candidate: object, config: object) -> dict[str, object]:
            calls.append(getattr(candidate, "implementation_identity"))
            return {
                "status": FormalStatus.BOUNDED_PASS,
                "mode": ProofMode.BMC,
                "depth": config.bmc_depth,
                "engine": config.engine,
                "solver": config.solver,
                "backend": "direct_systemverilog",
                **self.cache_identity(candidate, config),
            }

    with pytest.raises(Exception, match="scalar explore was removed"):
        compile_source(
            "fn select<type T>(x:T) { explore { x } } "
            "module GenericExplore { in x:u8 out y:u8 y=select(x) }",
            formal_policy=FormalPolicy.REQUIRED_BMC,
            formal_verifier=Verifier(),
        )
    assert calls == []


def test_removed_scalar_explore_does_not_enter_operator_body() -> None:
    with pytest.raises(Exception, match="scalar explore was removed"):
        compile_source(
            "struct Box { value:u8 } "
            "operator +(left:Box,right:Box) { "
            "Box { value=explore { left.value } } } "
            "module OperatorExplore { in a:Box in b:Box out y:Box y=a+b }",
        )
