# SPDX-License-Identifier: Apache-2.0
"""Timeout closes the owned tree even when a tool starts a new session."""

from pathlib import Path
import json
import os
import signal
import threading
from types import SimpleNamespace
import subprocess
import sys
import time

import pytest
from zlang.common import subprocess as bounded
from zlang.common.subprocess import ProcessCleanupError, run_bounded_process


def _process_is_live(pid):
    try:
        status = (
            (Path("/proc") / str(pid) / "stat")
            .read_text()
            .rsplit(")", 1)[1]
            .split()[0]
        )
    except (FileNotFoundError, ProcessLookupError):
        # A process can disappear after /proc/<pid>/stat opens but before its read.
        return False
    return status not in {"Z", "X", "x"}


def test_timeout_reaps_cross_session_child_without_touching_sibling(tmp_path, monkeypatch):
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

        deadline = time.monotonic() + 1
        while _process_is_live(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _process_is_live(pid)
        assert sibling.poll() is None
    finally:
        sibling.kill()
        sibling.wait()

    _check_owned_session_lifetimes(tmp_path, monkeypatch)
    _check_owned_anchor_contracts(monkeypatch)


@pytest.mark.parametrize("error", [FileNotFoundError, ProcessLookupError])
def test_liveness_observation_handles_process_exit_during_read(monkeypatch, error):
    def disappeared(path):
        assert path == Path("/proc/12345/stat")
        raise error("process exited during observation")

    monkeypatch.setattr(Path, "read_text", disappeared)
    assert not _process_is_live(12345)


@pytest.mark.parametrize("state, live", [("R", True), ("S", True), ("Z", False)])
def test_liveness_observation_distinguishes_running_from_zombie(monkeypatch, state, live):
    monkeypatch.setattr(Path, "read_text", lambda _: f"12345 (python) {state}")
    assert _process_is_live(12345) is live
    if state == "Z":
        # Dead can remain visible during reap; Z -> X must never revive a PID.
        observations = iter(("Z", "X", "x"))
        monkeypatch.setattr(Path, "read_text", lambda _: f"12345 (p) worker) {next(observations)}")
        assert [_process_is_live(12345) for _ in range(3)] == [False, False, False]
    elif state == "R":
        # Sleeping, stopped, parked and uninterruptible tasks are still live.
        for active in ("R", "S", "D", "T", "t", "I", "P", "?"):
            monkeypatch.setattr(Path, "read_text", lambda _: f"12345 (p) worker) {active}")
            assert _process_is_live(12345) is True


def test_liveness_observation_does_not_ignore_access_errors(monkeypatch):
    def denied(_):
        raise PermissionError("/proc stat access denied")

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(PermissionError, match="access denied"):
        _process_is_live(12345)


def _check_owned_session_lifetimes(tmp_path, monkeypatch):
    """A retained leader owns reparented engines, not an independent sentinel."""
    original_stop = bounded._stop_owned_session
    original_reap = bounded._OwnedSessionProcess.reap_after_cleanup
    cleaned = set()
    reaped = []

    def stop(session, started, *, deadline):
        assert bounded._process_identity(session)[:2] == (started, session)
        original_stop(session, started, deadline=deadline)
        assert bounded._process_identity(session) == (started, session, "Z")
        cleaned.add(session)

    def reap(process, deadline):
        assert process.pid in cleaned
        result = original_reap(process, deadline)
        reaped.append(process.pid)
        assert process.returncode == result and not process._hold_leader
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(bounded, "_stop_owned_session", stop)
        scoped.setattr(bounded._OwnedSessionProcess, "reap_after_cleanup", reap)
        for mode in ("normal", "nonzero", "timeout"):
            receipt = tmp_path / f"owned-{mode}.json"
            engine = (
                "import json,os,pathlib,time;os.setpgrp();"
                + ("child=os.fork();os._exit(0) if child else None;" if mode == "timeout" else "")
                + "open(" + repr(str(receipt)) + ",'w').write(json.dumps({"
                "'pid':os.getpid(),'start':int(pathlib.Path('/proc/self/stat').read_text()"
                ".rsplit(')',1)[1].split()[19]),'session':os.getsid(0)}));time.sleep(3)"
            )
            pipes = "" if mode == "timeout" else ",stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL"
            wrapper = (
                "import pathlib,subprocess,sys,time;child=subprocess.Popen([sys.executable,'-c',"
                + repr(engine) + "]" + pipes + ");"
                + ("child.wait(timeout=0.5);" if mode == "timeout" else "")
                + "deadline=time.monotonic()+0.5;\nwhile not pathlib.Path("
                + repr(str(receipt)) + ").exists() and time.monotonic()<deadline:time.sleep(0.005)\n"
                "print('partial-output',flush=True);print('partial-error',file=sys.stderr,flush=True);"
                + ("time.sleep(3)" if mode == "timeout" else f"sys.exit({7 if mode == 'nonzero' else 0})")
            )
            sentinel = subprocess.Popen(
                [sys.executable, "-c", "import time;time.sleep(8)"],
                start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            published = None
            started = time.monotonic()
            try:
                options = dict(timeout=0.15 if mode == "timeout" else 1,
                               capture_output=True, text=True, check=True,
                               cleanup_owned_session=True)
                if mode == "normal":
                    result = run_bounded_process([sys.executable, "-c", wrapper], **options)
                    assert result.returncode == 0
                else:
                    expected = subprocess.TimeoutExpired if mode == "timeout" else subprocess.CalledProcessError
                    with pytest.raises(expected) as raised:
                        run_bounded_process([sys.executable, "-c", wrapper], **options)
                    result = raised.value
                    assert not hasattr(result, "cleanup_error")
                    if mode == "nonzero":
                        assert result.returncode == 7
                assert time.monotonic() - started < 1.5
                assert "partial-output" in bounded.subprocess_text(result.stdout)
                assert "partial-error" in bounded.subprocess_text(result.stderr)
                published = json.loads(receipt.read_text())
                assert not _process_is_live(published["pid"])
                assert sentinel.poll() is None
            finally:
                if published is None and receipt.exists():
                    published = json.loads(receipt.read_text())
                if published is not None:
                    current = bounded._process_identity(published["pid"])
                    if current is not None and current[0] == published["start"] and current[2] != "Z":
                        os.kill(published["pid"], signal.SIGKILL)
                if sentinel.poll() is None:
                    sentinel.terminate()
                try:
                    sentinel.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    sentinel.kill()
                    sentinel.wait(timeout=0.5)
    assert len(reaped) == len(cleaned) == 3


def _check_owned_anchor_contracts(monkeypatch):
    def fake_process():
        process = object.__new__(bounded._OwnedSessionProcess)
        process.args, process.pid, process.returncode = ("fixture",), 12345, None
        process._hold_leader, process._anchor_lost = True, False
        process._observed_returncode = None
        process._waitpid_lock = threading.Lock()
        process._child_created = False
        process.stdout, process.stderr = object(), object()
        process._fileobj2output = {process.stdout: [b"partial-out"], process.stderr: [b"partial-err"]}
        return process

    with monkeypatch.context() as scoped:
        events = []
        observed = SimpleNamespace(si_pid=12345, si_code=os.CLD_EXITED, si_status=7)
        scoped.setattr(os, "waitid", lambda *args: (events.append("peek"), observed)[1])
        scoped.setattr(os, "waitpid", lambda *args: (events.append("reap"), (12345, 7 << 8))[1])
        process = fake_process()
        assert process.wait(timeout=0.01) == process.poll() == 7
        assert process.returncode is None and "reap" not in events
        assert process.reap_after_cleanup(time.monotonic() + 0.01) == 7
        assert process.reap_after_cleanup(time.monotonic() + 0.01) == 7
        assert events.count("reap") == 1

        scoped.setattr(os, "waitid", lambda *args: None)
        process = fake_process()
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0)
        assert process.returncode is None and events.count("reap") == 1

        def lost(*args):
            raise ChildProcessError("another waiter consumed leader")
        scoped.setattr(os, "waitid", lost)
        process = fake_process()
        with pytest.raises(ProcessCleanupError) as failure:
            process.poll()
        assert failure.value.stdout == b"partial-out" and failure.value.stderr == b"partial-err"
        with pytest.raises(ProcessCleanupError):
            process.reap_after_cleanup(time.monotonic() + 0.01)
        assert events.count("reap") == 1

    with monkeypatch.context() as scoped:
        scoped.setattr(signal, "getsignal", lambda value: signal.SIG_IGN)
        scoped.setattr(subprocess.Popen, "__init__", lambda *args, **kwargs: pytest.fail("unqualified spawn"))
        with pytest.raises(ProcessCleanupError, match="default SIGCHLD"):
            bounded._OwnedSessionProcess(("fixture",))

    for owner, feature in ((os, "pidfd_open"), (signal, "pidfd_send_signal")):
        with monkeypatch.context() as scoped:
            scoped.delattr(owner, feature)
            scoped.setattr(subprocess.Popen, "__init__", lambda *args, **kwargs: pytest.fail("spawn without pidfds"))
            with pytest.raises(ProcessCleanupError, match="pidfds"):
                bounded._OwnedSessionProcess(("fixture",))

    with monkeypatch.context() as scoped:
        scoped.setattr(sys, "implementation", SimpleNamespace(name="pypy"))
        scoped.setattr(subprocess.Popen, "__init__", lambda *args, **kwargs: pytest.fail("non-CPython spawn"))
        with pytest.raises(ProcessCleanupError, match="CPython"):
            bounded._OwnedSessionProcess(("fixture",))

    for identity, message in ((None, "anchor lost"), ((99, 12345, "Z"), "generation changed")):
        with monkeypatch.context() as scoped:
            scoped.setattr(bounded, "_process_identity", lambda pid: identity)
            scoped.setattr(os, "kill", lambda *args: pytest.fail("unowned PID signalled"))
            scoped.setattr(os, "killpg", lambda *args: pytest.fail("unowned PGID signalled"))
            with pytest.raises(RuntimeError, match=message):
                bounded._stop_owned_session(12345, 42, deadline=time.monotonic())
            process = SimpleNamespace(pid=12345, returncode=None, args=("fixture",),
                communicate=lambda **kwargs: pytest.fail("unowned drain"),
                reap_after_cleanup=lambda deadline: None)
            error = subprocess.TimeoutExpired(("fixture",), 0.15, output=b"out", stderr=b"err")
            stdout, stderr, cleanup = bounded._failed_process_output(
                process, (42, 12345, "S"), error, owned_session=True)
            assert stdout == b"out" and stderr == b"err" and message in str(cleanup)
