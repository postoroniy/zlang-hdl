"""Bound the compiler process behind the stdio LSP transport.

The protocol-facing process owns no ZLang semantic state.  A worker runs the
unchanged language server; if a compiler query stops making progress, the
supervisor terminates that worker instead of letting it consume the editor's
memory indefinitely.  The editor's normal language-client restart path then
reopens its documents against a fresh compiler session.
"""

from __future__ import annotations

from collections.abc import Sequence
import subprocess
import sys
import threading
import time
from typing import BinaryIO


WORKER_TIMEOUT_SECONDS = 60.0
WORKER_ADDRESS_SPACE_BYTES = 2 * 1024 * 1024 * 1024


def limit_worker_address_space() -> None:
    """Apply a Linux process ceiling without changing compiler semantics."""

    if not sys.platform.startswith("linux"):
        return
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    selected = (
        WORKER_ADDRESS_SPACE_BYTES
        if hard == resource.RLIM_INFINITY
        else min(WORKER_ADDRESS_SPACE_BYTES, hard)
    )
    if soft == resource.RLIM_INFINITY or soft > selected:
        resource.setrlimit(resource.RLIMIT_AS, (selected, hard))


def supervise_stdio(
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    *,
    command: Sequence[str] | None = None,
    timeout_seconds: float = WORKER_TIMEOUT_SECONDS,
) -> int:
    """Forward standard LSP frames and bound outstanding worker operations."""

    from zlang.lsp.server import LspProtocolError, _error, read_message, write_message

    if timeout_seconds <= 0:
        raise ValueError("LSP worker timeout must be positive")
    worker_command = tuple(command or (
        sys.executable, "-c",
        "from zlang.lsp.server import main; "
        "raise SystemExit(main(['--worker']))",
    ))
    worker = subprocess.Popen(
        worker_command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        bufsize=0,
    )
    assert worker.stdin is not None and worker.stdout is not None
    pending: dict[tuple[str, object], float] = {}
    versions: dict[str, int | None] = {}
    pending_lock = threading.Lock()
    output_lock = threading.Lock()
    reader_finished = threading.Event()
    transport_errors: list[Exception] = []

    def incoming() -> None:
        try:
            while True:
                message = read_message(input_stream)
                if message is None:
                    break
                if isinstance(message, dict):
                    method = message.get("method")
                    params = message.get("params")
                    document = (
                        params.get("textDocument")
                        if isinstance(params, dict) else None
                    )
                    uri = (
                        document.get("uri")
                        if isinstance(document, dict) else None
                    )
                    version = (
                        document.get("version")
                        if isinstance(document, dict) else None
                    )
                    version = (
                        version
                        if isinstance(version, int) and not isinstance(version, bool)
                        else None
                    )
                    with pending_lock:
                        if method == "textDocument/didOpen" and isinstance(uri, str):
                            versions[uri] = version
                            pending[("diagnostics", uri)] = time.monotonic()
                        elif method == "textDocument/didChange" and isinstance(uri, str):
                            previous = versions.get(uri)
                            if (
                                previous is None or version is None
                                or version >= previous
                            ):
                                versions[uri] = version
                                pending[("diagnostics", uri)] = time.monotonic()
                        elif method == "textDocument/didClose" and isinstance(uri, str):
                            versions.pop(uri, None)
                            pending.pop(("diagnostics", uri), None)
                        request_id = message.get("id")
                        if (
                            method is not None
                            and "id" in message
                            and (
                                request_id is None
                                or isinstance(request_id, (str, int))
                            )
                        ):
                            pending[("request", request_id)] = time.monotonic()
                write_message(worker.stdin, message)
        except LspProtocolError as error:
            # The unsupervised server reports malformed input as a JSON-RPC
            # parse error.  Preserve that protocol behavior at this boundary.
            try:
                with output_lock:
                    write_message(output_stream, _error(None, -32700, str(error)))
            except (BrokenPipeError, OSError):
                pass
            transport_errors.append(error)
            if worker.poll() is None:
                worker.kill()
        except (BrokenPipeError, OSError) as error:
            transport_errors.append(error)
        finally:
            try:
                worker.stdin.close()
            except OSError:
                pass

    def outgoing() -> None:
        try:
            while True:
                message = read_message(worker.stdout)
                if message is None:
                    break
                if isinstance(message, dict):
                    params = message.get("params")
                    with pending_lock:
                        request_id = message.get("id")
                        if "id" in message and (
                            request_id is None
                            or isinstance(request_id, (str, int))
                        ):
                            pending.pop(("request", request_id), None)
                        if (
                            message.get("method")
                            == "textDocument/publishDiagnostics"
                            and isinstance(params, dict)
                        ):
                            pending.pop(("diagnostics", params.get("uri")), None)
                with output_lock:
                    write_message(output_stream, message)
        except (BrokenPipeError, OSError, LspProtocolError) as error:
            transport_errors.append(error)
        finally:
            reader_finished.set()

    threading.Thread(
        target=incoming, name="zlang-lsp-supervisor-input", daemon=True
    ).start()
    threading.Thread(
        target=outgoing, name="zlang-lsp-supervisor-output", daemon=True
    ).start()
    timed_out = False
    while worker.poll() is None:
        with pending_lock:
            oldest = min(pending.values(), default=None)
        if oldest is not None and time.monotonic() - oldest > timeout_seconds:
            timed_out = True
            worker.kill()
            break
        time.sleep(0.05)
    exit_code = worker.wait()
    reader_finished.wait(timeout=2)
    if timed_out:
        print(
            "zlang-lsp: compiler worker exceeded the bounded request time; "
            "the editor must restart the language server",
            file=sys.stderr,
        )
        return 124
    if transport_errors and exit_code == 0:
        print(f"zlang-lsp: transport failure: {transport_errors[0]}", file=sys.stderr)
        return 1
    if exit_code != 0:
        print(
            "zlang-lsp: compiler worker exited unexpectedly; "
            "the editor must restart the language server",
            file=sys.stderr,
        )
        return exit_code if exit_code > 0 else 1
    return 0


__all__ = ["limit_worker_address_space", "supervise_stdio"]
