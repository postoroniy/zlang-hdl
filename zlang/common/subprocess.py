"""Small subprocess value normalizers shared by compiler tool runners."""

from __future__ import annotations

import os
import signal
import subprocess


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
