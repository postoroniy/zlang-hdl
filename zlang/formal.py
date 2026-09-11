"""M35 formal-flow orchestration.

Property generation is separate from execution. The runner returns ``skipped``
when no configured proof wrapper is available and never treats BMC as proof.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import contextmanager
from contextvars import ContextVar
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from zlang.common.tool_inventory import ToolInventory, discover_tool_inventory
from zlang.common.subprocess import subprocess_text
from zlang.formal_trace import TraceBinding, decode_vcd_trace
from zlang.ir.comparison_window import ComparisonWindow
from zlang.ir.formal import (
    CoverResult,
    CoverStatus,
    CoverWitness,
    FormalDesign,
    FormalError,
    FormalResult,
    FormalStatus,
    ProofMode,
    connect_formal_design as _connect_formal_design,
    cover_harness_top,
    emit_cover_harness,
    emit_harness,
    generate_properties,
    mark_formal_domain_applicability,
)
from zlang.ir.module import Module
from zlang.ir.recursive_formal import (
    RecursiveFormalDesign,
    RecursiveFormalResult,
    build_recursive_formal_design as _build_recursive_formal_design,
)

if TYPE_CHECKING:
    from zlang.toolchain import GeneratedDiagnosticContext


_FORMAL_VERSION_COMMANDS = (
    ("yosys", ("yosys", "-V")),
    ("sby", ("sby", "--version")),
    # Some Yosys releases expose only the smtbmc usage banner for this probe;
    # retaining that deterministic first line still versions the executable
    # surface independently from the Yosys front end.
    ("yosys-smtbmc", ("yosys-smtbmc", "--version")),
    ("z3", ("z3", "-version")),
    ("boolector", ("boolector", "--version")),
    ("cvc5", ("cvc5", "--version")),
)


def build_formal_design(module: Module) -> FormalDesign:
    return mark_formal_domain_applicability(generate_properties(module), module)


def connect_formal_design(design: FormalDesign, artifact: object) -> FormalDesign:
    """Connect only reset contracts covered by the frozen M35 harness model."""

    if design.non_executable_reason is not None:
        # Preserve property generation as a useful report, but deliberately do
        # not attach implementation text or an artifact hash.  ``emit_sby``
        # consequently remains fail-closed and cannot execute cycle-synchronous
        # assumptions against a non-default physical reset contract.
        return design
    return _connect_formal_design(design, artifact)


def build_recursive_formal_design(module: Module, *, selected_ir_identity: str | None = None) -> RecursiveFormalDesign:
    """Build recursive M35 properties without consulting backend signal names."""
    return _build_recursive_formal_design(module, selected_ir_identity=selected_ir_identity)


def emit_recursive_harness(design: RecursiveFormalDesign, *, mode: ProofMode = ProofMode.BMC,
                           depth: int = 20) -> str:
    """Emit the deterministic whole-top observation harness skeleton.

    Physical observation connections are supplied by the backend-published v4
    artifact.  The harness therefore declares one explicit observation port per
    semantic binding and never reconstructs an RTL path from an instance name.
    """
    if depth < 1:
        raise FormalError("formal depth must be positive")
    module_name = f"{design.root_instance_identity}__recursive_m35_formal"
    lines = ["`default_nettype none", f"module {module_name}(input wire clock, input wire reset,"]
    observations = sorted(design.bindings, key=lambda item: item.semantic_binding_id)
    ports = [f"  input wire [{item.width - 1}:0] zlang_formal_obs_{index}"
             for index, item in enumerate(observations)]
    lines.append(",\n".join(ports) + ");")
    lines.append(f"  // schema={design.schema_version} mode={mode.value} depth={depth}")
    for index, item in enumerate(observations):
        lines.append(f"  // observation {item.semantic_binding_id} = zlang_formal_obs_{index}")
    for item in sorted(design.properties, key=lambda value: value.concrete_property_id):
        statement = "assume" if item.property.kind.value == "assumption" else "assert"
        lines.append(f"  // {statement} {item.concrete_property_id} owned_by={item.ownership}")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def run_recursive_formal(design: RecursiveFormalDesign, *, mode: ProofMode = ProofMode.BMC,
                         depth: int = 20, reason: str | None = None,
                         artifact=None) -> tuple[RecursiveFormalResult, ...]:
    """Return explicit skips until a backend supplies connected observations.

    The semantic recursive layer must not claim a proof from an unconnected
    observation input.  A backend adapter can replace this conservative result
    path once its formal artifact publishes actual observation ports.
    """
    versions = tool_versions()
    if artifact is not None:
        unavailable = tuple(
            item.semantic_binding_id for item in design.bindings
            if not any(
                observation.semantic_binding_id == item.semantic_binding_id
                and observation.observation_token is not None
                for observation in getattr(artifact, "formal_observations", ())
            )
        )
        if unavailable and reason is None:
            reason = "formal observations unavailable: " + ", ".join(unavailable[:4])
    why = reason or "formal observation artifact is not connected to generated RTL"
    return tuple(
        RecursiveFormalResult(
            item.concrete_property_id, item.source_property_id,
            FormalStatus.SKIPPED, mode, "sby", "z3", depth,
            item.defining_module, item.specialization_identity,
            item.instance_identity, item.physical_instance_path,
            item.property.source_origin, reason=why,
        )
        for item in design.properties
    )


def _version_commands_for(names: tuple[str, ...]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    requested = set(names)
    return tuple(
        (name, command)
        for name, command in _FORMAL_VERSION_COMMANDS
        if name in requested
    )


def tool_versions(
    requested: tuple[str, ...] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return one deterministic version snapshot for the requested tools.

    With no argument this preserves the historical public discovery surface.
    Compiler-owned execution passes its exact engine/solver closure so an
    unrelated installed solver cannot perturb a proof identity.
    """

    names = requested or tuple(name for name, _ in _FORMAL_VERSION_COMMANDS)
    return discover_tool_inventory(
        names,
        version_commands=_version_commands_for(names),
        which=shutil.which,
        runner=subprocess.run,
    ).versions


@dataclass(frozen=True)
class FormalToolchainContext:
    """One immutable discovery snapshot shared by every job in a run."""

    engine: str
    solver: str
    inventory: ToolInventory

    def __post_init__(self) -> None:
        if not self.engine or not self.solver:
            raise FormalError("formal engine and solver must be non-empty tokens")

    @property
    def versions(self) -> tuple[tuple[str, str], ...]:
        return self.inventory.versions

    @property
    def missing(self) -> tuple[str, ...]:
        return self.inventory.missing

    @classmethod
    def discover(cls, *, engine: str = "sby", solver: str = "z3") -> "FormalToolchainContext":
        requested = (
            ("yosys", "sby", "yosys-smtbmc", solver)
            if engine == "sby"
            else (engine, solver)
        )
        names = tuple(dict.fromkeys(requested))
        inventory = discover_tool_inventory(
            names,
            version_commands=_version_commands_for(names),
            which=shutil.which,
            runner=subprocess.run,
        )
        return cls(engine, solver, inventory)


_FORMAL_TOOLCHAIN_OVERRIDE: ContextVar[FormalToolchainContext | None] = (
    ContextVar("zlang_formal_toolchain_override", default=None)
)


@contextmanager
def use_formal_toolchain(context: FormalToolchainContext):
    """Reuse one compiler-owned discovery snapshot through legacy runners.

    M36 deliberately retains its existing public API. This scoped
    adapter lets compiler orchestration call those APIs without causing their
    eventual :func:`run_verilog_formal` invocation to rediscover tools.
    """

    if not isinstance(context, FormalToolchainContext):
        raise TypeError("formal toolchain override requires a toolchain context")
    token = _FORMAL_TOOLCHAIN_OVERRIDE.set(context)
    try:
        yield context
    finally:
        _FORMAL_TOOLCHAIN_OVERRIDE.reset(token)


def run_formal(design: FormalDesign, *, mode: ProofMode = ProofMode.BMC,
               depth: int = 20, engine: str = "sby", solver: str | None = None,
               executable: str | None = None) -> tuple[FormalResult, ...]:
    """Execute only through an explicitly configured result adapter.

    M35 deliberately refuses to infer solver semantics from arbitrary command
    output. A future backend can provide a wrapper that creates ``FormalResult``
    records while preserving property IDs and source origins.
    """
    versions = tool_versions()
    reason = (
        "formal execution unavailable: no runner executable configured"
        if executable is None else f"formal runner not found: {executable}"
    )
    if executable is None and solver and shutil.which("sby") and shutil.which(solver):
        # The design-level API remains conservative: callers must provide an
        # implementation-bound harness for meaningful target proofs. This
        # branch is intentionally not used for unbound semantic placeholders.
        reason = "selected IR has no backend-bound formal harness"
    if executable is None or shutil.which(executable) is None:
        return tuple(FormalResult(p.id, FormalStatus.SKIPPED, mode, engine, solver, depth,
                                  source_origin=p.source_origin, tool_versions=versions,
                                  reason=reason) for p in design.properties)
    raise FormalError("custom formal execution requires an M35 result adapter")


def _publish_formal_auxiliary_files(
    root: Path,
    files: Mapping[str, str | bytes],
) -> tuple[str, ...]:
    """Publish immutable flat auxiliary inputs for one SBY workspace.

    SBY copies every entry from its ``[files]`` section into the generated
    ``src`` directory.  ROM images must therefore be declared there rather
    than merely existing next to the outer configuration file.  Keep this
    surface intentionally flat: compiler-owned companion names are already
    content-addressed and no user path is interpreted here.
    """

    names: list[str] = []
    for name in sorted(files):
        path = Path(name)
        if (
            not name
            or path.is_absolute()
            or len(path.parts) != 1
            or path.name != name
            or name in {".", ".."}
        ):
            raise FormalError(
                f"formal auxiliary file '{name}' must be one relative filename"
            )
        content = files[name]
        destination = root / name
        if isinstance(content, bytes):
            destination.write_bytes(content)
        elif isinstance(content, str):
            destination.write_text(content)
        else:
            raise FormalError(
                f"formal auxiliary file '{name}' has unsupported contents"
            )
        names.append(name)
    return tuple(names)


@dataclass(frozen=True)
class _SbyStatus:
    state: str
    return_code: int
    engine_code: int


def _read_sby_status(root: Path, top: str) -> tuple[_SbyStatus | None, str | None]:
    """Read SymbiYosys' authoritative status artifact.

    Human-readable stdout is deliberately not the result authority: error
    logs may contain words such as ``pass`` or ``failed`` while SBY itself
    records ``ERROR``.  A missing or malformed status is therefore an
    inconclusive tool result, never an inferred pass or counterexample.
    """

    path = root / top / "status"
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as error:
        return None, f"SymbiYosys status artifact is unavailable: {error}"
    match = re.fullmatch(r"([A-Z]+)\s+([0-9]+)\s+([0-9]+)", text)
    if match is None:
        return None, "SymbiYosys status artifact is malformed"
    state = match.group(1)
    if state not in {"PASS", "FAIL", "ERROR", "UNKNOWN", "TIMEOUT"}:
        return None, f"SymbiYosys status artifact has unknown state '{state}'"
    return _SbyStatus(state, int(match.group(2)), int(match.group(3))), None


def _sby_trace_files(root: Path, top: str) -> tuple[Path, ...]:
    work = root / top
    if not work.is_dir():
        return ()
    return tuple(sorted(work.glob("**/trace*.vcd")))


def _execution_error_reason(
    reason: str | None,
    output: str,
    diagnostic_sources: tuple[GeneratedDiagnosticContext, ...],
) -> str | None:
    """Attach a mapped tool diagnostic without making log prose authoritative."""

    if not output or not diagnostic_sources:
        return reason
    # Import lazily to keep the external runner separate from source-map
    # attribution is an execution-error convenience and must not create a
    # module-initialization dependency in the formal core.
    from zlang.toolchain import attribute_combined_generated_diagnostic

    attributed = attribute_combined_generated_diagnostic(
        output,
        diagnostic_sources,
    )
    if attributed == output:
        return reason
    # The immutable work directory retains the complete raw streams.  Keep
    # structured result metadata bounded while retaining the mapped error tail.
    diagnostic = attributed[-2000:]
    return diagnostic if not reason else f"{reason}\n{diagnostic}"


def run_verilog_formal(source: str, *, top: str, property_id: str,
                       mode: ProofMode = ProofMode.BMC, depth: int = 20,
                       solver: str = "z3", engine: str = "sby",
                       source_origin=None, systemverilog: bool = False,
                       timeout_seconds: int = 120,
                       work_directory: Path | None = None,
                       auxiliary_files: Mapping[str, str | bytes] | None = None,
                       toolchain: FormalToolchainContext | None = None,
                       trace_bindings: tuple[TraceBinding, ...] = (),
                       comparison_window: ComparisonWindow | None = None,
                       diagnostic_sources: tuple[GeneratedDiagnosticContext, ...] = (),
                       ) -> FormalResult:
    """Run a concrete backend-bound Verilog harness through SymbiYosys.

    This is deliberately separate from semantic property generation. It is used
    by M35/M36 integration tests and by backend adapters that have published a
    complete binding map. Missing tools return ``skipped``.
    """
    context = toolchain or _FORMAL_TOOLCHAIN_OVERRIDE.get()
    if context is None:
        context = FormalToolchainContext.discover(engine=engine, solver=solver)
    if context.engine != engine or context.solver != solver:
        raise FormalError("formal toolchain context does not match the requested route")
    missing = context.missing
    if missing:
        return FormalResult(property_id, FormalStatus.SKIPPED, mode, engine, solver, depth,
                            source_origin=source_origin,
                            tool_versions=context.versions,
                            reason="missing formal tools: " + ", ".join(missing))
    if depth < 1:
        raise FormalError("formal depth must be positive")
    if timeout_seconds < 1:
        raise FormalError("formal timeout must be positive")
    temporary_workspace = None
    if work_directory is None:
        temporary_workspace = tempfile.TemporaryDirectory(prefix="zlang-formal-")
        root = Path(temporary_workspace.name)
    else:
        root = Path(work_directory)
        root.mkdir(parents=True, exist_ok=True)
    try:
        verilog = root / f"{top}.v"
        config = root / f"{top}.sby"
        verilog.write_text(source)
        auxiliary_names = _publish_formal_auxiliary_files(
            root, auxiliary_files or {}
        )
        mode_name = "bmc" if mode is ProofMode.BMC else "prove"
        config.write_text("\n".join((
            "[options]", f"mode {mode_name}", f"depth {depth}", "", "[engines]",
            f"smtbmc {solver}", "", "[script]",
            f"read_verilog {'-sv ' if systemverilog else ''}-formal {verilog.name}",
            f"prep -top {top}", "", "[files]", verilog.name,
            *auxiliary_names, "",
        )))
        try:
            completed = subprocess.run(("sby", "-f", str(config)), cwd=root,
                                       capture_output=True, text=True,
                                       timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            # Python may retain TimeoutExpired partial streams as bytes even
            # when subprocess.run was requested with text=True.  Normalize
            # each stream before concatenation so a real solver timeout is a
            # structured UNKNOWN result, never a secondary TypeError.
            partial = subprocess_text(error.stdout) + subprocess_text(error.stderr)
            if work_directory is not None:
                (root / "solver.timeout.log").write_text(partial)
            return FormalResult(
                property_id, FormalStatus.UNKNOWN, mode, engine, solver, depth,
                source_origin=source_origin, tool_versions=context.versions,
                reason=_execution_error_reason((
                    "formal execution timed out after "
                    f"{timeout_seconds} seconds"
                )
                + (f": {partial[-500:]}" if partial else ""),
                    partial,
                    diagnostic_sources,
                ),
            )
        output = (completed.stdout or "") + (completed.stderr or "")
        if work_directory is not None:
            (root / "solver.stdout.log").write_text(completed.stdout or "")
            (root / "solver.stderr.log").write_text(completed.stderr or "")
        status, status_error = _read_sby_status(root, top)
        if status is None:
            return FormalResult(
                property_id, FormalStatus.UNKNOWN, mode, engine, solver, depth,
                source_origin=source_origin, tool_versions=context.versions,
                reason=_execution_error_reason(
                    status_error, output, diagnostic_sources
                ),
            )
        if status.state == "FAIL":
            traces = _sby_trace_files(root, top)
            if not traces:
                return FormalResult(
                    property_id, FormalStatus.UNKNOWN, mode, engine, solver,
                    depth, source_origin=source_origin,
                    tool_versions=context.versions,
                    reason=_execution_error_reason((
                        "SymbiYosys reported FAIL without a counterexample trace"
                    ), output, diagnostic_sources),
                )
            from zlang.ir.formal import Counterexample
            # The VCD ``smt_step`` signal is the authoritative failure frame.
            # Solver log prose is intentionally not part of result semantics.
            snapshot = decode_vcd_trace(
                traces[-1],
                cycle=None,
                bindings=trace_bindings,
                comparison_window=comparison_window,
            )
            return FormalResult(property_id, FormalStatus.FAILED, mode, engine, solver, depth,
                                counterexample=Counterexample(
                                    property_id,
                                    cycle=snapshot.failure_cycle,
                                    values=snapshot.values,
                                    raw_trace=output[-4000:] or "formal counterexample"),
                                source_origin=source_origin, tool_versions=context.versions,
                                reason="formal counterexample reported")
        if status.state != "PASS" or completed.returncode != 0:
            return FormalResult(
                property_id, FormalStatus.UNKNOWN, mode, engine, solver, depth,
                source_origin=source_origin, tool_versions=context.versions,
                reason=_execution_error_reason((
                    f"formal engine produced {status.state} status "
                    f"(process={completed.returncode}, sby={status.return_code})"
                ), output, diagnostic_sources),
            )
        result_status = (
            FormalStatus.BOUNDED_PASS
            if mode is ProofMode.BMC else FormalStatus.PROVEN
        )
        return FormalResult(
            property_id, result_status, mode, engine, solver, depth,
            source_origin=source_origin, tool_versions=context.versions,
        )
    finally:
        if temporary_workspace is not None:
            temporary_workspace.cleanup()


def run_verilog_targets(targets: tuple[tuple[str, str, str], ...], *,
                        mode: ProofMode = ProofMode.BMC, depth: int = 20,
                        solver: str = "z3") -> tuple[FormalResult, ...]:
    """Run a set of backend-published ``(property_id, top, source)`` targets."""
    return tuple(run_verilog_formal(source, top=top, property_id=property_id,
                                    mode=mode, depth=depth, solver=solver)
                 for property_id, top, source in targets)


def run_verilog_cover(
    source: str,
    *,
    top: str,
    property_id: str,
    depth: int = 20,
    solver: str = "z3",
    engine: str = "sby",
    source_origin=None,
    systemverilog: bool = False,
    timeout_seconds: int = 120,
    work_directory: Path | None = None,
    auxiliary_files: Mapping[str, str | bytes] | None = None,
    toolchain: FormalToolchainContext | None = None,
    diagnostic_sources: tuple[GeneratedDiagnosticContext, ...] = (),
) -> CoverResult:
    """Execute one backend-bound bounded reachability query through SBY.

    SBY cover mode reports an unreached bounded goal as ``FAIL``.  That result
    is intentionally translated to ``bounded_unreached`` rather than the
    safety vocabulary's ``failed`` or ``proven``.
    """

    context = toolchain or _FORMAL_TOOLCHAIN_OVERRIDE.get()
    if context is None:
        context = FormalToolchainContext.discover(engine=engine, solver=solver)
    if context.engine != engine or context.solver != solver:
        raise FormalError("formal toolchain context does not match the requested route")
    missing = context.missing
    if missing:
        return CoverResult(
            property_id,
            CoverStatus.SKIPPED,
            engine,
            solver,
            depth,
            source_origin=source_origin,
            tool_versions=context.versions,
            reason="missing formal tools: " + ", ".join(missing),
        )
    if depth < 1:
        raise FormalError("formal depth must be positive")
    if timeout_seconds < 1:
        raise FormalError("formal timeout must be positive")
    temporary_workspace = None
    if work_directory is None:
        temporary_workspace = tempfile.TemporaryDirectory(prefix="zlang-cover-")
        root = Path(temporary_workspace.name)
    else:
        root = Path(work_directory)
        root.mkdir(parents=True, exist_ok=True)
    try:
        verilog = root / f"{top}.v"
        config = root / f"{top}.sby"
        verilog.write_text(source)
        auxiliary_names = _publish_formal_auxiliary_files(
            root, auxiliary_files or {}
        )
        config.write_text("\n".join((
            "[options]", "mode cover", f"depth {depth}", "", "[engines]",
            f"smtbmc {solver}", "", "[script]",
            f"read_verilog {'-sv ' if systemverilog else ''}-formal {verilog.name}",
            f"prep -top {top}", "", "[files]", verilog.name,
            *auxiliary_names, "",
        )))
        try:
            completed = subprocess.run(
                ("sby", "-f", str(config)),
                cwd=root,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            partial = subprocess_text(error.stdout) + subprocess_text(error.stderr)
            if work_directory is not None:
                (root / "solver.timeout.log").write_text(partial)
            return CoverResult(
                property_id,
                CoverStatus.UNKNOWN,
                engine,
                solver,
                depth,
                source_origin=source_origin,
                tool_versions=context.versions,
                reason=_execution_error_reason((
                    f"cover execution timed out after {timeout_seconds} seconds"
                    + (f": {partial[-500:]}" if partial else "")
                ), partial, diagnostic_sources),
            )
        output = (completed.stdout or "") + (completed.stderr or "")
        if work_directory is not None:
            (root / "solver.stdout.log").write_text(completed.stdout or "")
            (root / "solver.stderr.log").write_text(completed.stderr or "")
        status, status_error = _read_sby_status(root, top)
        if status is None:
            return CoverResult(
                property_id, CoverStatus.UNKNOWN, engine, solver, depth,
                source_origin=source_origin, tool_versions=context.versions,
                reason=_execution_error_reason(
                    status_error, output, diagnostic_sources
                ),
            )
        traces = _sby_trace_files(root, top)
        if (
            completed.returncode == 0
            and status.state == "PASS"
            and traces
        ):
            # A PASS status establishes reachability; the witness cycle comes
            # from the trace's authoritative ``smt_step`` signal, never from
            # human-readable solver output.
            snapshot = decode_vcd_trace(
                traces[-1], cycle=None, bindings=(),
            )
            if snapshot.failure_cycle is None:
                return CoverResult(
                    property_id, CoverStatus.UNKNOWN, engine, solver, depth,
                    source_origin=source_origin,
                    tool_versions=context.versions,
                    reason=(
                        "SymbiYosys reported PASS with a malformed cover "
                        "witness trace"
                    ),
                )
            # yosys-smtbmc cover traces terminate with one extra transition
            # frame.  The witnessed sample is therefore the preceding
            # ``smt_step`` (including step zero for an initially true cover).
            witness_cycle = max(0, snapshot.failure_cycle - 1)
            return CoverResult(
                property_id,
                CoverStatus.WITNESSED,
                engine,
                solver,
                depth,
                witness=CoverWitness(
                    property_id,
                    witness_cycle,
                    raw_trace=output[-4000:] or "formal cover witness",
                ),
                source_origin=source_origin,
                tool_versions=context.versions,
            )
        if status.state == "PASS":
            return CoverResult(
                property_id, CoverStatus.UNKNOWN, engine, solver, depth,
                source_origin=source_origin, tool_versions=context.versions,
                reason="SymbiYosys reported PASS without a cover witness trace",
            )
        if status.state == "FAIL":
            return CoverResult(
                property_id,
                CoverStatus.BOUNDED_UNREACHED,
                engine,
                solver,
                depth,
                source_origin=source_origin,
                tool_versions=context.versions,
                reason=f"cover was not reached within depth {depth}",
            )
        return CoverResult(
            property_id,
            CoverStatus.UNKNOWN,
            engine,
            solver,
            depth,
            source_origin=source_origin,
            tool_versions=context.versions,
            reason=_execution_error_reason((
                f"cover engine produced {status.state} status "
                f"(process={completed.returncode}, sby={status.return_code})"
            ), output, diagnostic_sources),
        )
    finally:
        if temporary_workspace is not None:
            temporary_workspace.cleanup()


def emit_sby(
    design: FormalDesign,
    *,
    depth: int = 20,
    top: str | None = None,
    mode: ProofMode = ProofMode.BMC,
    solver: str = "z3",
    source_file: str | None = None,
) -> str:
    """Emit SBY only for an implementation-bound executable harness."""
    if depth < 1:
        raise FormalError("formal depth must be positive")
    if design.connected_artifact_hash is None:
        raise FormalError(
            "executable SBY output requires a connected backend formal artifact"
        )
    unavailable = next((
        item for item in design.properties
        if item.non_executable_reason is not None or item.predicate is None
    ), None)
    if unavailable is not None:
        why = (
            unavailable.non_executable_reason
            or "structured predicate unavailable"
        )
        raise FormalError(
            "combined executable SBY view requires every safety property and "
            f"assumption on one backend; '{unavailable.id}' is unavailable: "
            f"{why}. Use --verification-bundle for per-goal backend routing"
        )
    if not solver or any(character.isspace() for character in solver):
        raise FormalError("formal solver name must be one non-empty token")
    top = top or f"{design.module_name}__m35_formal"
    source_file = source_file or f"{top}.sv"
    if not source_file or any(character in source_file for character in "\n\r"):
        raise FormalError("formal harness filename must be one non-empty line")
    return "\n".join((
        "[options]", f"mode {mode.value}", f"depth {depth}", "", "[engines]",
        f"smtbmc {solver}", "", "[script]",
        f"read_verilog -sv -formal {source_file}", f"prep -top {top}", "", "[files]",
        source_file, "",
    ))


def emit_cover_sby(
    design: FormalDesign,
    *,
    cover_id: str,
    depth: int = 20,
    top: str | None = None,
    solver: str = "z3",
    source_file: str | None = None,
) -> str:
    """Emit a deterministic per-goal SBY cover configuration."""

    if depth < 1:
        raise FormalError("formal depth must be positive")
    if design.connected_artifact_hash is None:
        raise FormalError(
            "executable cover SBY output requires a connected backend formal artifact"
        )
    matches = tuple(item for item in design.covers if item.id == cover_id)
    if len(matches) != 1:
        if not matches:
            raise FormalError(f"unknown cover property: {cover_id}")
        raise FormalError(f"duplicate cover property id: {cover_id}")
    if matches[0].non_executable_reason is not None:
        raise FormalError(
            f"cover property '{cover_id}' is not executable: "
            f"{matches[0].non_executable_reason}"
        )
    unavailable_assumption = next((
        item for item in design.properties
        if item.kind.value == "assumption"
        and (item.non_executable_reason is not None or item.predicate is None)
    ), None)
    if unavailable_assumption is not None:
        why = (
            unavailable_assumption.non_executable_reason
            or "structured predicate unavailable"
        )
        raise FormalError(
            f"cover property '{cover_id}' requires executable assumption "
            f"'{unavailable_assumption.id}': {why}"
        )
    if not solver or any(character.isspace() for character in solver):
        raise FormalError("formal solver name must be one non-empty token")
    top = top or cover_harness_top(design, cover_id)
    source_file = source_file or f"{top}.sv"
    if not source_file or any(character in source_file for character in "\n\r"):
        raise FormalError("formal harness filename must be one non-empty line")
    return "\n".join((
        "[options]", "mode cover", f"depth {depth}", "", "[engines]",
        f"smtbmc {solver}", "", "[script]",
        f"read_verilog -sv -formal {source_file}", f"prep -top {top}", "",
        "[files]", source_file, "",
    ))


__all__ = ["FormalToolchainContext", "build_formal_design", "build_recursive_formal_design", "connect_formal_design",
           "emit_cover_harness", "emit_cover_sby", "emit_harness",
           "emit_recursive_harness", "emit_sby", "run_formal", "run_recursive_formal",
           "run_verilog_cover", "run_verilog_formal", "run_verilog_targets", "tool_versions",
           "use_formal_toolchain"]
