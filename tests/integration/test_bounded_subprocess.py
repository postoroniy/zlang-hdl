# SPDX-License-Identifier: Apache-2.0
"""Timeout closes the owned tree even when a tool starts a new session."""

from pathlib import Path
import subprocess
import sys
import time

import pytest
from zlang.common.subprocess import run_bounded_process


def test_timeout_reaps_cross_session_child_without_touching_sibling(tmp_path):
    receipt = tmp_path / "child.pid"
    child = (
        "import os,time;os.setsid();open("
        + repr(str(receipt))
        + ',"w").write(str(os.getpid()));time.sleep(60)'
    )
    parent = (
        'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",'
        + repr(child)
        + "]);time.sleep(60)"
    )
    sibling = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            run_bounded_process(
                [sys.executable, "-c", parent], timeout=2, capture_output=True
            )
        pid = int(receipt.read_text())

        def live():
            try:
                status = (
                    (Path("/proc") / str(pid) / "stat")
                    .read_text()
                    .split(") ")[1]
                    .split()[0]
                )
            except FileNotFoundError:
                return False
            return status != "Z"

        deadline = time.monotonic() + 1
        while live() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not live()
        assert sibling.poll() is None
    finally:
        sibling.kill()
        sibling.wait()
