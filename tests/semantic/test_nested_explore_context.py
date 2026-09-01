from __future__ import annotations

from zlang.compiler import compile_source
from zlang.formal_exploration import FormalPolicy
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.simulate import simulate


def test_explore_inside_direct_function_uses_expression_context() -> None:
    module = compile_source(
        "fn select(x:u8)->u8 { explore { x } } "
        "module DirectExplore { in x:u8 out y:u8 y=select(x) }",
        include_clash=False,
    ).ir
    assert simulate(module, x=37) == {"y": 37}


def test_explore_inside_generic_function_receives_formal_configuration() -> None:
    calls: list[str] = []

    class Verifier:
        formal_route = "M36_clash"

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
                "backend": "clash",
                **self.cache_identity(candidate, config),
            }

    module = compile_source(
        "fn select<type T>(x:T) { explore { x } } "
        "module GenericExplore { in x:u8 out y:u8 y=select(x) }",
        include_clash=False,
        formal_policy=FormalPolicy.REQUIRED_BMC,
        formal_verifier=Verifier(),
    ).ir
    assert calls
    assert simulate(module, x=91) == {"y": 91}


def test_explore_inside_operator_body_is_typed_without_free_variables() -> None:
    module = compile_source(
        "struct Box { value:u8 } "
        "operator +(left:Box,right:Box) { "
        "Box { value=explore { left.value } } } "
        "module OperatorExplore { in a:Box in b:Box out y:Box y=a+b }",
        include_clash=False,
    ).ir
    assert simulate(module, a={"value": 12}, b={"value": 99}) == {
        "y": {"value": 12}
    }
