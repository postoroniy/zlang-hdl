"""Bounded tool execution, output normalization and explicit session ownership."""


from __future__ import annotations


import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def _linux_process(pid):
    try:
        # comm may contain spaces or parentheses; field 22 is starttime.
        text = (Path('/proc') / str(pid) / 'stat').read_text()
        fields = text[text.rfind(')') + 2:].split()
        return int(fields[1]), fields[19]
    except (OSError, ValueError, IndexError):
        return None


def _owned_descendants(root):
    """Snapshot children before killing a wrapper that may start new sessions."""
    if not Path('/proc/self/stat').is_file():
        return ()
    processes = {int(p.name): value for p in Path('/proc').iterdir()
                 if p.name.isdigit() and (value := _linux_process(int(p.name))) is not None}
    owned, frontier = [], {root}
    while frontier:
        children = {pid for pid, (parent, _) in processes.items() if parent in frontier}
        owned.extend((pid, processes[pid][1]) for pid in sorted(children))
        frontier = children
    return tuple(reversed(owned))


def _process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return int(fields[19]), int(fields[3]), fields[0]


def _require_session_generation(session, started):
    leader = _process_identity(session)
    if leader is None:
        raise RuntimeError("owned formal-tool session leader anchor lost")
    if leader[:2] != (started, session):
        raise RuntimeError("owned formal-tool session generation changed")


def _stop_owned_session(session, started, *, deadline=None):
    """Stop only active members of this invocation's new Linux session.

    SBY gives its engines separate process groups. Session membership survives
    both that change and reparenting after SBY exits. Starttime rechecks and
    required pidfds prevent recycled PIDs inheriting ownership.
    """
    if deadline is None:
        deadline = time.monotonic() + 1
    while True:
        _require_session_generation(session, started)
        active = {}
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            pid = int(path.name)
            try:
                if os.getsid(pid) != session:
                    continue
                identity = _process_identity(pid)
            except ProcessLookupError:
                continue
            if identity is not None and identity[1] == session and identity[2] != "Z":
                if identity[0] < started:
                    raise RuntimeError("owned session has an invalid process generation")
                active[pid] = identity[:2]
        if not active:
            return
        for pid, identity in active.items():
            descriptor = None
            try:
                descriptor = os.pidfd_open(pid)
                current = _process_identity(pid)
                if current is None or current[:2] != identity or current[2] == "Z":
                    continue
                _require_session_generation(session, started)
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        if time.monotonic() >= deadline:
            raise RuntimeError("owned formal-tool session did not exit within cleanup bound")
        time.sleep(0.005)


class ProcessCleanupError(RuntimeError):
    """A tool completed, but its owned process lifetime was not confirmed."""

    def __init__(self, args, cleanup_error, *, returncode, stdout, stderr):
        super().__init__(f"owned process cleanup failed: {cleanup_error}")
        self.cmd = args
        self.cleanup_error = cleanup_error
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _OwnedSessionProcess(subprocess.Popen):
    """Observe exit without releasing the PID until owned cleanup completes.

    Linux/CPython owned-only adapter; the caller is the exclusive wait owner.
    Every caller must invoke reap_after_cleanup in finally under its existing
    cleanup deadline. Destruction cannot release a still-active session lease.
    """

    def __init__(self, args, **options):
        self._hold_leader = self._anchor_lost = False
        self._observed_returncode = None
        required = ("waitid", "WNOWAIT", "WEXITED", "WNOHANG", "P_PID",
                    "CLD_EXITED", "CLD_KILLED", "CLD_DUMPED", "pidfd_open")
        if (sys.implementation.name != "cpython"
                or not all(hasattr(os, name) for name in required)
                or not hasattr(signal, "pidfd_send_signal")
                or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL):
            raise ProcessCleanupError(args, RuntimeError("owned session requires CPython, pidfds, WNOWAIT and default SIGCHLD"),
                                      returncode=None, stdout=None, stderr=None)
        super().__init__(args, **options)
        self._hold_leader = True

    def _anchor_failure(self, reason="owned session leader anchor lost"):
        self._anchor_lost = True
        buffers = getattr(self, "_fileobj2output", {})

        def partial(name):
            chunks = buffers.get(getattr(self, name, None))
            return None if chunks is None else b"".join(chunks)

        return ProcessCleanupError(self.args, RuntimeError(reason), returncode=self.returncode,
                                   stdout=partial("stdout"), stderr=partial("stderr"))

    def _peek_exit(self):
        if self._anchor_lost:
            raise self._anchor_failure()
        try:
            with self._waitpid_lock:
                observed = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except OSError as error:
            raise self._anchor_failure() from error
        if observed is None or observed.si_pid == 0:
            return None
        if observed.si_pid != self.pid or observed.si_code not in (os.CLD_EXITED, os.CLD_KILLED, os.CLD_DUMPED):
            raise self._anchor_failure("owned session exit observation changed")
        code = observed.si_status if observed.si_code == os.CLD_EXITED else -observed.si_status
        if self._observed_returncode is not None and self._observed_returncode != code:
            raise self._anchor_failure("owned session exit status changed")
        self._observed_returncode = code
        return code

    def _wait(self, timeout):
        if not self._hold_leader:
            return super()._wait(timeout)
        if timeout is None:
            raise RuntimeError("owned session wait requires a timeout")
        deadline = time.monotonic() + max(0, timeout)
        while True:
            code = self._peek_exit()
            if code is not None:
                return code
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(min(0.005, remaining))

    def _internal_poll(self, _deadstate=None):
        if self._hold_leader:
            return self._peek_exit()
        return super()._internal_poll(_deadstate=_deadstate)

    def reap_after_cleanup(self, deadline):
        if self._anchor_lost:
            raise self._anchor_failure()
        if not self._hold_leader:
            return self.returncode
        observed = self._wait(max(0, deadline - time.monotonic()))
        try:
            with self._waitpid_lock:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
        except OSError as error:
            raise self._anchor_failure() from error
        if pid != self.pid:
            raise self._anchor_failure("owned session reap correspondence changed")
        self.returncode = os.waitstatus_to_exitcode(status)
        self._hold_leader = False
        if self.returncode != observed:
            raise self._anchor_failure("owned session reaped status differs from observation")
        return self.returncode

    def __del__(self):
        if getattr(self, "_hold_leader", False) or getattr(self, "_anchor_lost", False):
            return
        super().__del__()


def _reap_cleanup_error(process, deadline, cleanup_error):
    try:
        process.reap_after_cleanup(deadline)
    except Exception as failure:
        if cleanup_error is None:
            return failure
        return RuntimeError(f"{cleanup_error}; leader reap also failed: {failure}")
    return cleanup_error


def _failed_process_output(process, identity, error, *, owned_session=False):
    """Preserve the primary failure within one shared cleanup/drain/reap second."""
    deadline = time.monotonic() + 1
    stdout, stderr = getattr(error, "stdout", None), getattr(error, "stderr", None)
    cleanup_error = None
    try:
        # Preserve the default failure-path ownership snapshot, including
        # descendants that chose a different session. This default path is
        # independent from the opt-in formal session lease below.
        if not owned_session:
            try:
                descendants = _owned_descendants(process.pid)
                for pid, starttime in descendants:
                    current = _linux_process(pid)
                    if current is not None and current[1] == starttime:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
            except Exception as failure:
                cleanup_error = failure
        try:
            if owned_session and identity is None:
                raise RuntimeError("owned formal-tool session leader identity unavailable")
            if identity is not None:
                _require_session_generation(process.pid, identity[0])
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        except Exception as failure:
            cleanup_error = failure
        if identity is not None:
            try:
                _stop_owned_session(process.pid, identity[0], deadline=deadline)
            except Exception as failure:
                cleanup_error = failure
        # No signal or drain is authorized after loss of the retained leader.
        # The original exception already carries any captured partial output.
        if cleanup_error is None:
            try:
                stdout, stderr = process.communicate(timeout=max(0, deadline - time.monotonic()))
            except Exception as failure:
                cleanup_error = failure
                if isinstance(failure, subprocess.TimeoutExpired):
                    if failure.stdout is not None:
                        stdout = failure.stdout
                    if failure.stderr is not None:
                        stderr = failure.stderr
        if not owned_session and process.returncode is None:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except Exception as failure:
                if cleanup_error is None:
                    cleanup_error = failure
    finally:
        if owned_session:
            cleanup_error = _reap_cleanup_error(process, deadline, cleanup_error)
    return stdout, stderr, cleanup_error


def run_bounded_process(args, *, timeout, capture_output=False, check=False,
                        cleanup_owned_session=False, **options):
    """Run one tool-owned process tree under one unchanged wall deadline.

    On POSIX the process starts in a dedicated session/process group. Default
    callers retain failure-path descendant cleanup, including cross-session
    children. Opt-in formal cleanup additionally holds a Linux/CPython session
    leader until every same-session engine is stopped and the leader reaped.
    Owned cleanup requires pidfds, default SIGCHLD and exclusive wait ownership;
    an unavailable or uncertain lifetime never establishes formal acceptance.
    """
    if capture_output:
        if "stdout" in options or "stderr" in options:
            raise ValueError("capture_output cannot be combined with stdout/stderr")
        options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if cleanup_owned_session and (os.name != "posix" or not Path("/proc/self/stat").is_file()):
        raise ProcessCleanupError(
            args, RuntimeError("formal-tool session cleanup requires Linux /proc"),
            returncode=None, stdout=None, stderr=None,
        )
    factory = _OwnedSessionProcess if cleanup_owned_session else subprocess.Popen
    process = factory(args, start_new_session=os.name == "posix", **options)
    try:
        identity = None
        try:
            identity = _process_identity(process.pid) if cleanup_owned_session else None
            if cleanup_owned_session and (identity is None or identity[1] != process.pid):
                raise ProcessCleanupError(args, RuntimeError("cannot bind the owned formal-tool session"),
                                          returncode=None, stdout=None, stderr=None)
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException as error:
            # An unbound identity cannot authorize scanning a Linux session.
            owned = identity if identity is not None and identity[1] == process.pid else None
            stdout, stderr, cleanup_error = _failed_process_output(
                process, owned, error, owned_session=cleanup_owned_session)
            if isinstance(error, subprocess.TimeoutExpired):
                failure = subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr)
                if cleanup_error is not None:
                    failure.cleanup_error = cleanup_error
                    failure.add_note(f"process cleanup also failed: {cleanup_error}")
                raise failure from error
            if cleanup_error is not None:
                error.cleanup_error = cleanup_error
                error.add_note(f"process cleanup also failed: {cleanup_error}")
            raise
        cleanup_error = None
        if cleanup_owned_session:
            deadline = time.monotonic() + 1
            try:
                _stop_owned_session(process.pid, identity[0], deadline=deadline)
            except Exception as error:
                cleanup_error = error
            finally:
                cleanup_error = _reap_cleanup_error(process, deadline, cleanup_error)
        result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        if check:
            try:
                result.check_returncode()
            except subprocess.CalledProcessError as error:
                if cleanup_error is not None:
                    error.cleanup_error = cleanup_error
                    error.add_note(f"process cleanup also failed: {cleanup_error}")
                raise
        if cleanup_error is not None:
            raise ProcessCleanupError(args, cleanup_error, returncode=process.returncode,
                                      stdout=stdout, stderr=stderr) from cleanup_error
        return result
    finally:
        # Popen.__exit__ waits without a deadline. All waits above are explicit;
        # a cleanup failure must not turn exit from this function into a hang.
        for name in ("stdout", "stderr"):
            stream = getattr(process, name, None)
            reader = getattr(process, f"_{name}_thread", None)
            # Non-POSIX communicate may still have a daemon reader blocked on
            # an inherited pipe. It closes that stream on EOF; close() here
            # must not acquire its I/O lock after bounded cleanup has failed.
            if stream is not None and (reader is None or not reader.is_alive()):
                stream.close()


def subprocess_text(value: str | bytes | None) -> str:
    """Return one captured subprocess stream as deterministic text.

    ``subprocess.TimeoutExpired`` can retain partial output as bytes even when
    the original run requested ``text=True``. Tool callers should not each
    carry a subtly different bytes/None fallback.
    """

    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


__all__ = ["ProcessCleanupError", "run_bounded_process", "subprocess_text"]
