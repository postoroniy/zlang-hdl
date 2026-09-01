"""Bounded whole-root M36/M38 for one pure scalar child hierarchy.

This module deliberately reuses the existing same-cycle value equivalence
relations.  The semantic reference is materialized from typed hierarchy and
instance bindings, while implementation RTL is flattened and namespaced by
Yosys as a *formal-only* artifact.  Production RTL is neither rewritten nor
published under the formal namespace.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Mapping

from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.manifest import BackendArtifact, publish_artifact
from zlang.backend.systemverilog import emit_artifact as emit_systemverilog_artifact
from zlang.common import stable_digest
from zlang.cross_backend import run_cross_backend_formal
from zlang.equivalence import (
    artifact_hash,
    emit_miter_with_metadata,
    emit_reference_model,
    make_equivalence_property,
    run_equivalence_formal,
)
from zlang.equivalence_result_codec import (
    cross_backend_result_from_data,
    cross_backend_result_to_data,
    equivalence_result_from_data,
    equivalence_result_to_data,
)
from zlang.formal_artifact_provider import (
    FormalArtifactNamespace,
    FormalArtifactProvider,
    decisive_formal_cacheable,
)
from zlang.formal_candidate import PreparedCandidateEquivalence
from zlang.ir.callables import reachable_module_callables
from zlang.ir.cross_backend import (
    CrossBackendMode,
    CrossBackendProperty,
    CrossBackendRelation,
    CrossBackendResult,
)
from zlang.ir.equivalence import (
    BindingMap,
    BindingSide,
    EquivalenceMode,
    EquivalenceProperty,
    EquivalenceResult,
    EquivalenceStatus,
)
from zlang.ir.formal_planning import (
    FormalBackendArtifactRef,
    FormalExecutableRoute,
    FormalGoalPlan,
    FormalPlanGoalKind,
    FormalRouteKind,
)
from zlang.ir.hierarchical_values import (
    MaterializedHierarchicalValue,
    materialize_pure_hierarchical_output,
)
from zlang.ir.module import Assignment, Module, default_selected_ir_identity
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.toolchain import ToolchainError, find_clash_executable, generate_verilog
from zlang.triangular_evidence import M36EvidenceLeg, M38EvidenceReport


ROOT_EQUIVALENCE_PLAN_SCHEMA = 1
_DECISIVE_M36 = frozenset(
    {EquivalenceStatus.BOUNDED_PASS, EquivalenceStatus.PROVEN, EquivalenceStatus.FAILED}
)


class RootEquivalenceError(ValueError):
    """A requested whole-root equivalence route is malformed or unsupported."""


class RootEquivalenceUnavailable(RootEquivalenceError):
    """A required external backend/tool cannot prepare the bounded route."""


def _mapping(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise RootEquivalenceError(f"{description} must be a JSON object")
    return value


def _nonempty(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise RootEquivalenceError(f"{description} must be a non-empty string")
    return value


def _binding_identity(artifact: BackendArtifact) -> str:
    manifest = json.loads(artifact.to_json())
    return "bindings:" + stable_digest(
        {
            "manifest_version": artifact.manifest_version,
            "artifact_hash": artifact.artifact_hash,
            "bindings": manifest["bindings"],
        }
    )


def _artifact_ref(artifact: BackendArtifact) -> FormalBackendArtifactRef:
    return FormalBackendArtifactRef(
        artifact.backend,
        artifact.artifact_hash,
        _binding_identity(artifact),
    )


@dataclass(frozen=True)
class RootEquivalencePlan:
    """Strict replayable plan for one whole-root triangle.

    Implementation and reference source text intentionally remain outside this
    compact planning record.  Existing immutable artifact/bundle machinery can
    carry them when the route is promoted to the public CLI later.
    """

    selected_ir_identity: str
    output_semantic_id: str
    reference_identity: str
    clash_m36: FormalGoalPlan
    direct_systemverilog_m36: FormalGoalPlan
    m38: FormalGoalPlan

    def __post_init__(self) -> None:
        _nonempty(self.selected_ir_identity, "root selected-IR identity")
        _nonempty(self.output_semantic_id, "root output semantic identity")
        _nonempty(self.reference_identity, "root reference identity")
        for label, plan, kind in (
            ("Clash M36", self.clash_m36, FormalPlanGoalKind.M36_EQUIVALENCE),
            (
                "direct-SystemVerilog M36",
                self.direct_systemverilog_m36,
                FormalPlanGoalKind.M36_EQUIVALENCE,
            ),
            ("M38", self.m38, FormalPlanGoalKind.M38_EQUIVALENCE),
        ):
            if not isinstance(plan, FormalGoalPlan) or plan.kind is not kind:
                raise RootEquivalenceError(f"{label} uses an incompatible goal plan")
            if plan.selected_ir_identity != self.selected_ir_identity:
                raise RootEquivalenceError(f"{label} selected-IR identity mismatch")
            if self.output_semantic_id not in plan.required_observations:
                raise RootEquivalenceError(f"{label} omits the root output observation")
            if plan.route is None:
                raise RootEquivalenceError(f"{label} must be executable")
        if self.clash_m36.property_identity != self.direct_systemverilog_m36.property_identity:
            raise RootEquivalenceError("root M36 legs must use one canonical property")
        if self.clash_m36.route.reference_identity != self.reference_identity:
            raise RootEquivalenceError("Clash M36 reference identity mismatch")
        if self.direct_systemverilog_m36.route.reference_identity != self.reference_identity:
            raise RootEquivalenceError("direct-SystemVerilog M36 reference identity mismatch")
        expected = (
            self.clash_m36.route.artifacts[0],
            self.direct_systemverilog_m36.route.artifacts[0],
        )
        if self.m38.route.artifacts != expected:
            raise RootEquivalenceError("M38 artifacts differ from the two M36 legs")

    @property
    def identity(self) -> str:
        return "root-equivalence-plan:" + stable_digest(self.identity_data())

    def identity_data(self) -> dict[str, object]:
        return {
            "schema_version": ROOT_EQUIVALENCE_PLAN_SCHEMA,
            "selected_ir_identity": self.selected_ir_identity,
            "output_semantic_id": self.output_semantic_id,
            "reference_identity": self.reference_identity,
            "clash_m36_plan_identity": self.clash_m36.plan_identity,
            "direct_systemverilog_m36_plan_identity": (
                self.direct_systemverilog_m36.plan_identity
            ),
            "m38_plan_identity": self.m38.plan_identity,
        }

    def to_data(self) -> dict[str, object]:
        return {
            **self.identity_data(),
            "identity": self.identity,
            "clash_m36": self.clash_m36.to_data(),
            "direct_systemverilog_m36": self.direct_systemverilog_m36.to_data(),
            "m38": self.m38.to_data(),
        }

    @classmethod
    def from_data(cls, value: object) -> "RootEquivalencePlan":
        data = _mapping(value, "root equivalence plan")
        expected = {
            "schema_version",
            "selected_ir_identity",
            "output_semantic_id",
            "reference_identity",
            "clash_m36_plan_identity",
            "direct_systemverilog_m36_plan_identity",
            "m38_plan_identity",
            "identity",
            "clash_m36",
            "direct_systemverilog_m36",
            "m38",
        }
        if set(data) != expected:
            raise RootEquivalenceError("root equivalence plan fields are invalid")
        if data["schema_version"] != ROOT_EQUIVALENCE_PLAN_SCHEMA:
            raise RootEquivalenceError("root equivalence plan schema is unsupported")
        try:
            result = cls(
                _nonempty(data["selected_ir_identity"], "root selected-IR identity"),
                _nonempty(data["output_semantic_id"], "root output semantic identity"),
                _nonempty(data["reference_identity"], "root reference identity"),
                FormalGoalPlan.from_data(data["clash_m36"]),
                FormalGoalPlan.from_data(data["direct_systemverilog_m36"]),
                FormalGoalPlan.from_data(data["m38"]),
            )
        except ValueError as error:
            raise RootEquivalenceError(str(error)) from error
        for field, actual in (
            ("clash_m36_plan_identity", result.clash_m36.plan_identity),
            (
                "direct_systemverilog_m36_plan_identity",
                result.direct_systemverilog_m36.plan_identity,
            ),
            ("m38_plan_identity", result.m38.plan_identity),
            ("identity", result.identity),
        ):
            if data[field] != actual:
                raise RootEquivalenceError(f"root equivalence {field} mismatch")
        return result


@dataclass(frozen=True)
class PreparedRootEquivalence:
    materialized: MaterializedHierarchicalValue
    property: EquivalenceProperty
    m38_property: CrossBackendProperty
    reference_artifact: BackendArtifact
    clash: PreparedCandidateEquivalence
    direct_systemverilog: PreparedCandidateEquivalence
    plan: RootEquivalencePlan

    def __post_init__(self) -> None:
        if self.clash.reference_artifact_hash != self.reference_artifact.artifact_hash:
            raise RootEquivalenceError("Clash leg uses a different semantic reference")
        if self.direct_systemverilog.reference_artifact_hash != self.reference_artifact.artifact_hash:
            raise RootEquivalenceError("direct-SV leg uses a different semantic reference")
        if self.property.id != self.plan.clash_m36.property_identity:
            raise RootEquivalenceError("prepared root property differs from its plan")
        if self.m38_property.id != self.plan.m38.property_identity:
            raise RootEquivalenceError("prepared root M38 property differs from its plan")


@dataclass(frozen=True)
class RootEquivalenceExecution:
    clash_m36: EquivalenceResult
    direct_systemverilog_m36: EquivalenceResult
    m38: CrossBackendResult | None
    triangle: M38EvidenceReport


def _tool_version(executable: str, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            (executable, *arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"unavailable:{type(error).__name__}"
    output = (completed.stdout or completed.stderr).strip()
    return f"exit={completed.returncode}:{output}"


def _required_public_ids(module: Module) -> tuple[str, ...]:
    return tuple(f"port:{port.name}" for port in module.ports)


def _validate_public_artifact(artifact: BackendArtifact, module: Module) -> None:
    artifact.binding_map().validate()
    by_id = {item.semantic_signal_id: item for item in artifact.bindings}
    expected = _required_public_ids(module)
    missing = tuple(item for item in expected if item not in by_id)
    unavailable = tuple(
        item for item in expected if item in by_id and not by_id[item].physical_available
    )
    if missing or unavailable:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unavailable:
            details.append("not physically published " + ", ".join(unavailable))
        raise RootEquivalenceError(
            "root equivalence artifact bindings are incomplete: " + "; ".join(details)
        )
    for semantic_id in expected:
        binding = by_id[semantic_id]
        token = binding.rtl_path.rsplit(".", 1)[-1]
        if not token or re.search(
            rf"(?<![A-Za-z0-9_$]){re.escape(token)}(?![A-Za-z0-9_$])",
            artifact.text,
        ) is None:
            raise RootEquivalenceError(
                f"root equivalence artifact does not contain bound RTL port "
                f"'{semantic_id}' ({binding.rtl_path})"
            )


def _yosys_formal_namespace(
    artifact: BackendArtifact,
    module: Module,
    *,
    namespace: str,
    yosys_executable: str,
) -> BackendArtifact:
    """Flatten and rename one exact root through Yosys, never RTL text edits."""

    if not namespace or not namespace.replace("_", "a").isalnum():
        raise RootEquivalenceError("formal RTL namespace must be an identifier")
    with tempfile.TemporaryDirectory(prefix="zlang-root-formal-") as temporary:
        workspace = Path(temporary)
        source = workspace / "implementation.sv"
        output = workspace / "implementation_flat.sv"
        source.write_text(artifact.text)
        script = "; ".join(
            (
                f"read_verilog -sv -formal {source}",
                f"hierarchy -check -top {artifact.module}",
                "proc",
                "flatten",
                f"rename {artifact.module} {namespace}",
                f"write_verilog -sv -noattr {output}",
            )
        )
        try:
            completed = subprocess.run(
                (yosys_executable, "-q", "-p", script),
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RootEquivalenceUnavailable(
                f"formal RTL namespace preparation failed: {error}"
            ) from error
        if completed.returncode != 0 or not output.is_file():
            detail = (completed.stderr or completed.stdout).strip()
            raise RootEquivalenceUnavailable(
                "formal RTL namespace preparation failed"
                + (f": {detail}" if detail else "")
            )
        text = output.read_text()
    if f"module {namespace}" not in text and f"module \\{namespace}" not in text:
        raise RootEquivalenceError("Yosys did not publish the requested formal namespace")
    digest = hashlib.sha256(text.encode()).hexdigest()
    bindings = tuple(
        replace(
            item,
            rtl_module=namespace,
            artifact_hash=digest,
        )
        for item in artifact.bindings
    )
    result = replace(
        artifact,
        module=namespace,
        artifact_hash=digest,
        text=text,
        bindings=bindings,
        components=(),
        instances=(),
        recursive_bindings=(),
        formal_observations=(),
        formal_artifact_hash=digest,
        companions=(),
    )
    _validate_public_artifact(result, module)
    # Exercise the exact public manifest codec before the artifact reaches a
    # miter.  The text remains out-of-band by design, as for all manifests.
    BackendArtifact.from_json(result.to_json())
    return result


def _namespace_artifact(
    artifact: BackendArtifact,
    module: Module,
    *,
    backend: str,
    selected_ir_identity: str,
    yosys_executable: str,
    provider: FormalArtifactProvider,
) -> BackendArtifact:
    namespace = "ZLangRootEq_" + stable_digest(
        {
            "schema": "zlang-root-formal-namespace-v1",
            "backend": backend,
            "selected_ir_identity": selected_ir_identity,
            "artifact": artifact.artifact_hash,
        }
    )[:16]
    return provider.get_or_prepare(
        FormalArtifactNamespace.PREPARED,
        "root-formal-namespace-v1",
        {
            "backend": backend,
            "selected_ir_identity": selected_ir_identity,
            "source_build_identity": artifact.build_identity,
            "namespace": namespace,
            "yosys": str(Path(yosys_executable).resolve()),
            "yosys_version": _tool_version(yosys_executable, "-V"),
        },
        lambda: _yosys_formal_namespace(
            artifact,
            module,
            namespace=namespace,
            yosys_executable=yosys_executable,
        ),
    )


def _clash_rtl_artifact(
    module: Module,
    *,
    selected_ir_identity: str,
    clash_executable: str,
    provider: FormalArtifactProvider,
) -> BackendArtifact:
    source_artifact = emit_clash_artifact(
        module, selected_ir_identity=selected_ir_identity
    )

    def prepare() -> BackendArtifact:
        try:
            with tempfile.TemporaryDirectory(prefix="zlang-root-clash-") as temporary:
                paths = generate_verilog(
                    source_artifact.text,
                    module.name,
                    Path(temporary) / "rtl",
                    clash_executable,
                    companions=source_artifact.companions,
                )
                text = "\n".join(path.read_text() for path in sorted(paths))
        except (OSError, ToolchainError, ValueError) as error:
            raise RootEquivalenceUnavailable(
                f"root Clash RTL generation failed: {error}"
            ) from error
        names = {
            item.semantic_signal_id: item.rtl_path
            for item in source_artifact.bindings
            if item.physical_available and item.rtl_path
        }
        result = publish_artifact(
            module,
            text,
            backend="clash",
            selected_ir_identity=selected_ir_identity,
            rtl_names=names,
            side=BindingSide.IMPLEMENTATION,
        )
        _validate_public_artifact(result, module)
        return result

    return provider.get_or_prepare(
        FormalArtifactNamespace.PREPARED,
        "root-clash-rtl-v1",
        {
            "selected_ir_identity": selected_ir_identity,
            "source_build_identity": source_artifact.build_identity,
            "clash": str(Path(clash_executable).resolve()),
            "clash_version": _tool_version(clash_executable, "--version"),
        },
        prepare,
    )


def _prepared_leg(
    property_: EquivalenceProperty,
    reference_artifact: BackendArtifact,
    implementation_artifact: BackendArtifact,
    input_semantic_ids: tuple[str, ...],
) -> PreparedCandidateEquivalence:
    bindings = BindingMap(
        (*reference_artifact.bindings, *implementation_artifact.bindings),
        map_version=max(
            reference_artifact.manifest_version,
            implementation_artifact.manifest_version,
        ),
    )
    emission = emit_miter_with_metadata(
        property_,
        bindings,
        reference_module=reference_artifact.module,
        implementation_module=implementation_artifact.module,
    )
    miter_hash = artifact_hash(emission.source)
    source = "\n".join(
        (reference_artifact.text, implementation_artifact.text, emission.source)
    )
    assumptions = stable_digest(
        {
            "schema": "zlang-root-m36-assumptions-v1",
            "relation": property_.relation_kind.value,
            "comparison_window": property_.comparison_window.to_data(),
        }
    )
    return PreparedCandidateEquivalence(
        property_,
        source,
        "m36_" + property_.id.replace(".", "_"),
        reference_artifact.artifact_hash,
        implementation_artifact.artifact_hash,
        miter_hash,
        property_.id,
        assumptions,
        stable_digest(
            {
                "schema": "zlang-root-m36-backend-v1",
                "backend": implementation_artifact.backend,
                "implementation_build_identity": implementation_artifact.build_identity,
                "reference_build_identity": reference_artifact.build_identity,
            }
        ),
        implementation_artifact.backend,
        reference_artifact,
        implementation_artifact,
        input_semantic_ids,
        emission.trace_metadata,
    )


def _m36_goal(
    property_: EquivalenceProperty,
    prepared: PreparedCandidateEquivalence,
) -> FormalGoalPlan:
    assert prepared.implementation_artifact is not None
    observations = tuple(
        dict.fromkeys((*property_.inputs, property_.implementation_output))
    )
    return FormalGoalPlan(
        "goal:root-m36:"
        + stable_digest(
            {
                "property": property_.id,
                "backend": prepared.backend,
                "artifact": prepared.implementation_artifact_hash,
            }
        ),
        property_.id,
        FormalPlanGoalKind.M36_EQUIVALENCE,
        None,
        None,
        (),
        observations,
        property_.implementation_root,
        property_.comparison_window,
        property_.comparison_window.minimum_bmc_depth,
        route=FormalExecutableRoute(
            FormalRouteKind.SEMANTIC_EQUIVALENCE,
            (_artifact_ref(prepared.implementation_artifact),),
            reference_identity=prepared.reference_artifact_hash,
        ),
        source_origin=property_.source_origin,
    )


def prepare_root_equivalence(
    module: Module,
    output: str,
    *,
    selected_ir_identity: str | None = None,
    clash_executable: str | None = None,
    yosys_executable: str | None = None,
    artifact_provider: FormalArtifactProvider | None = None,
) -> PreparedRootEquivalence:
    """Prepare both M36 legs and one M38 triangle without running a solver."""

    materialized = materialize_pure_hierarchical_output(module, output)
    selected = selected_ir_identity or default_selected_ir_identity(module)
    provider = artifact_provider or FormalArtifactProvider()
    clash = clash_executable or find_clash_executable()
    yosys = yosys_executable or shutil.which("yosys")
    if clash is None:
        raise RootEquivalenceUnavailable("root equivalence requires Clash 1.11")
    if yosys is None:
        raise RootEquivalenceUnavailable("root equivalence requires Yosys")

    output_id = f"port:{materialized.output.name}"
    input_ids = tuple(f"port:{port.name}" for port in materialized.inputs)
    reference_expression_id = expression_semantic_identity(materialized.expression)
    prefix = stable_digest(
        {
            "schema": "zlang-root-reference-v1",
            "selected_ir_identity": selected,
            "output": output_id,
            "expression": reference_expression_id,
        }
    )[:16]
    reference_name = f"ZLangRootRef_{prefix}"
    inputs = tuple((port.name, port.type) for port in materialized.inputs)
    try:
        definitions = reachable_module_callables(module, include_hierarchy=True)
        reference_text = emit_reference_model(
            reference_name,
            materialized.output.name,
            materialized.output.type,
            inputs,
            materialized.expression,
            callable_definitions=definitions,
        )
    except (TypeError, ValueError) as error:
        raise RootEquivalenceError(
            f"root semantic reference cannot be emitted: {error}"
        ) from error
    reference_module = Module(
        reference_name,
        (*materialized.inputs, materialized.output),
        (Assignment(materialized.output, materialized.expression),),
    )
    reference_names = {
        **{f"port:{port.name}": port.name for port in materialized.inputs},
        output_id: materialized.output.name,
    }
    reference_artifact = publish_artifact(
        reference_module,
        reference_text,
        backend="semantic_reference",
        selected_ir_identity=selected,
        rtl_names=reference_names,
        side=BindingSide.REFERENCE,
    )
    _validate_public_artifact(reference_artifact, reference_module)
    property_ = make_equivalence_property(
        materialized.expression,
        materialized.expression,
        candidate_class="value",
        reference_root=reference_expression_id,
        implementation_root=selected,
        inputs=input_ids,
        reference_output=output_id,
        implementation_output=output_id,
        source_origin=materialized.expression.origin,
        selected_origin=materialized.expression.origin,
    )

    direct_raw = emit_systemverilog_artifact(
        module, selected_ir_identity=selected
    )
    _validate_public_artifact(direct_raw, module)
    clash_raw = _clash_rtl_artifact(
        module,
        selected_ir_identity=selected,
        clash_executable=clash,
        provider=provider,
    )
    direct_artifact = _namespace_artifact(
        direct_raw,
        module,
        backend="direct_systemverilog",
        selected_ir_identity=selected,
        yosys_executable=yosys,
        provider=provider,
    )
    clash_artifact = _namespace_artifact(
        clash_raw,
        module,
        backend="clash",
        selected_ir_identity=selected,
        yosys_executable=yosys,
        provider=provider,
    )
    if clash_artifact.module == direct_artifact.module:
        raise RootEquivalenceError("formal backend namespaces must be distinct")
    clash_leg = _prepared_leg(property_, reference_artifact, clash_artifact, input_ids)
    direct_leg = _prepared_leg(property_, reference_artifact, direct_artifact, input_ids)

    m38_property = CrossBackendProperty(
        "m38.root." + stable_digest(
            {
                "selected_ir_identity": selected,
                "output": output_id,
                "m36_property": property_.id,
            }
        )[:16],
        CrossBackendRelation.SAME_CYCLE_VALUE,
        selected,
        (output_id,),
        None,
        None,
        0,
        0,
        source_origin=materialized.expression.origin,
    )
    clash_plan = _m36_goal(property_, clash_leg)
    direct_plan = _m36_goal(property_, direct_leg)
    m38_plan = FormalGoalPlan(
        "goal:root-m38:"
        + stable_digest(
            {
                "property": m38_property.id,
                "clash": clash_artifact.artifact_hash,
                "direct": direct_artifact.artifact_hash,
            }
        ),
        m38_property.id,
        FormalPlanGoalKind.M38_EQUIVALENCE,
        None,
        None,
        (),
        (output_id,),
        selected,
        m38_property.comparison_window,
        m38_property.comparison_window.minimum_bmc_depth,
        route=FormalExecutableRoute(
            FormalRouteKind.CROSS_BACKEND_EQUIVALENCE,
            (_artifact_ref(clash_artifact), _artifact_ref(direct_artifact)),
        ),
        source_origin=materialized.expression.origin,
    )
    plan = RootEquivalencePlan(
        selected,
        output_id,
        reference_artifact.artifact_hash,
        clash_plan,
        direct_plan,
        m38_plan,
    )
    return PreparedRootEquivalence(
        materialized,
        property_,
        m38_property,
        reference_artifact,
        clash_leg,
        direct_leg,
        plan,
    )


def _m36_result(
    prepared: PreparedCandidateEquivalence,
    *,
    mode: EquivalenceMode,
    depth: int,
    solver: str,
    timeout_seconds: int,
    work_directory: Path | None,
    provider: FormalArtifactProvider,
) -> EquivalenceResult:
    recipe = {
        "schema": "zlang-root-m36-execution-v1",
        "property": prepared.property_identity,
        "backend": prepared.backend,
        "reference_artifact": prepared.reference_artifact_hash,
        "implementation_artifact": prepared.implementation_artifact_hash,
        "harness": prepared.harness_hash,
        "mode": mode.value,
        "depth": depth,
        "solver": solver,
        "timeout_seconds": timeout_seconds,
    }
    job_directory = (
        None
        if work_directory is None
        else Path(work_directory) / "M36" / stable_digest(recipe)
    )
    return provider.get_or_prepare(
        FormalArtifactNamespace.M36,
        "root-equivalence-result-v1",
        recipe,
        lambda: run_equivalence_formal(
            prepared.property,
            prepared.source,
            top=prepared.top,
            backend=prepared.backend,
            mode=mode,
            depth=depth,
            solver=solver,
            reference_hash=prepared.reference_artifact_hash,
            implementation_hash=prepared.implementation_artifact_hash,
            timeout_seconds=timeout_seconds,
            work_directory=job_directory,
            trace_metadata=prepared.trace_metadata,
        ),
        encode=equivalence_result_to_data,
        decode=equivalence_result_from_data,
        cacheable=decisive_formal_cacheable,
    )


def execute_root_equivalence(
    prepared: PreparedRootEquivalence,
    *,
    mode: EquivalenceMode = EquivalenceMode.BMC,
    depth: int = 4,
    solver: str = "z3",
    timeout_seconds: int = 120,
    work_directory: Path | None = None,
    artifact_provider: FormalArtifactProvider | None = None,
) -> RootEquivalenceExecution:
    """Run both existing M36 legs and advisory M38 for the prepared root."""

    if not isinstance(prepared, PreparedRootEquivalence):
        raise TypeError("root equivalence execution requires a prepared root")
    provider = artifact_provider or FormalArtifactProvider()
    clash_result = _m36_result(
        prepared.clash,
        mode=mode,
        depth=depth,
        solver=solver,
        timeout_seconds=timeout_seconds,
        work_directory=work_directory,
        provider=provider,
    )
    direct_result = _m36_result(
        prepared.direct_systemverilog,
        mode=mode,
        depth=depth,
        solver=solver,
        timeout_seconds=timeout_seconds,
        work_directory=work_directory,
        provider=provider,
    )
    m38_result: CrossBackendResult | None = None
    if (
        clash_result.status in _DECISIVE_M36
        and direct_result.status in _DECISIVE_M36
    ):
        assert prepared.clash.implementation_artifact is not None
        assert prepared.direct_systemverilog.implementation_artifact is not None
        m38_mode = CrossBackendMode(mode.value)
        recipe = {
            "schema": "zlang-root-m38-execution-v1",
            "property": prepared.m38_property.id,
            "left_artifact": prepared.clash.implementation_artifact_hash,
            "right_artifact": prepared.direct_systemverilog.implementation_artifact_hash,
            "inputs": list(prepared.clash.input_semantic_ids),
            "mode": m38_mode.value,
            "depth": depth,
            "solver": solver,
            "timeout_seconds": timeout_seconds,
        }
        job_directory = (
            None
            if work_directory is None
            else Path(work_directory) / "M38" / stable_digest(recipe)
        )
        m38_result = provider.get_or_prepare(
            FormalArtifactNamespace.M38,
            "root-cross-backend-result-v1",
            recipe,
            lambda: run_cross_backend_formal(
                prepared.m38_property,
                prepared.clash.implementation_artifact,
                prepared.direct_systemverilog.implementation_artifact,
                inputs=prepared.clash.input_semantic_ids,
                mode=m38_mode,
                depth=depth,
                solver=solver,
                timeout_seconds=timeout_seconds,
                work_directory=job_directory,
            ),
            encode=cross_backend_result_to_data,
            decode=cross_backend_result_from_data,
            cacheable=decisive_formal_cacheable,
        )
    triangle = M38EvidenceReport.from_results(
        prepared.plan.m38,
        m38_result,
        M36EvidenceLeg.from_result(prepared.plan.clash_m36, clash_result),
        M36EvidenceLeg.from_result(
            prepared.plan.direct_systemverilog_m36, direct_result
        ),
    )
    return RootEquivalenceExecution(
        clash_result, direct_result, m38_result, triangle
    )


__all__ = [
    "PreparedRootEquivalence",
    "RootEquivalenceError",
    "RootEquivalenceExecution",
    "RootEquivalencePlan",
    "RootEquivalenceUnavailable",
    "execute_root_equivalence",
    "prepare_root_equivalence",
]
