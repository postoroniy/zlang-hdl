"""Shared failure contract for lossless canonical IR conversion."""


class CanonicalizationError(ValueError):
    """Semantic IR cannot be represented or restored losslessly."""
