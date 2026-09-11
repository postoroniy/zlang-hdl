"""Reject every skip in the release EDA lane."""

from __future__ import annotations

def pytest_sessionfinish(session, exitstatus) -> None:  # pragma: no cover - pytest hook
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = () if reporter is None else reporter.stats.get("skipped", ())
    if skipped:
        session.exitstatus = 1
