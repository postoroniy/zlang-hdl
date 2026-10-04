# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Candidate-family classification shared by selection and formal preparation."""

from __future__ import annotations


def candidate_equivalence_class(candidate: object) -> str:
    """Return the frozen semantic-reference equivalence candidate family."""

    explicit = getattr(candidate, "candidate_class", None)
    if explicit in {
        "value",
        "guarded_rewrite",
        "architecture_alternatives",
        "pipeline_scheduler",
        "exact_reduction",
        "pipeline",
    }:
        return str(explicit)
    stages = tuple(str(item) for item in getattr(candidate, "stages", ()))
    if any("pipeline" in item for item in stages):
        return "pipeline_scheduler"
    if any("reduction" in item for item in stages):
        return "exact_reduction"
    if getattr(candidate, "architecture", None) is not None:
        return "architecture_alternatives"
    if any(item == "value" for item in stages):
        return "guarded_rewrite"
    return "value"


__all__ = ["candidate_equivalence_class"]
