# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Stable identities and physical-domain ownership for candidate sites."""

from __future__ import annotations

from zlang.ir.cdc import ClockDomain, PowerUpPolicy
from zlang.ir.hierarchy import candidate_specialization_identity
from zlang.ir.module import Module
from zlang.ir.signed_reductions import expression_semantic_identity


class CandidateIdentityError(ValueError):
    """A candidate owner cannot be resolved unambiguously."""


def module_candidate_owner_identity(module: Module) -> str:
    """Return the candidate owner encoded by a concrete typed module."""

    return candidate_specialization_identity(module.name, module.parameters)


def candidate_owner_formal_domain(
    module: Module,
    owner_identity: str | None,
) -> tuple[ClockDomain | None, str | None]:
    """Resolve the exact physical domain of one retained candidate owner."""

    matches: list[Module] = []

    def visit(current: Module) -> None:
        module_owner = module_candidate_owner_identity(current)
        owns_callable = False
        if owner_identity is not None and owner_identity.startswith("callable:"):
            callee = owner_identity.removeprefix("callable:")
            owns_callable = any(
                item.callee_identity == callee
                for item in (*current.functions, *current.callable_definitions)
            )
        if (
            owner_identity is None
            or owner_identity == "<anonymous-module>"
            or owner_identity == module_owner
            or owns_callable
        ):
            matches.append(current)
        for child in current.children:
            visit(child)

    visit(module)
    if owner_identity in {None, "<anonymous-module>"}:
        matches = [module]
    if len(matches) != 1:
        raise CandidateIdentityError(
            "formal candidate owner does not resolve to one typed module: "
            f"{owner_identity or '<anonymous-module>'}"
        )
    domains = matches[0].clock_domains
    if not domains:
        return None, None
    if len(domains) != 1:
        return None, (
            "semantic-reference equivalence/formal-aware selection candidate "
            "equivalence requires exactly one physical clock/reset domain per "
            "candidate owner"
        )
    domain = domains[0]
    if domain.power_up is not PowerUpPolicy.UNSPECIFIED:
        return None, (
            "semantic-reference equivalence/formal-aware selection candidate "
            "equivalence does not support power_up reset semantics"
        )
    return domain, None


def candidate_site_key(
    owner_identity: str | None,
    output: str | None,
    source_identity: str,
) -> tuple[str, str | None, str]:
    """Return the typed key joining one implementation region."""

    return (owner_identity or "<anonymous-module>", output, source_identity)


def pipeline_site_key(
    module: Module,
    pipeline: object,
) -> tuple[str, str | None, str]:
    """Key retained pipeline metadata using the shared candidate-site contract."""

    return candidate_site_key(
        module_candidate_owner_identity(module),
        getattr(pipeline, "output", None),
        expression_semantic_identity(pipeline.source_expression),
    )


__all__ = [
    "CandidateIdentityError",
    "candidate_owner_formal_domain",
    "candidate_site_key",
    "module_candidate_owner_identity",
    "pipeline_site_key",
]
