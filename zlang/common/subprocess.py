"""Small subprocess value normalizers shared by compiler tool runners."""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path


def _linux_process(pid):
    try:
        # comm may contain spaces or parentheses; field 22 is starttime.
        text = (Path('/proc') / str(pid) / 'stat').read_text()
        fields = text[text.rfind(')') + 2:].split()
        return int(fields[1]), fields[19]
    except (OSError, ValueError, IndexError):
        return None


def _owned_descendants(root):
    """Snapshot children before killing a wrapper; SBY starts separate sessions."""
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


def run_bounded_process(args, *, timeout, capture_output=False, check=False, **options):
    """Run one tool-owned process tree under one unchanged wall deadline.

    On POSIX every descendant inherits a dedicated session/process group.
    Killing only a wrapper (SBY, time, make) leaves its solver/compiler alive.
    No unrelated process group is ever signalled by this owner.
    """
    if capture_output:
        if "stdout" in options or "stderr" in options:
            raise ValueError("capture_output cannot be combined with stdout/stderr")
        options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with subprocess.Popen(args, start_new_session=os.name == "posix", **options) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException as error:
            descendants = _owned_descendants(process.pid)
            for pid, starttime in descendants:
                identity = _linux_process(pid)
                if identity is not None and identity[1] == starttime:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            if isinstance(error, subprocess.TimeoutExpired):
                raise subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr) from error
            raise
        result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result


def subprocess_text(value: str | bytes | None) -> str:
    """Return one captured subprocess stream as deterministic text.

    ``subprocess.TimeoutExpired`` can retain partial output as bytes even when
    the original run requested ``text=True``. Tool callers should not each
    carry a subtly different bytes/None fallback.
    """

    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


__all__ = ["run_bounded_process", "subprocess_text"]
