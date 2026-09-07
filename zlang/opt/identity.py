"""Content identities for complete canonical optimization IR."""

from __future__ import annotations

from zlang.common import stable_digest
from zlang.opt.ir import CanonicalModule, OptimizationStage
from zlang.opt.render import render_identity


CANONICAL_IR_IDENTITY_SCHEMA = "zlang-canonical-ir-content-v13"


def canonical_ir_identity(module: CanonicalModule) -> str:
    """Return the origin-insensitive identity of one canonical IR stage.

    The stage is both present in the canonical rendering and repeated in the
    hash envelope.  The explicit envelope makes future serialization changes
    versioned rather than silently changing the meaning of an existing key.
    """

    prefix = {
        OptimizationStage.HIGH_LEVEL: "high-level",
        OptimizationStage.SELECTED_ARCHITECTURE: "selected",
    }[module.stage]
    return prefix + ":" + stable_digest(
        {
            "schema": CANONICAL_IR_IDENTITY_SCHEMA,
            "stage": module.stage.value,
            "canonical_ir": render_identity(module),
        }
    )


__all__ = ["CANONICAL_IR_IDENTITY_SCHEMA", "canonical_ir_identity"]
