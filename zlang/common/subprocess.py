"""Small subprocess value normalizers shared by compiler tool runners."""

from __future__ import annotations


def subprocess_text(value: str | bytes | None) -> str:
    """Return one captured subprocess stream as deterministic text.

    ``subprocess.TimeoutExpired`` can retain partial output as bytes even when
    the original run requested ``text=True``. Tool callers should not each
    carry a subtly different bytes/None fallback.
    """

    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


__all__ = ["subprocess_text"]
