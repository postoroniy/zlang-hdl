"""Untrusted, bounded on-disk cache of exact validated simulation plans.

The compiler must still construct its current planned typed module.  This
cache skips only primitive plan lowering across processes; it does not restore
semantic IR, publish evidence, or cache executable machine code.
"""

from __future__ import annotations

from functools import cache
import hashlib
import json
import os
from pathlib import Path
import tempfile

from zlang._version import __version__
from zlang.compilation_session import CompilationSession
from zlang.opt import OptimizationStage, canonical_ir_identity, lower
from zlang.simulation_plan import (
    MAX_PLAN_BYTES,
    SIMULATION_PLAN_SCHEMA,
    SIMULATION_RUNTIME_ABI,
    SimulationPlan,
    SimulationPlanError,
)


_CACHE_SCHEMA = "zlang-persistent-simulation-plan-v1"
_MAX_ENTRIES = 128
_MAX_BYTES = 256 * 1024 * 1024


@cache
def _compiler_code_identity() -> str:
    """Invalidate even between local dirty-tree runs with the same version."""

    root = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _cache_root() -> Path:
    selected = os.environ.get("XDG_CACHE_HOME")
    if selected and Path(selected).expanduser().is_absolute():
        return Path(selected).expanduser() / "zlang-hdl" / "simulation-plan-v1"
    return Path.home() / ".cache" / "zlang-hdl" / "simulation-plan-v1"


def _recipe(session: CompilationSession) -> str:
    module = session.planning.module
    inputs = session.physical_inputs
    overlays = dict(inputs.editor_source_overlays)
    sources = []
    for path in inputs.all_paths:
        digest = overlays.get(path)
        if digest is None:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        sources.append(digest)
    payload = {
        "schema": _CACHE_SCHEMA,
        "compiler": __version__,
        "code": _compiler_code_identity(),
        "plan": SIMULATION_PLAN_SCHEMA,
        "abi": SIMULATION_RUNTIME_ABI,
        "planned": canonical_ir_identity(
            lower(module, stage=OptimizationStage.SELECTED_ARCHITECTURE)
        ),
        "source": hashlib.sha256(session.source.encode("utf-8")).hexdigest(),
        "source_unit": session.source_unit,
        "physical_root": (
            None
            if inputs.root_source is None
            else hashlib.sha256(
                inputs.root_source.as_posix().encode("utf-8")
            ).hexdigest()
        ),
        "inputs": sources,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _clean_old_entries(directory: Path) -> None:
    try:
        files = sorted(
            (path for path in directory.glob("*.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        size = 0
        for index, path in enumerate(files):
            size += path.stat().st_size
            if index >= _MAX_ENTRIES or size > _MAX_BYTES:
                path.unlink(missing_ok=True)
    except OSError:
        pass


def _decode_cached_plan(payload: bytes, recipe: str) -> SimulationPlan:
    if len(payload) > MAX_PLAN_BYTES + 512:
        raise SimulationPlanError("cached simulation plan exceeds its bound")
    try:
        record = json.loads(payload)
        if (
            not isinstance(record, dict)
            or set(record) != {"schema", "recipe", "plan"}
            or record["schema"] != _CACHE_SCHEMA
            or record["recipe"] != recipe
        ):
            raise SimulationPlanError("simulation cache recipe mismatch")
        encoded = json.dumps(
            record["plan"], sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise SimulationPlanError("malformed simulation cache record") from error
    return SimulationPlan.from_bytes(encoded)


def _encode_cached_plan(plan: SimulationPlan, recipe: str) -> bytes:
    return (
        b'{"plan":'
        + plan.canonical_bytes
        + b',"recipe":"'
        + recipe.encode("ascii")
        + b'","schema":"'
        + _CACHE_SCHEMA.encode("ascii")
        + b'"}'
    )


def load_or_build(session: CompilationSession) -> SimulationPlan:
    """Return an exact plan; malformed or unavailable cache is a strict miss."""

    already = session.cached_simulation_plan()
    if already is not None:
        return already
    # A persisted plan alone does not restore its typed planned module.  Keep
    # this second-stage codec opt-in until a strict typed-product codec makes
    # cross-process hits beneficial on ordinary small designs.
    if os.environ.get("ZLANG_SIM_PLAN_CACHE", "off").lower() != "persistent":
        return session.simulation_plan
    recipe = None
    try:
        recipe = _recipe(session)
        directory = _cache_root()
        path = directory / f"{recipe}.json"
        if path.stat().st_size <= MAX_PLAN_BYTES + 512:
            plan = _decode_cached_plan(path.read_bytes(), recipe)
            if plan.payload["module"] == session.planning.module.name:
                return session.accept_simulation_plan(plan)
    except (OSError, ValueError, SimulationPlanError):
        pass
    plan = session.simulation_plan
    if recipe is None or len(plan.canonical_bytes) > MAX_PLAN_BYTES:
        return plan
    if session.source.encode("utf-8") in plan.canonical_bytes or any(
        path.as_posix().encode("utf-8") in plan.canonical_bytes
        for path in session.physical_inputs.all_paths
    ):
        # A source literal or external model path may be present in a future
        # primitive schema.  Such a plan remains executable in memory but is
        # never published to the source-free persistent cache.
        return plan
    temporary = None
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=directory,
            prefix=".plan-",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(_encode_cached_plan(plan, recipe))
        os.replace(temporary, directory / f"{recipe}.json")
        _clean_old_entries(directory)
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return plan
