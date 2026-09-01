"""External Clash and Verilator orchestration for generated hardware."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from zlang.backend.source_map import GeneratedSourceMap
from zlang.backend.clash.public_wrapper import ClashPublicTopWrapper
from zlang.backend.companions import (
    CompanionArtifact,
    CompanionArtifactError,
    publish_companion_bundle,
)
from zlang.backend.publication import SafePublicationError, publish_relative_files


class ToolchainError(RuntimeError):
    """An optional external compiler is missing or rejected generated code."""


_GENERATED_LOCATION = re.compile(
    r"(?P<path>[^\s:]+\.(?:hs|sv|v)):(?P<line>[1-9][0-9]*)(?::[1-9][0-9]*)?"
)


@dataclass(frozen=True)
class GeneratedDiagnosticContext:
    """One hash-verified source map embedded in a combined formal source."""

    source_map: GeneratedSourceMap
    generated_text: str
    line_offset: int = 0

    def __post_init__(self) -> None:
        if self.line_offset < 0:
            raise ValueError("generated diagnostic line offset must be non-negative")
        if hashlib.sha256(self.generated_text.encode()).hexdigest() != (
            self.source_map.artifact_hash
        ):
            raise ValueError(
                "generated diagnostic text does not match its source-map hash"
            )


def attribute_combined_generated_diagnostic(
    detail: str,
    contexts: tuple[GeneratedDiagnosticContext, ...],
) -> str:
    """Append one exact ZLang origin for a combined formal source diagnostic.

    SBY feeds implementation and checker text to Yosys as one generated file.
    Each source map still authenticates only its original backend artifact, so
    ``line_offset`` translates the combined-file line back to that artifact
    without weakening the map's hash boundary.
    """

    matches: list[object] = []
    for generated_location in _GENERATED_LOCATION.finditer(detail):
        combined_line = int(generated_location.group("line"))
        for context in contexts:
            local_line = combined_line - context.line_offset
            if local_line < 1:
                continue
            matches.extend(context.source_map.entries_for_line(local_line))
    unique = {
        (
            item.semantic_identity,
            item.source_origin.render(),
        ): item
        for item in matches
    }
    if len(unique) != 1:
        return detail
    entry = next(iter(unique.values()))
    origin = entry.source_origin
    unit = origin.source_unit or "<unknown-source>"
    return (
        f"{detail}\nZLang origin: {unit}:{origin.span.render()} "
        f"({origin.construct})"
    )


def attribute_generated_diagnostic(
    detail: str,
    source_map: GeneratedSourceMap | None,
    generated_text: str | None,
) -> str:
    """Append an exact ZLang origin when a verified map covers the tool line.

    Attribution is deliberately fail closed: a missing text payload, a changed
    artifact hash, an unrecognized tool location, or an unmapped line returns
    the diagnostic unchanged.
    """

    if source_map is None or generated_text is None:
        return detail
    if hashlib.sha256(generated_text.encode()).hexdigest() != source_map.artifact_hash:
        return detail
    return attribute_combined_generated_diagnostic(
        detail,
        (GeneratedDiagnosticContext(source_map, generated_text),),
    )


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _configured_executable(value: str) -> str | None:
    """Resolve one explicit executable path or command name.

    An explicit but invalid configuration remains authoritative: callers do not
    silently fall back to another installation after the user selected one.
    """

    candidate = Path(value).expanduser()
    if _is_executable(candidate):
        return str(candidate)
    return shutil.which(value)


def _configured_clash_checkout(executable: str) -> Path | None:
    """Return a configured or structurally inferred Clash source checkout."""

    configured = os.environ.get("ZLANG_CLASH_ROOT")
    if configured:
        checkout = Path(configured).expanduser().resolve(strict=False)
        try:
            Path(executable).expanduser().resolve(strict=False).relative_to(checkout)
        except ValueError:
            return None
        return checkout

    executable_path = Path(executable).expanduser().resolve(strict=False)
    parts = executable_path.parts
    try:
        dist_index = parts.index("dist-newstyle")
    except ValueError:
        return None
    if dist_index == 0:
        return None
    return Path(*parts[:dist_index])


def find_clash_executable() -> str | None:
    configured = os.environ.get("ZLANG_CLASH")
    if configured:
        return _configured_executable(configured)
    if executable := shutil.which("clash"):
        return executable
    configured_root = os.environ.get("ZLANG_CLASH_ROOT")
    if not configured_root:
        return None
    checkout = Path(configured_root).expanduser()
    candidates = checkout.glob(
        "dist-newstyle/build/*/ghc-*/clash-ghc-*/x/clash/build/clash/clash"
    )
    executables = [candidate for candidate in candidates if _is_executable(candidate)]
    if not executables:
        return None
    return str(max(executables, key=lambda candidate: candidate.stat().st_mtime))


def clash_subprocess_environment(executable: str | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    selected = executable or find_clash_executable()
    if not selected:
        return environment
    checkout = _configured_clash_checkout(selected)
    if checkout is None:
        return environment
    executable_path = Path(selected).expanduser().resolve(strict=False)
    try:
        build_index = executable_path.parts.index("build")
        platform = executable_path.parts[build_index + 1]
        ghc_version = executable_path.parts[build_index + 2].removeprefix("ghc-")
        package_environment = checkout / f".ghc.environment.{platform}-{ghc_version}"
    except (ValueError, IndexError):
        package_environment = Path()
    if package_environment.is_file():
        environment["GHC_ENVIRONMENT"] = str(package_environment)
    environment["clash_lib_datadir"] = str(checkout / "clash-lib")
    return environment


def generate_verilog(
    clash_source: str,
    module_name: str,
    output_directory: Path,
    clash_executable: str | None = None,
    source_map: GeneratedSourceMap | None = None,
    companions: tuple[CompanionArtifact, ...] = (),
    public_wrapper: ClashPublicTopWrapper | None = None,
) -> tuple[Path, ...]:
    """Compile emitted Clash source and retain only this run's Verilog files.

    Clash writes into a fresh staging directory.  Publishing from that exact
    output set prevents a pre-existing ``*.v`` below ``output_directory`` from
    being mistaken for a product of the current invocation, while preserving
    unrelated files in the caller-owned directory.
    """

    executable = clash_executable or find_clash_executable()
    if executable is None:
        raise ToolchainError(
            "Clash executable was not found; set ZLANG_CLASH or ZLANG_CLASH_ROOT"
        )
    # Keep the caller's path spelling intact.  The shared publisher opens every
    # component with ``O_NOFOLLOW`` and creates missing directories relative to
    # an already-open parent; resolving or pre-creating the root here would
    # silently follow a symlink before those checks can run.
    output_directory = Path(output_directory)
    try:
        publish_companion_bundle(companions, output_directory)
    except CompanionArtifactError as error:
        raise ToolchainError(str(error)) from error
    generated_payloads: tuple[tuple[Path, bytes], ...] = ()
    with (
        tempfile.TemporaryDirectory(prefix="zlang-clash-") as temporary,
        tempfile.TemporaryDirectory(prefix="zlang-clash-output-") as staged,
    ):
        workspace = Path(temporary)
        staged_output = Path(staged).resolve()
        source_path = workspace / f"{module_name}.hs"
        source_path.write_text(clash_source)
        try:
            publish_companion_bundle(companions, workspace)
        except CompanionArtifactError as error:
            raise ToolchainError(str(error)) from error
        command = [
            executable,
            "--verilog",
            str(source_path),
            "-outputdir",
            str(staged_output),
        ]
        if public_wrapper is not None:
            if public_wrapper.module_name != module_name:
                raise ToolchainError(
                    "Clash public wrapper module does not match the compiled top"
                )
            # Clash owns the packed core name.  Its explicit component-prefix
            # option avoids rewriting or parsing generated RTL merely to make
            # the selected public module name available to the wrapper.
            command.extend((
                "-fclash-component-prefix",
                public_wrapper.core_prefix,
            ))
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            env=clash_subprocess_environment(executable),
            cwd=workspace,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            detail = attribute_generated_diagnostic(
                detail, source_map, clash_source
            )
            raise ToolchainError(f"Clash Verilog generation failed: {detail}")
        staged_files = tuple(sorted(staged_output.rglob("*.v")))
        if not staged_files:
            raise ToolchainError("Clash completed without producing a Verilog file")
        payloads: list[tuple[Path, bytes]] = []
        for generated in staged_files:
            try:
                relative = generated.resolve(strict=True).relative_to(staged_output)
            except (FileNotFoundError, ValueError) as error:
                raise ToolchainError(
                    f"Clash generated Verilog outside its staging directory: {generated}"
                ) from error
            if not generated.is_file():
                raise ToolchainError(
                    f"Clash generated Verilog is not a regular file: {relative}"
                )
            payloads.append((relative, generated.read_bytes()))
        if public_wrapper is not None:
            expected_core = f"{public_wrapper.core_module_name}.v"
            if not any(relative.name == expected_core for relative, _ in payloads):
                raise ToolchainError(
                    "Clash completed without the typed public wrapper's expected "
                    f"core module '{public_wrapper.core_module_name}'"
                )
            wrapper_path = Path(public_wrapper.logical_path)
            if wrapper_path.is_absolute() or ".." in wrapper_path.parts:
                raise ToolchainError(
                    "Clash public wrapper has an unsafe publication path"
                )
            if any(relative == wrapper_path for relative, _ in payloads):
                raise ToolchainError(
                    f"Clash public wrapper collides with generated RTL '{wrapper_path}'"
                )
            payloads.append((wrapper_path, public_wrapper.text.encode()))
            payloads.sort(key=lambda item: item[0].as_posix())
        generated_payloads = tuple(payloads)

    try:
        published_verilog = publish_relative_files(
            output_directory,
            generated_payloads,
            existing="replace",
        )
        # Clash emits relative ``$readmemb`` paths.  A consumer commonly runs
        # Verilator or synthesis from the generated module directory rather
        # than the output root, so every distinct RTL directory must receive
        # the same content-addressed companion bundle.  This is still one
        # semantic companion identity; the additional copies are physical
        # publication locations, never inferred by rewriting generated RTL.
        for parent in sorted(
            {path.parent for path in published_verilog},
            key=lambda path: path.as_posix(),
        ):
            publish_companion_bundle(companions, parent)
        return published_verilog
    except (CompanionArtifactError, SafePublicationError) as error:
        raise ToolchainError(
            f"cannot safely publish Clash Verilog: {error}"
        ) from error


def lint_with_verilator(
    verilog_files: tuple[Path, ...],
    top_module: str,
    verilator_executable: str | None = None,
    source_map: GeneratedSourceMap | None = None,
) -> None:
    """Run Verilator's synthesizable RTL lint over generated Verilog."""

    executable = verilator_executable or shutil.which("verilator")
    if executable is None:
        raise ToolchainError("Verilator executable was not found")
    completed = subprocess.run(
        [
            executable,
            "--lint-only",
            "--top-module",
            top_module,
            *(str(path) for path in verilog_files),
        ],
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        generated_text = None
        if source_map is not None:
            for path in verilog_files:
                try:
                    candidate = path.read_text()
                except OSError:
                    continue
                if hashlib.sha256(candidate.encode()).hexdigest() == source_map.artifact_hash:
                    generated_text = candidate
                    break
        detail = attribute_generated_diagnostic(
            detail, source_map, generated_text
        )
        raise ToolchainError(f"Verilator lint failed: {detail}")
