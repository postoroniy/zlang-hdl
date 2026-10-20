"""Bind retained proof source to its typed relation and published RTL legs."""

from zlang.equivalence import artifact_hash, emit_miter_with_metadata
from zlang.formal_primitive_models import validate_primitive_models
from zlang.ir.equivalence import BindingMap


REPLAY_INTEGRITY_SCHEMA = "zlang-equivalence-replay-integrity-v2"


def validate_prepared_equivalence_source(bundle):
    """Reconstruct the miter before accepting retained source or cache evidence."""
    reference = bundle.reference_artifact
    implementation = bundle.implementation_artifact
    for artifact, recorded in (
        (reference, bundle.reference_artifact_hash),
        (implementation, bundle.implementation_artifact_hash),
    ):
        if artifact_hash(artifact.text) != artifact.artifact_hash or recorded != artifact.artifact_hash:
            raise ValueError("prepared replay identity does not match its RTL artifact hash")
        validate_primitive_models(artifact)
    emission = emit_miter_with_metadata(
        bundle.property,
        BindingMap((*reference.bindings, *implementation.bindings)),
        reference_module=reference.module,
        implementation_module=implementation.module,
        clock_name=bundle.property.implementation_clock or "clock",
        reset_name=bundle.property.implementation_reset or "reset",
    )
    expected = "\n".join((reference.text, implementation.text, emission.source))
    if (
        bundle.source != expected
        or bundle.harness_hash != artifact_hash(emission.source)
        or bundle.top != "semantic_equivalence_" + bundle.property.id.replace(".", "_")
        or bundle.trace_metadata != emission.trace_metadata
        or bundle.input_semantic_ids != bundle.property.inputs
    ):
        raise ValueError(
            "prepared replay identity does not match its exact RTL, typed miter, "
            "top, inputs or trace metadata"
        )
