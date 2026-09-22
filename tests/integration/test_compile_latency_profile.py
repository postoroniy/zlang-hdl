# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""The opt-in latency profiler must report real compiler stages, not estimates."""

import json
from pathlib import Path
import subprocess
import sys

from tools.profile_compilation_latency import _Timeline


ROOT = Path(__file__).resolve().parents[2]


def test_nested_timing_accounts_for_repeat_calls_and_exclusive_time() -> None:
    timeline = _Timeline()
    with timeline.span("outer"):
        with timeline.span("inner"):
            pass
        with timeline.span("inner"):
            pass
    report = timeline.aggregate()
    assert report["inner"]["calls"] == 2
    assert report["outer"]["calls"] == 1
    assert report["outer"]["inclusive_ms"] >= report["inner"]["inclusive_ms"]
    assert report["outer"]["exclusive_ms"] >= 0


def test_sv_profile_uses_actual_cli_and_reports_eager_products(tmp_path: Path) -> None:
    source = tmp_path / "counter.zhl"
    source.write_bytes((ROOT / "examples/counter.zhl").read_bytes())
    completed = subprocess.run(
        (
            sys.executable,
            str(ROOT / "tools/profile_compilation_latency.py"),
            str(source),
            "--top",
            "Counter",
            "--mode",
            "sv",
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    result = json.loads(completed.stdout)
    assert result["counters"]["rtl_bytes"] > 0
    assert result["counters"]["canonical_nodes"] > 0
    assert result["counters"]["ast_nodes"] > 0
    assert result["timings"]["product.formal"]["calls"] == 1
    assert result["timings"]["emit_systemverilog_artifact"]["calls"] == 1
