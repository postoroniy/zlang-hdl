"""Typed backend-independent contracts for bounded external components."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import TYPE_CHECKING

from zlang.source import SourceOrigin

if TYPE_CHECKING:
    from zlang.ir.module import ModuleSignature


@dataclass(frozen=True)
class ExternalModuleContract:
    """Semantic identity of one model-backed external module.

    Physical HDL names and source bytes are deliberately absent.  They are a
    backend concern and therefore cannot change semantic or canonical identity.
    """

    logical_name: str
    signature: ModuleSignature
    model_callee_identity: str
    semantic_identity: str = ""
    source_origin: SourceOrigin | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        payload = {
            "logical_name": self.logical_name,
            "model_callee_identity": self.model_callee_identity,
            "signature": self.signature.to_data(),
        }
        expected = "extern:" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if self.semantic_identity and self.semantic_identity != expected:
            raise ValueError("external module semantic identity does not match contract")
        if not self.semantic_identity:
            object.__setattr__(self, "semantic_identity", expected)


__all__ = ["ExternalModuleContract"]
