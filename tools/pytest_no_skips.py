"""Reject unexpected skips in the release EDA lane.

Historical Clash compatibility tests may skip when Clash is absent.  They are
not part of the production backend contract.  Every other skip remains a hard
failure, including a skip caused by a missing production EDA tool.
"""

from __future__ import annotations


def _retired_clash_skip(report) -> bool:
    detail = getattr(report, "longreprtext", "") or str(report.longrepr)
    lowered = detail.lower()
    return "clash" in lowered and any(
        marker in lowered
        for marker in (
            "unavailable",
            "not available",
            "not found",
            "required",
            "requires",
        )
    )


def pytest_sessionfinish(session, exitstatus) -> None:  # pragma: no cover - pytest hook
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = () if reporter is None else reporter.stats.get("skipped", ())
    if any(not _retired_clash_skip(report) for report in skipped):
        session.exitstatus = 1
