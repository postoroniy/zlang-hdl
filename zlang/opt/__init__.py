"""Stable public optimization names backed by authoritative owner modules."""

from zlang.opt.identity import canonical_ir_identity
from zlang.opt.egraph import (
    EGraphAdapterError,
    canonical_to_egraph,
    deserialize_egraph,
    egraph_to_canonical,
    egraph_to_expression,
    render_egraph,
    serialize_egraph,
)
from zlang.opt.ir import (
    EffectKind,
    EquivalenceMode,
    NodeCategory,
    OptimizationStage,
    Purity,
    Signedness,
    equivalence_definition,
)
from zlang.opt.lowering import CanonicalizationError, lower, restore
from zlang.opt.render import render
from zlang.opt.rewrite_spec import RewriteRule
from zlang.opt.rewrite_model import Term, render_saturation, term_to_expression
from zlang.opt.saturation import SaturationError, saturate

__all__ = [
    "CanonicalizationError",
    "EGraphAdapterError",
    "EffectKind",
    "EquivalenceMode",
    "NodeCategory",
    "OptimizationStage",
    "Purity",
    "RewriteRule",
    "SaturationError",
    "Signedness",
    "Term",
    "canonical_to_egraph",
    "canonical_ir_identity",
    "deserialize_egraph",
    "egraph_to_canonical",
    "egraph_to_expression",
    "equivalence_definition",
    "lower",
    "render",
    "render_saturation",
    "render_egraph",
    "restore",
    "saturate",
    "serialize_egraph",
    "term_to_expression",
]
