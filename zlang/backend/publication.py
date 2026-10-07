"""No-follow, atomic publication of backend-owned relative files."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import secrets
import stat
from typing import Iterable, Literal


class SafePublicationError(ValueError):
    """A relative backend product cannot be published without path ambiguity."""


ExistingPolicy = Literal["replace", "identical"]


def _normalized_files(
    files: Iterable[tuple[Path, bytes]],
) -> tuple[tuple[Path, bytes], ...]:
    normalized: list[tuple[Path, bytes]] = []
    seen: set[Path] = set()
    for raw_relative, payload in files:
        relative = Path(raw_relative)
        parts = relative.parts
        if (
            not parts
            or relative.is_absolute()
            or any(
                part in {"", ".", ".."} or Path(part).name != part
                for part in parts
            )
        ):
            raise SafePublicationError(
                f"unsafe relative publication path '{relative}'"
            )
        if relative in seen:
            raise SafePublicationError(
                f"duplicate relative publication path '{relative}'"
            )
        if not isinstance(payload, bytes):
            raise SafePublicationError(
                f"publication payload for '{relative}' must be bytes"
            )
        seen.add(relative)
        normalized.append((relative, payload))
    return tuple(sorted(normalized, key=lambda item: item[0].as_posix()))


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_root(directory: Path, *, create: bool) -> int:
    absolute = Path(os.path.abspath(directory))
    flags = _directory_flags()
    descriptor = os.open(absolute.anchor or os.sep, flags)
    try:
        for component in absolute.parts[1:]:
            if create:
                try:
                    os.mkdir(component, dir_fd=descriptor)
                except FileExistsError:
                    pass
                else:
                    os.fsync(descriptor)
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as error:
                raise SafePublicationError(
                    "publication root contains a symbolic link or non-directory "
                    f"component: '{directory}'"
                ) from error
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_parent(
    root_descriptor: int,
    relative: Path,
    *,
    create: bool,
) -> tuple[int, str]:
    descriptor = os.dup(root_descriptor)
    flags = _directory_flags()
    try:
        for component in relative.parts[:-1]:
            if create:
                try:
                    os.mkdir(component, dir_fd=descriptor)
                except FileExistsError:
                    pass
                else:
                    os.fsync(descriptor)
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as error:
                raise SafePublicationError(
                    "publication destination contains a symbolic link or "
                    f"non-directory component: '{relative}'"
                ) from error
            os.close(descriptor)
            descriptor = child
        return descriptor, relative.parts[-1]
    except Exception:
        os.close(descriptor)
        raise


def _read_leaf(
    parent_descriptor: int,
    leaf: str,
    relative: Path,
) -> tuple[bytes, int] | None:
    try:
        metadata = os.stat(
            leaf, dir_fd=parent_descriptor, follow_symlinks=False
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SafePublicationError(
            f"cannot inspect publication destination '{relative}'"
        ) from error
    if stat.S_ISLNK(metadata.st_mode):
        raise SafePublicationError(
            f"publication destination is a symbolic link: '{relative}'"
        )
    if not stat.S_ISREG(metadata.st_mode):
        raise SafePublicationError(
            f"publication destination is not a regular file: '{relative}'"
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(leaf, flags, dir_fd=parent_descriptor)
    except OSError as error:
        raise SafePublicationError(
            f"cannot safely open publication destination '{relative}'"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise SafePublicationError(
                f"publication destination is not a regular file: '{relative}'"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), stat.S_IMODE(opened.st_mode)
    finally:
        os.close(descriptor)


def _publish_leaf(
    parent_descriptor: int,
    leaf: str,
    relative: Path,
    payload: bytes,
    *,
    policy: ExistingPolicy,
    existing_mode: int | None,
) -> None:
    temporary_name: str | None = None
    temporary_descriptor: int | None = None
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        for _ in range(100):
            candidate = f".{leaf}.zlang-{secrets.token_hex(8)}.tmp"
            try:
                temporary_descriptor = os.open(
                    candidate, flags, 0o666, dir_fd=parent_descriptor
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary_descriptor is None or temporary_name is None:
            raise SafePublicationError(
                f"cannot allocate temporary publication for '{relative}'"
            )
        if existing_mode is not None:
            os.fchmod(temporary_descriptor, existing_mode)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(temporary_descriptor, remaining)
            if written <= 0:
                raise OSError("short write while publishing backend product")
            remaining = remaining[written:]
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = None

        current = _read_leaf(parent_descriptor, leaf, relative)
        if current is not None and policy == "identical":
            if current[0] == payload:
                return
            raise SafePublicationError(
                f"publication collides with existing regular file '{relative}'"
            )
        os.replace(
            temporary_name,
            leaf,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        temporary_name = None
        os.fsync(parent_descriptor)
    except SafePublicationError:
        raise
    except OSError as error:
        raise SafePublicationError(
            f"cannot publish backend product '{relative}'"
        ) from error
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass


def publish_relative_files(
    directory: Path,
    files: Iterable[tuple[Path, bytes]],
    *,
    existing: ExistingPolicy,
) -> tuple[Path, ...]:
    """Preflight then atomically publish files below one no-follow root."""

    if existing not in {"replace", "identical"}:
        raise SafePublicationError(f"unsupported existing-file policy '{existing}'")
    normalized = _normalized_files(files)
    directory = Path(directory)
    root_descriptor = _open_root(directory, create=True)
    opened: list[tuple[Path, bytes, int | None]] = []
    try:
        # Validate the complete set before writing its first leaf.
        for relative, payload in normalized:
            parent_descriptor, leaf = _open_parent(
                root_descriptor, relative, create=True
            )
            try:
                current = _read_leaf(parent_descriptor, leaf, relative)
            finally:
                os.close(parent_descriptor)
            if current is not None and existing == "identical":
                if current[0] != payload:
                    raise SafePublicationError(
                        f"publication collides with existing regular file '{relative}'"
                    )
                continue
            opened.append((relative, payload, current[1] if current else None))

        for relative, payload, mode in opened:
            parent_descriptor, leaf = _open_parent(
                root_descriptor, relative, create=False
            )
            try:
                _publish_leaf(
                    parent_descriptor,
                    leaf,
                    relative,
                    payload,
                    policy=existing,
                    existing_mode=mode,
                )
            finally:
                os.close(parent_descriptor)

        # Detect concurrent deletion or mutation before reporting success.
        for relative, expected in normalized:
            parent_descriptor, leaf = _open_parent(
                root_descriptor, relative, create=False
            )
            try:
                current = _read_leaf(parent_descriptor, leaf, relative)
            finally:
                os.close(parent_descriptor)
            if current is None or current[0] != expected:
                raise SafePublicationError(
                    f"published file '{relative}' does not match its expected contents"
                )
    finally:
        os.close(root_descriptor)
    return tuple(directory / relative for relative, _ in normalized)


def validate_relative_files(
    directory: Path,
    files: Iterable[tuple[Path, bytes]],
) -> None:
    """Validate exact relative payloads without following any path symlink."""

    normalized = _normalized_files(files)
    if not normalized:
        return
    directory = Path(directory)
    root_descriptor = _open_root(directory, create=False)
    try:
        for relative, expected in normalized:
            parent_descriptor, leaf = _open_parent(
                root_descriptor, relative, create=False
            )
            try:
                current = _read_leaf(parent_descriptor, leaf, relative)
            finally:
                os.close(parent_descriptor)
            if current is None:
                raise SafePublicationError(
                    f"required publication '{relative}' is missing"
                )
            if current[0] != expected:
                raise SafePublicationError(
                    f"published file '{relative}' does not match its expected contents"
                )
    finally:
        os.close(root_descriptor)


def validate_relative_hashes(
    directory: Path,
    files: Iterable[tuple[Path, str]],
) -> None:
    """Validate content-addressed relative files without requiring payloads."""

    normalized: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for raw_relative, expected_hash in files:
        relative = Path(raw_relative)
        # Reuse the exact public relative-path validation.
        _normalized_files(((relative, b""),))
        if relative in seen:
            raise SafePublicationError(
                f"duplicate relative publication path '{relative}'"
            )
        if not isinstance(expected_hash, str) or (
            len(expected_hash) != 64
            or any(
                character not in "0123456789abcdef"
                for character in expected_hash
            )
        ):
            raise SafePublicationError(
                f"invalid expected content hash for '{relative}'"
            )
        seen.add(relative)
        normalized.append((relative, expected_hash))
    if not normalized:
        return
    normalized.sort(key=lambda item: item[0].as_posix())
    directory = Path(directory)
    root_descriptor = _open_root(directory, create=False)
    try:
        for relative, expected_hash in normalized:
            parent_descriptor, leaf = _open_parent(
                root_descriptor, relative, create=False
            )
            try:
                current = _read_leaf(parent_descriptor, leaf, relative)
            finally:
                os.close(parent_descriptor)
            if current is None:
                raise SafePublicationError(
                    f"required publication '{relative}' is missing"
                )
            if hashlib.sha256(current[0]).hexdigest() != expected_hash:
                raise SafePublicationError(
                    f"published file '{relative}' does not match its expected hash"
                )
    finally:
        os.close(root_descriptor)


__all__ = [
    "SafePublicationError",
    "publish_relative_files",
    "validate_relative_files",
    "validate_relative_hashes",
]
