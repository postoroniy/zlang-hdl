"""Pytest plugin that makes any skipped test fail an external-tool CI lane."""

from __future__ import annotations


def pytest_sessionfinish(session, exitstatus) -> None:  # pragma: no cover - pytest hook
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None and reporter.stats.get("skipped"):
        session.exitstatus = 1

