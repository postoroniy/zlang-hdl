"""Per-owner physical-domain applicability for the existing M39 route."""

from __future__ import annotations

from zlang.candidate_sites import (
    candidate_owner_formal_domain,
    module_candidate_owner_identity,
)
from zlang.ir.cdc import ClockDomain, PowerUpPolicy
from zlang.ir.module import Module


def test_unrelated_unsupported_child_does_not_poison_root_candidate() -> None:
    child = Module(
        "PowerUpChild",
        (),
        (),
        clock="child_clk",
        reset="child_rst",
        clock_domains=(ClockDomain(
            "child_clk",
            "child_rst",
            power_up=PowerUpPolicy.RESET,
        ),),
    )
    root_domain = ClockDomain("clk", "rst")
    root = Module(
        "RootCandidate",
        (),
        (),
        clock="clk",
        reset="rst",
        clock_domains=(root_domain,),
        children=(child,),
    )

    domain, limitation = candidate_owner_formal_domain(
        root, module_candidate_owner_identity(root)
    )
    assert domain == root_domain
    assert limitation is None

    child_domain, child_limitation = candidate_owner_formal_domain(
        root, module_candidate_owner_identity(child)
    )
    assert child_domain is None
    assert "power_up reset" in (child_limitation or "")


def test_multidomain_limitation_is_local_to_exact_candidate_owner() -> None:
    child = Module(
        "MultiDomainChild",
        (),
        (),
        clock_domains=(ClockDomain("a", "ar"), ClockDomain("b", "br")),
    )
    root = Module("Root", (), (), children=(child,))

    root_domain, root_limitation = candidate_owner_formal_domain(
        root, module_candidate_owner_identity(root)
    )
    assert root_domain is None
    assert root_limitation is None

    child_domain, child_limitation = candidate_owner_formal_domain(
        root, module_candidate_owner_identity(child)
    )
    assert child_domain is None
    assert "exactly one physical" in (child_limitation or "")
