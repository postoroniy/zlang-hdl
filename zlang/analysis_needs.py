"""Demanded compiler-owned observational analysis products.

Semantic checking is authoritative regardless of which editor projection is
requested.  These flags only control optional records produced alongside the
typed IR; they never alter type checking or generated IR.
"""

from __future__ import annotations

from enum import IntFlag


class AnalysisNeeds(IntFlag):
    """Optional source-observation products requested by one analysis."""

    NONE = 0
    DEFINITIONS = 1 << 0
    COMPLETION = 1 << 1
    SIGNATURE_HELP = 1 << 2

    def wants(self, need: "AnalysisNeeds") -> bool:
        """Return whether this analysis includes one observational product."""

        return bool(self & need)


__all__ = ["AnalysisNeeds"]
