"""Compiler-owned M39 adapter for the frozen M36 RTL proof routes.

The adapter deliberately builds a small, backend-independent value module for
one already-typed candidate.  It does not add a new equivalence relation: M36
still owns the reference model, binding validation, latency alignment, miter,
and result semantics.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
import json
import tempfile
from typing import Any, Iterable

from zlang.backend.systemverilog import (
    SystemVerilogEmissionError,
    emit_artifact as emit_systemverilog_artifact,
)
from zlang.backend.systemverilog.target import emit_target_artifact
from zlang.backend.manifest import MANIFEST_VERSION, BackendArtifact, publish_artifact
from zlang.backend.naming import RTL_NAMING_SCHEMA
from zlang.common import stable_digest
from zlang.costs import CandidateCost, extract_best
from zlang.equivalence import (
    MiterTraceMetadata,
    artifact_hash,
    emit_miter_with_metadata,
    emit_reference_model,
    make_equivalence_property,
    run_equivalence_formal,
)
from zlang.equivalence_artifact_domain import (
    validate_prepared_equivalence_domains,
)
from zlang.formal_artifact_provider import (
    FormalArtifactNamespace,
    FormalArtifactProvider,
)
from zlang.formal import use_formal_toolchain
from zlang.formal_exploration import (
    FormalExplorationConfig,
    FormalExplorationError,
    FormalPolicy,
)
from zlang.ir.cdc import (
    ClockDomain,
    clock_domain_data,
    clock_domain_from_data,
)
from zlang.ir.equivalence import (
    BindingMap,
    BindingSide,
    EquivalenceMode,
    EquivalenceProperty,
    EquivalenceRelation,
)
from zlang.ir.comparison_window import ComparisonWindow, ComparisonWindowKind
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.ir.module import (
    Assignment,
    Module,
    Port,
    PortDirection,
    dependency_context_identity,
)
from zlang.ir.target import ImplementationGraph
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.ir.types import StructType, TupleType, VecType
from zlang.ir.type_codec import canonical_type_data, canonical_type_from_data
from zlang.source import SourceOrigin
from zlang.timing import TimingInfo, timing_info, validate_timed_candidate


class FormalCandidateUnavailable(FormalExplorationError):
    """The frozen candidate cannot reach the production M36 route."""


@dataclass(frozen=True)
class PhysicalTargetFormalCandidate:
    """One complete value schedule plus its selected physical target graph."""

    expression: object
    module: Module
    implementation_graph: ImplementationGraph
    semantic_identity: str
    implementation_identity: str
    cost: CandidateCost
    candidate_class: str = "pipeline"


@dataclass(frozen=True)
class PreparedCandidateEquivalence:
    """One completely prepared existing M36 candidate/backend route.

    This is a compiler preparation product, not a new equivalence relation.  It
    retains the two artifacts which the historical M39-only adapter used to
    discard after building its miter.  Retaining those typed artifacts lets the
    compiler evidence path retain the direct-SV M36 leg for deterministic replay.

    The trailing optional fields preserve the private ``_ProofBundle`` test
    construction API while callers migrate to this public product.
    """

    property: EquivalenceProperty | object
    source: str
    top: str
    reference_artifact_hash: str
    implementation_artifact_hash: str
    harness_hash: str
    property_identity: str
    assumptions_identity: str
    backend_identity: str
    backend: str = "direct_systemverilog"
    reference_artifact: BackendArtifact | None = None
    implementation_artifact: BackendArtifact | None = None
    input_semantic_ids: tuple[str, ...] = ()
    trace_metadata: MiterTraceMetadata | None = None


# Compatibility for focused cache tests which exercised the former private
# preparation object directly.  Production code uses the public name above.
_ProofBundle = PreparedCandidateEquivalence


@dataclass(frozen=True)
class _CandidateEquivalenceShape:
    implementation: object
    reference: object
    module: Module
    reference_module: Module
    property: EquivalenceProperty
    reference_rtl: str
    reference_rtl_names: dict[str, str]
    input_semantic_ids: tuple[str, ...]
    output_name: str
    clock: str | None
    reset: str | None


def _proof_bundle_fingerprint(
    bundle: PreparedCandidateEquivalence,
) -> dict[str, object]:
    result = {
        "property_identity": bundle.property_identity,
        "reference_artifact_hash": bundle.reference_artifact_hash,
        "implementation_artifact_hash": bundle.implementation_artifact_hash,
        "harness_hash": bundle.harness_hash,
        "assumptions_identity": bundle.assumptions_identity,
        "backend_identity": bundle.backend_identity,
        "source_hash": artifact_hash(bundle.source),
        "top": bundle.top,
        "backend": bundle.backend,
    }
    if bundle.reference_artifact is not None:
        result["reference_build_identity"] = (
            bundle.reference_artifact.build_identity
        )
    if bundle.implementation_artifact is not None:
        result["implementation_build_identity"] = (
            bundle.implementation_artifact.build_identity
        )
    return result


def _m39_work_directory(
    candidate: object,
    candidate_key: str,
    bundle: PreparedCandidateEquivalence,
    config: FormalExplorationConfig,
    expected_mode: ProofMode,
) -> Path:
    """Return a retained workspace unique to the exact M39 proof recipe."""

    work_identity = stable_digest({
        "schema": "zlang-m39-work-v1",
        "candidate": candidate_key,
        # The same selected implementation expression may occur at two
        # independent sites with different semantic references.  Keep their
        # retained SBY logs physically distinct.
        "property": bundle.property_identity,
        "harness": bundle.harness_hash,
        "reference_artifact": bundle.reference_artifact_hash,
        "implementation_artifact": bundle.implementation_artifact_hash,
        "dependency": (
            config.dependency_identity
            or getattr(candidate, "dependency_identity", None)
            or dependency_context_identity(candidate)
        ),
        "mode": expected_mode.value,
        "depth": config.bmc_depth,
        "timeout_seconds": config.timeout_seconds,
        "engine": config.engine,
        "solver": config.solver,
    })
    if config.work_directory is None:
        return Path(tempfile.mkdtemp(prefix="zlang-m39-proof-"))
    return (
        Path(config.work_directory).resolve(strict=False)
        / "m39"
        / work_identity
    )


def _origin_data(origin: SourceOrigin | None) -> object:
    return None if origin is None else origin.to_data()


def _origin_from_data(value: object) -> SourceOrigin | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("prepared M36 source origin must be an object")
    return SourceOrigin.from_data(value)


def _property_data(property_: EquivalenceProperty) -> dict[str, object]:
    return {
        "id": property_.id,
        "relation_kind": property_.relation_kind.value,
        "reference_root": property_.reference_root,
        "implementation_root": property_.implementation_root,
        "canonical_type": canonical_type_data(property_.canonical_type),
        "inputs": list(property_.inputs),
        "reference_output": property_.reference_output,
        "implementation_output": property_.implementation_output,
        "reference_latency": property_.reference_latency,
        "implementation_latency": property_.implementation_latency,
        "reference_ii": property_.reference_ii,
        "implementation_ii": property_.implementation_ii,
        "reference_clock": property_.reference_clock,
        "implementation_clock": property_.implementation_clock,
        "reference_reset": property_.reference_reset,
        "implementation_reset": property_.implementation_reset,
        "latency_delta": property_.latency_delta,
        "comparison_window": property_.comparison_window.to_data(),
        "source_origin": _origin_data(property_.source_origin),
        "selected_origin": _origin_data(property_.selected_origin),
        "candidate_class": property_.candidate_class,
        "clock_domain_contract": clock_domain_data(
            property_.clock_domain_contract
        ),
    }


def _property_from_data(value: object) -> EquivalenceProperty:
    if not isinstance(value, dict):
        raise ValueError("prepared M36 property must be an object")
    expected = {
        "id", "relation_kind", "reference_root", "implementation_root",
        "canonical_type", "inputs", "reference_output", "implementation_output",
        "reference_latency", "implementation_latency", "reference_ii",
        "implementation_ii", "reference_clock", "implementation_clock",
        "reference_reset", "implementation_reset", "latency_delta",
        "comparison_window", "source_origin", "selected_origin", "candidate_class",
        "clock_domain_contract",
    }
    if set(value) != expected:
        raise ValueError("prepared M36 property fields are invalid")
    inputs = value["inputs"]
    window = value["comparison_window"]
    if not isinstance(inputs, list) or not all(isinstance(item, str) for item in inputs):
        raise ValueError("prepared M36 inputs must be strings")
    if not isinstance(window, dict) or set(window) != {
        "kind", "fill_cycles", "reset_release_cycles",
        "first_comparison_cycle", "minimum_bmc_depth",
    }:
        raise ValueError("prepared M36 comparison window is invalid")
    typed_window = ComparisonWindow(
        ComparisonWindowKind(str(window["kind"])),
        int(window["fill_cycles"]),
        int(window["reset_release_cycles"]),
    )
    if window != typed_window.to_data():
        raise ValueError("prepared M36 comparison window metadata is stale")
    return EquivalenceProperty(
        str(value["id"]),
        EquivalenceRelation(str(value["relation_kind"])),
        str(value["reference_root"]),
        str(value["implementation_root"]),
        canonical_type_from_data(value["canonical_type"]),
        tuple(inputs),
        str(value["reference_output"]),
        str(value["implementation_output"]),
        int(value["reference_latency"]),
        int(value["implementation_latency"]),
        int(value["reference_ii"]),
        int(value["implementation_ii"]),
        value["reference_clock"],
        value["implementation_clock"],
        value["reference_reset"],
        value["implementation_reset"],
        int(value["latency_delta"]),
        typed_window,
        _origin_from_data(value["source_origin"]),
        _origin_from_data(value["selected_origin"]),
        str(value["candidate_class"]),
        clock_domain_from_data(value["clock_domain_contract"]),
    )


def _artifact_data(artifact: BackendArtifact | None) -> object:
    if artifact is None:
        return None
    return {"manifest": json.loads(artifact.to_json()), "text": artifact.text}


def _artifact_from_data(value: object) -> BackendArtifact | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"manifest", "text"}:
        raise ValueError("prepared M36 artifact payload is invalid")
    text = value["text"]
    if not isinstance(text, str):
        raise ValueError("prepared M36 artifact text must be a string")
    artifact = replace(BackendArtifact.from_json(value["manifest"]), text=text)
    if artifact_hash(text) != artifact.artifact_hash:
        raise ValueError("prepared M36 artifact text hash does not match its manifest")
    return artifact


def _trace_metadata_data(value: MiterTraceMetadata | None) -> object:
    if value is None:
        return None
    return {
        "reference_output": value.reference_output,
        "implementation_output": value.implementation_output,
        "reset": value.reset,
        "comparison_valid": value.comparison_valid,
    }


def _trace_metadata_from_data(value: object) -> MiterTraceMetadata | None:
    if value is None:
        return None
    expected = {
        "reference_output", "implementation_output", "reset",
        "comparison_valid",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("prepared M36 trace metadata is invalid")
    reference = value["reference_output"]
    implementation = value["implementation_output"]
    reset = value["reset"]
    comparison_valid = value["comparison_valid"]
    if not isinstance(reference, str) or not isinstance(implementation, str):
        raise ValueError("prepared M36 output trace names must be strings")
    if reset is not None and not isinstance(reset, str):
        raise ValueError("prepared M36 reset trace name must be a string")
    if comparison_valid is not None and not isinstance(comparison_valid, str):
        raise ValueError(
            "prepared M36 comparison-valid trace name must be a string"
        )
    return MiterTraceMetadata(
        reference, implementation, reset, comparison_valid,
    )


def _encode_prepared_candidate(
    bundle: PreparedCandidateEquivalence,
) -> dict[str, object]:
    if isinstance(bundle.property, EquivalenceProperty):
        validate_prepared_equivalence_domains(
            bundle.property,
            bundle.reference_artifact,
            bundle.implementation_artifact,
        )
    return {
        "schema": "zlang-prepared-candidate-equivalence-v2",
        "property": _property_data(bundle.property),
        "source": bundle.source,
        "top": bundle.top,
        "reference_artifact_hash": bundle.reference_artifact_hash,
        "implementation_artifact_hash": bundle.implementation_artifact_hash,
        "harness_hash": bundle.harness_hash,
        "property_identity": bundle.property_identity,
        "assumptions_identity": bundle.assumptions_identity,
        "backend_identity": bundle.backend_identity,
        "backend": bundle.backend,
        "reference_artifact": _artifact_data(bundle.reference_artifact),
        "implementation_artifact": _artifact_data(bundle.implementation_artifact),
        "input_semantic_ids": list(bundle.input_semantic_ids),
        "trace_metadata": _trace_metadata_data(bundle.trace_metadata),
    }


def _decode_prepared_candidate(value: object) -> PreparedCandidateEquivalence:
    if not isinstance(value, dict):
        raise ValueError("prepared candidate equivalence must be an object")
    expected = {
        "schema", "property", "source", "top", "reference_artifact_hash",
        "implementation_artifact_hash", "harness_hash", "property_identity",
        "assumptions_identity", "backend_identity", "backend",
        "reference_artifact", "implementation_artifact", "input_semantic_ids",
        "trace_metadata",
    }
    if set(value) != expected or value["schema"] != "zlang-prepared-candidate-equivalence-v2":
        raise ValueError("prepared candidate equivalence schema/fields are invalid")
    inputs = value["input_semantic_ids"]
    if not isinstance(inputs, list) or not all(isinstance(item, str) for item in inputs):
        raise ValueError("prepared candidate input identities must be strings")
    result = PreparedCandidateEquivalence(
        _property_from_data(value["property"]),
        str(value["source"]),
        str(value["top"]),
        str(value["reference_artifact_hash"]),
        str(value["implementation_artifact_hash"]),
        str(value["harness_hash"]),
        str(value["property_identity"]),
        str(value["assumptions_identity"]),
        str(value["backend_identity"]),
        str(value["backend"]),
        _artifact_from_data(value["reference_artifact"]),
        _artifact_from_data(value["implementation_artifact"]),
        tuple(inputs),
        _trace_metadata_from_data(value["trace_metadata"]),
    )
    if result.property.id != result.property_identity:
        raise ValueError("prepared candidate property identity is inconsistent")
    validate_prepared_equivalence_domains(
        result.property,
        result.reference_artifact,
        result.implementation_artifact,
    )
    return result


def prepared_candidate_equivalence_to_data(
    value: PreparedCandidateEquivalence,
) -> dict[str, object]:
    """Encode one already-prepared M36 leg for immutable replay.

    The provider cache and verification bundles deliberately share this exact
    codec. Publishing it does not expose candidate selection or introduce a
    new relation: it serializes only the artifacts and miter already required
    by the existing M36 runner.
    """

    if not isinstance(value.property, EquivalenceProperty):
        raise ValueError("prepared candidate requires typed M36 property IR")
    return _encode_prepared_candidate(value)


def prepared_candidate_equivalence_from_data(
    value: object,
) -> PreparedCandidateEquivalence:
    """Strictly restore one already-prepared M36 leg."""

    result = _decode_prepared_candidate(value)
    if not isinstance(result.property, EquivalenceProperty):
        raise ValueError("prepared candidate requires typed M36 property IR")
    if result.reference_artifact is None or result.implementation_artifact is None:
        raise ValueError("replayable M36 candidate requires both exact artifacts")
    if result.reference_artifact_hash != result.reference_artifact.artifact_hash:
        raise ValueError("prepared M36 reference artifact hash is inconsistent")
    if (
        result.implementation_artifact_hash
        != result.implementation_artifact.artifact_hash
    ):
        raise ValueError("prepared M36 implementation artifact hash is inconsistent")
    if result.backend != result.implementation_artifact.backend:
        raise ValueError("prepared M36 backend differs from its implementation artifact")
    if (
        result.property.implementation_root
        != result.implementation_artifact.selected_ir_identity
    ):
        raise ValueError("prepared M36 candidate identity differs from its artifact")
    if not result.source or not result.top or not result.harness_hash:
        raise ValueError("prepared M36 source, top, and harness hash are required")
    validate_prepared_equivalence_domains(
        result.property,
        result.reference_artifact,
        result.implementation_artifact,
    )
    return result


@dataclass(frozen=True)
class _PipelineFormalCandidate:
    pipeline_candidate: object
    expression: object
    semantic_identity: str
    implementation_identity: str
    cost: CandidateCost
    timing_relation: object | None = None
    candidate_class: str = "m31"


def _candidate_class(candidate: object) -> str:
    explicit = getattr(candidate, "candidate_class", None)
    if explicit in {"value", "m27", "m29", "m31", "m32", "pipeline"}:
        return str(explicit)
    stages = tuple(str(item) for item in getattr(candidate, "stages", ()))
    if any("pipeline" in item for item in stages):
        return "m31"
    if any("reduction" in item for item in stages):
        return "m32"
    if getattr(candidate, "architecture", None) is not None:
        return "m29"
    if any(item == "value" for item in stages):
        return "m27"
    return "value"


def candidate_equivalence_class(candidate: object) -> str:
    """Return the frozen M36 candidate family used by M39 preparation."""

    return _candidate_class(candidate)


def _input_refs(expression: object) -> dict[str, object]:
    # Reuse the exploration traversal policy: exact reductions retain their
    # selected implementation and implementation choices follow only the
    # selected branch.  This is the same typed graph supplied to each backend.
    from zlang.exploration import _input_refs as exploration_input_refs

    return exploration_input_refs(expression)


class _M36CandidateVerifierBase:
    """Shared preparation for the production direct-SystemVerilog M36 route."""

    def __init__(
        self,
        reference_expression: object,
        *,
        candidate_class: str | None = None,
        artifact_provider: FormalArtifactProvider | None = None,
        clock_domain_contract: ClockDomain | None = None,
        unavailable_reason: str | None = None,
    ):
        self.reference_expression = reference_expression
        self.candidate_class = candidate_class
        if clock_domain_contract is not None:
            clock_domain_contract.validate()
        self.clock_domain_contract = clock_domain_contract
        if unavailable_reason is not None and not unavailable_reason:
            raise ValueError("formal candidate unavailable reason cannot be empty")
        self.unavailable_reason = unavailable_reason
        self.artifact_provider = (
            artifact_provider
            if artifact_provider is not None
            else FormalArtifactProvider()
        )

    def _key(self, candidate: object) -> str:
        return str(getattr(candidate, "implementation_identity", "")) or stable_digest(
            {
                "schema": "zlang-m39-candidate-fallback-v1",
                "expression": expression_semantic_identity(candidate.expression),
            }
        )


    def prepare(
        self,
        candidate: object,
        config: FormalExplorationConfig,
    ) -> PreparedCandidateEquivalence:
        """Prepare the production direct-SystemVerilog M36 leg."""

        if self.unavailable_reason is not None:
            raise FormalCandidateUnavailable(self.unavailable_reason)
        implementation = getattr(candidate, "expression", None)
        if implementation is None:
            raise FormalCandidateUnavailable(
                "M36 candidate has no typed implementation expression"
            )
        dependency_identity = (
            config.dependency_identity
            or getattr(candidate, "dependency_identity", None)
            or dependency_context_identity(candidate)
        )
        tool_route = {
            "emitter": "zlang-direct-systemverilog",
            "manifest_version": MANIFEST_VERSION,
        }
        recipe = {
            "schema": "zlang-m36-candidate-backend-preparation-v1",
            "rtl_naming": RTL_NAMING_SCHEMA,
            "backend": "direct_systemverilog",
            "candidate": self._key(candidate),
            "candidate_semantics": expression_semantic_identity(implementation),
            "reference_semantics": expression_semantic_identity(
                self.reference_expression
            ),
            "candidate_class": self.candidate_class or _candidate_class(candidate),
            "clock_domain_contract": clock_domain_data(
                self.clock_domain_contract
            ),
            "dependency_identity": dependency_identity,
            "tool_route": tool_route,
        }
        physical_graph = getattr(candidate, "implementation_graph", None)
        if physical_graph is not None:
            if not isinstance(physical_graph, ImplementationGraph):
                raise FormalCandidateUnavailable(
                    "M36 physical candidate has an invalid implementation graph"
                )
            recipe["physical_graph_identity"] = physical_graph.identity
        return self.artifact_provider.get_or_prepare(
            FormalArtifactNamespace.M36,
            "direct-systemverilog-candidate-equivalence-preparation-v1",
            recipe,
            lambda: self._prepare_candidate_equivalence(candidate, config),
            encode=_encode_prepared_candidate,
            decode=_decode_prepared_candidate,
            fingerprint=_proof_bundle_fingerprint,
        )


    def _prepare_candidate_equivalence(
        self,
        candidate: object,
        config: FormalExplorationConfig,
    ) -> PreparedCandidateEquivalence:
        if config.engine != "sby":
            raise FormalCandidateUnavailable(
                "M36 direct-SystemVerilog route supports only the configured "
                "'sby' engine"
            )

        implementation = candidate.expression
        reference = self.reference_expression
        physical_graph = getattr(candidate, "implementation_graph", None)
        physical_module = getattr(candidate, "module", None)
        if (physical_graph is None) != (physical_module is None):
            raise FormalCandidateUnavailable(
                "M36 physical candidate requires both module and implementation graph"
            )
        if physical_graph is not None and (
            not isinstance(physical_graph, ImplementationGraph)
            or not isinstance(physical_module, Module)
        ):
            raise FormalCandidateUnavailable(
                "M36 physical candidate metadata has invalid typed objects"
            )
        if implementation.type != reference.type:
            raise FormalCandidateUnavailable(
                "M36 candidate/reference canonical types differ"
            )
        if isinstance(implementation.type, (StructType, TupleType, VecType)):
            raise FormalCandidateUnavailable(
                "M36 formal-aware exploration currently requires a scalar result"
            )
        inputs = _input_refs(implementation)
        reference_inputs = _input_refs(reference)
        if inputs != reference_inputs:
            raise FormalCandidateUnavailable(
                "M36 candidate/reference input bindings differ"
            )

        implementation_latency = timing_info(implementation).latency
        timed = implementation_latency != timing_info(reference).latency
        physical_domain = None
        if timed and physical_module is not None:
            if len(physical_module.clock_domains) != 1:
                raise FormalCandidateUnavailable(
                    "M36 physical candidate requires exactly one clock/reset domain"
                )
            physical_domain = physical_module.clock_domains[0]
            physical_domain.validate()
            if (
                self.clock_domain_contract is not None
                and physical_domain != self.clock_domain_contract
            ):
                raise FormalCandidateUnavailable(
                    "M36 physical candidate clock/reset domain differs from the "
                    "requested proof contract"
                )
        proof_domain = physical_domain or self.clock_domain_contract
        clock = proof_domain.clock if timed and proof_domain is not None else (
            "clock" if timed else None
        )
        reset = proof_domain.reset if timed and proof_domain is not None else (
            "reset" if timed else None
        )
        output_name = "result"
        if physical_graph is not None:
            matches = tuple(
                assignment
                for assignment in physical_module.assignments
                if assignment.expression == implementation
                and hasattr(assignment.target, "name")
            )
            if len(matches) != 1:
                raise FormalCandidateUnavailable(
                    "M36 physical candidate requires one exact output assignment"
                )
            output_name = matches[0].target.name
        occupied = set(inputs)
        while physical_graph is None and output_name in occupied:
            output_name += "_"
        prefix = stable_digest({
            "schema": "zlang-m39-module-name-v1",
            "candidate": self._key(candidate),
        })[:12]
        implementation_module = f"ZLangM39ImplSv_{prefix}"
        reference_module = f"ZLangM39Ref_{prefix}"
        ports = tuple(
            Port(PortDirection.INPUT, name, type_)
            for name, type_ in sorted(inputs.items())
        )
        output = Port(PortDirection.OUTPUT, output_name, implementation.type)
        if physical_graph is None:
            module = Module(
                implementation_module,
                (*ports, output),
                (Assignment(output, implementation),),
                clock=clock,
                reset=reset,
                clock_domains=(
                    ((proof_domain or ClockDomain(clock, reset)),)
                    if clock is not None and reset is not None else ()
                ),
            )
        else:
            module = replace(physical_module, name=implementation_module)
            output = next(
                item for item in module.ports
                if item.direction is PortDirection.OUTPUT and item.name == output_name
            )
        selected_identity = self._key(candidate)
        try:
            implementation_artifact = (
                emit_systemverilog_artifact(
                    module,
                    selected_ir_identity=selected_identity,
                )
                if physical_graph is None
                else emit_target_artifact(
                    module,
                    physical_graph,
                    simulation_model=True,
                    selected_ir_identity=selected_identity,
                )
            )
        except (SystemVerilogEmissionError, ValueError) as error:
            raise FormalCandidateUnavailable(
                "M36 direct-SystemVerilog proof route could not emit candidate "
                f"RTL: {error}"
            ) from error

        reference_rtl = emit_reference_model(
            reference_module,
            output_name,
            reference.type,
            tuple(sorted(reference_inputs.items())),
            reference,
            clock_name=clock,
            reset_name=reset,
        )
        reference_ir_module = replace(
            module,
            name=reference_module,
            assignments=(Assignment(output, reference),),
        )
        candidate_kind = self.candidate_class or _candidate_class(candidate)
        reference_timing = timing_info(reference)
        candidate_timing = timing_info(implementation)
        if timed:
            reference_timing = TimingInfo(
                reference_timing.latency, 1, clock, reset
            )
            candidate_timing = TimingInfo(
                candidate_timing.latency, 1, clock, reset
            )
        property_ = make_equivalence_property(
            reference,
            implementation,
            candidate_class=candidate_kind,
            reference_root=expression_semantic_identity(reference),
            implementation_root=self._key(candidate),
            reference_timing=reference_timing,
            implementation_timing=candidate_timing,
            inputs=tuple(f"port:{name}" for name in sorted(inputs)),
            reference_output=f"port:{output_name}",
            implementation_output=f"port:{output_name}",
            source_origin=getattr(reference, "origin", None),
            selected_origin=getattr(implementation, "origin", None),
            clock_domain_contract=(
                proof_domain if timed else None
            ),
        )
        reference_rtl_names = {
            **{f"port:{name}": name for name in reference_inputs},
            f"port:{output_name}": output_name,
        }
        if timed:
            reference_rtl_names.update({"clock": clock, "reset": reset})
        reference_artifact = publish_artifact(
            reference_ir_module,
            reference_rtl,
            backend="semantic_reference",
            selected_ir_identity=selected_identity,
            rtl_names=reference_rtl_names,
            side=BindingSide.REFERENCE,
        )
        _validate_candidate_artifact(
            implementation_artifact,
            expected_semantic_ids=tuple(reference_rtl_names),
        )
        _validate_candidate_artifact(
            reference_artifact,
            expected_semantic_ids=tuple(reference_rtl_names),
        )
        bindings = BindingMap((
            *reference_artifact.bindings,
            *implementation_artifact.bindings,
        ))
        miter_emission = emit_miter_with_metadata(
            property_,
            bindings,
            reference_module=reference_module,
            implementation_module=implementation_module,
            clock_name=clock or "clock",
            reset_name=reset or "reset",
        )
        miter = miter_emission.source
        source = "\n".join((reference_rtl, implementation_artifact.text, miter))
        assumptions_identity = stable_digest(
            {
                "schema": "zlang-m39-m36-assumptions-v1",
                "relation": property_.relation_kind.value,
                "latency_delta": property_.latency_delta,
                "comparison_window": property_.comparison_window.to_data(),
                "clock": property_.implementation_clock,
                "reset": property_.implementation_reset,
                "ii": property_.implementation_ii,
            }
        )
        return PreparedCandidateEquivalence(
            property_,
            source,
            "m36_" + property_.id.replace(".", "_"),
            reference_artifact.artifact_hash,
            implementation_artifact.artifact_hash,
            artifact_hash(miter),
            property_.id,
            assumptions_identity,
            stable_digest({
                "schema": "zlang-m36-direct-systemverilog-artifact-route-v1",
                "rtl_build_identity": implementation_artifact.build_identity,
                "reference_build_identity": reference_artifact.build_identity,
                "manifest_version": implementation_artifact.manifest_version,
            }),
            "direct_systemverilog",
            reference_artifact,
            implementation_artifact,
            tuple(f"port:{name}" for name in sorted(inputs)),
            miter_emission.trace_metadata,
        )



class M36DirectSystemVerilogCandidateVerifier(_M36CandidateVerifierBase):
    """M39 adapter for the production direct-SystemVerilog route.

    This is the only compiler-owned RTL equivalence adapter.  It preserves the
    backend-independent reference model and publishes a direct-SV implementation
    artifact with exact bindings.
    """

    formal_route = "M36_direct_systemverilog"

    def preparation_cache_recipe(
        self,
        candidate: object,
        config: FormalExplorationConfig,
    ) -> dict[str, object] | None:
        if self.unavailable_reason is not None:
            return None
        implementation = getattr(candidate, "expression", None)
        if implementation is None:
            return None
        dependency_identity = (
            config.dependency_identity
            or getattr(candidate, "dependency_identity", None)
            or dependency_context_identity(candidate)
        )
        physical_graph = getattr(candidate, "implementation_graph", None)
        return {
            "schema": "zlang-m36-direct-systemverilog-proof-bundle-v1",
            "rtl_naming": RTL_NAMING_SCHEMA,
            "candidate": self._key(candidate),
            "candidate_semantics": expression_semantic_identity(implementation),
            "reference_semantics": expression_semantic_identity(
                self.reference_expression
            ),
            "candidate_class": self.candidate_class or _candidate_class(candidate),
            "clock_domain_contract": clock_domain_data(
                self.clock_domain_contract
            ),
            "engine": config.engine,
            "solver": config.solver,
            "dependency_identity": dependency_identity,
            "backend": "direct_systemverilog",
            "manifest_version": MANIFEST_VERSION,
            "physical_graph_identity": (
                physical_graph.identity
                if isinstance(physical_graph, ImplementationGraph)
                else None
            ),
        }

    def cache_identity(
        self, candidate: object, config: FormalExplorationConfig,
    ) -> dict[str, str]:
        if self.unavailable_reason is not None:
            return {"unavailable_reason": self.unavailable_reason}
        try:
            bundle = self.prepare(candidate, config)
        except FormalCandidateUnavailable as error:
            return {"unavailable_reason": str(error)}
        return {
            "property_identity": bundle.property_identity,
            "artifact_hash": bundle.implementation_artifact_hash,
            "reference_artifact_hash": bundle.reference_artifact_hash,
            "implementation_artifact_hash": bundle.implementation_artifact_hash,
            "harness_hash": bundle.harness_hash,
            "assumptions_identity": bundle.assumptions_identity,
            "backend_identity": bundle.backend_identity,
        }

    def __call__(self, candidate: object, config: FormalExplorationConfig) -> dict[str, object]:
        expected_mode = (
            ProofMode.PROVE
            if config.policy is FormalPolicy.REQUIRED_PROVEN else ProofMode.BMC
        )
        if self.unavailable_reason is not None:
            return {
                "status": FormalStatus.SKIPPED,
                "mode": expected_mode,
                "depth": config.bmc_depth,
                "engine": config.engine,
                "solver": config.solver,
                "backend": "direct_systemverilog",
                "reason": self.unavailable_reason,
            }
        try:
            bundle = self.prepare(candidate, config)
        except FormalCandidateUnavailable as error:
            return {
                "status": FormalStatus.SKIPPED,
                "mode": expected_mode,
                "depth": config.bmc_depth,
                "engine": config.engine,
                "solver": config.solver,
                "backend": "direct_systemverilog",
                "reason": str(error),
            }
        resolver = config.tool_resolver
        resolve = getattr(resolver, "formal_context", None)
        context = (
            resolve(engine=config.engine, solver=config.solver)
            if callable(resolve) else None
        )
        manager = (
            use_formal_toolchain(context)
            if context is not None else nullcontext()
        )
        work_directory = _m39_work_directory(
            candidate,
            self._key(candidate),
            bundle,
            config,
            expected_mode,
        )
        with manager:
            result = run_equivalence_formal(
                bundle.property,
                bundle.source,
                top=bundle.top,
                backend="direct_systemverilog",
                mode=(
                    EquivalenceMode.PROVE
                    if expected_mode is ProofMode.PROVE else EquivalenceMode.BMC
                ),
                depth=config.bmc_depth,
                solver=config.solver,
                reference_hash=bundle.reference_artifact_hash,
                implementation_hash=bundle.implementation_artifact_hash,
                timeout_seconds=config.timeout_seconds,
                work_directory=work_directory,
                trace_metadata=bundle.trace_metadata,
            )
        return {
            "status": FormalStatus(result.status.value),
            "mode": expected_mode,
            "depth": result.depth or config.bmc_depth,
            "engine": result.engine or config.engine,
            "solver": result.solver or config.solver,
            "backend": "direct_systemverilog",
            "artifact_hash": bundle.implementation_artifact_hash,
            "implementation_artifact_hash": bundle.implementation_artifact_hash,
            "reference_artifact_hash": bundle.reference_artifact_hash,
            "property_identity": bundle.property_identity,
            "harness_hash": bundle.harness_hash,
            "assumptions_identity": bundle.assumptions_identity,
            "backend_identity": bundle.backend_identity,
            "source_origin": result.source_origin,
            "selected_origin": result.selected_origin,
            "work_directory": str(work_directory),
            "reason": result.reason or "",
            "counterexample": result.counterexample,
        }


def _validate_candidate_artifact(
    artifact: BackendArtifact,
    *,
    expected_semantic_ids: tuple[str, ...],
) -> None:
    """Require complete, physically published candidate ports before proving."""

    artifact.binding_map().validate()
    by_id = {item.semantic_signal_id: item for item in artifact.bindings}
    missing = tuple(sorted(set(expected_semantic_ids) - set(by_id)))
    unavailable = tuple(
        semantic_id
        for semantic_id in sorted(expected_semantic_ids)
        if semantic_id in by_id and not by_id[semantic_id].physical_available
    )
    if missing or unavailable:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unavailable:
            details.append("not physically published " + ", ".join(unavailable))
        raise FormalCandidateUnavailable(
            "M36 candidate artifact bindings are incomplete: " + "; ".join(details)
        )


def gate_standalone_pipelines(
    module: Module,
    config: FormalExplorationConfig,
    verifier: object | None = None,
    *,
    canonical_site_keys: Iterable[tuple[str, str | None, str]] = (),
    backend: str = "direct_systemverilog",
) -> Module:
    """Apply the frozen M39 gate to ordinary implementation regions.

    Canonical ``implement`` regions are gated while their M28 candidate set is
    available.  The retained ``PipelineExploration`` records are an internal
    candidate table, so this adapter recreates the same M28 ranking and
    rewires only the selected output assignment.
    """

    if config.policy is FormalPolicy.OFF or not module.pipeline_explorations:
        return module
    from zlang.formal_exploration import gate_candidates
    from zlang.ir.expressions import CostMetric
    from zlang.candidate_sites import (
        candidate_owner_formal_domain,
        module_candidate_owner_identity,
        pipeline_site_key,
    )

    assignments = list(module.assignments)
    # A canonical ``implement`` region publishes one unified ExplorationResult.
    # Its PipelineExploration entries are planner/report metadata only and must
    # not be sent through the standalone M39 route a second time.  Persisted
    # standalone pipeline records (which predate ``implement``) retain the
    # legacy route and are gated below.
    canonical_keys = frozenset(canonical_site_keys)
    standalone = tuple(
        (index, exploration)
        for index, exploration in enumerate(module.pipeline_explorations)
        if pipeline_site_key(module, exploration) not in canonical_keys
    )
    if not standalone:
        return module
    # Keep the retained catalog order stable.  Canonical planner entries are
    # skipped by site key; only true standalone entries are replaced in place.
    updated_explorations = list(module.pipeline_explorations)
    for pipeline_index, exploration in standalone:
        wrapped, constraints, extraction = standalone_pipeline_candidate_space(
            exploration
        )
        domain, domain_limitation = candidate_owner_formal_domain(
            module, module_candidate_owner_identity(module)
        )
        if backend != "direct_systemverilog":
            raise FormalCandidateUnavailable(
                f"formal backend '{backend}' is retired; use direct_systemverilog"
            )
        selected_verifier = (
            verifier
            if verifier is not None and domain_limitation is None
            else M36DirectSystemVerilogCandidateVerifier(
                exploration.source_expression,
                candidate_class="m31",
                artifact_provider=getattr(config, "artifact_provider", None),
                clock_domain_contract=domain,
                unavailable_reason=domain_limitation,
            )
        )
        gate = gate_candidates(
            wrapped,
            extraction.evaluations,
            config,
            selected_verifier,
        )
        gated = extract_best(
            gate.eligible,
            objective=CostMetric.FMAX_EST,
            constraints=constraints,
            cost_fn=lambda item: item.cost,
        )
        selected = gated.selected.pipeline_candidate
        updated_explorations[pipeline_index] = replace(
            exploration,
            selected=selected.name,
            formal_records=gate.records,
        )
        matches = [
            index for index, assignment in enumerate(assignments)
            if assignment.target.name == exploration.output
            and assignment.signal is None
            and assignment.channel is None
        ]
        if len(matches) != 1:
            raise FormalCandidateUnavailable(
                "standalone pipeline formal gate requires one exact output assignment"
            )
        index = matches[0]
        assignments[index] = replace(assignments[index], expression=selected.expression)
    return replace(
        module,
        assignments=tuple(assignments),
        pipeline_explorations=tuple(updated_explorations),
    )


def standalone_pipeline_candidate_space(exploration: object):
    """Reconstruct the exact frozen M28 space for one retained pipeline site.

    Both the selection-phase M39 adapter and ``CandidateSiteLedger`` consume
    this helper.  Keeping one implementation prevents candidate ranks from
    drifting between evidence publication and the actual gate.
    """

    from zlang.ir.expressions import CostMetric
    from zlang.pipelines import pipeline_constraint_to_unified

    wrapped = tuple(
        _PipelineFormalCandidate(
            candidate,
            candidate.expression,
            expression_semantic_identity(exploration.source_expression),
            stable_digest({
                "schema": "zlang-m39-pipeline-candidate-v1",
                "source": expression_semantic_identity(
                    exploration.source_expression
                ),
                "name": candidate.name,
                "expression": expression_semantic_identity(candidate.expression),
                "latency": candidate.latency,
                "ii": candidate.initiation_interval,
                "transformations": list(candidate.transformations),
            }),
            CandidateCost.estimate(
                lut=candidate.estimate.lut,
                ff=candidate.estimate.ff,
                dsp=candidate.estimate.dsp,
                latency=candidate.latency,
                ii=candidate.initiation_interval,
                fmax_est=candidate.estimate.fmax_mhz,
                structural_cost=len(candidate.transformations),
            ),
            timing_relation=validate_timed_candidate(
                exploration.source_expression,
                candidate.expression,
                value_equivalent=True,
            ),
        )
        for candidate in exploration.candidates
    )
    constraints = tuple(
        pipeline_constraint_to_unified(item) for item in exploration.constraints
    )
    extraction = extract_best(
        wrapped,
        objective=CostMetric.FMAX_EST,
        constraints=constraints,
        cost_fn=lambda item: item.cost,
    )
    return wrapped, constraints, extraction


__all__ = [
    "candidate_equivalence_class",
    "FormalCandidateUnavailable",
    "M36DirectSystemVerilogCandidateVerifier",
    "PhysicalTargetFormalCandidate",
    "PreparedCandidateEquivalence",
    "prepared_candidate_equivalence_from_data",
    "prepared_candidate_equivalence_to_data",
    "validate_prepared_equivalence_domains",
    "gate_standalone_pipelines",
    "standalone_pipeline_candidate_space",
]
