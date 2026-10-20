"""Real deterministic miter fields for immutable replay fixtures."""

from zlang.equivalence import artifact_hash, emit_miter_with_metadata
from zlang.ir.equivalence import BindingMap


def prepared_miter_fields(property_, reference, implementation):
    emission = emit_miter_with_metadata(
        property_, BindingMap((*reference.bindings, *implementation.bindings)),
        reference_module=reference.module, implementation_module=implementation.module,
        clock_name=property_.implementation_clock or "clock",
        reset_name=property_.implementation_reset or "reset",
    )
    return (
        "\n".join((reference.text, implementation.text, emission.source)),
        "semantic_equivalence_" + property_.id.replace(".", "_"),
        artifact_hash(emission.source), emission.trace_metadata,
    )
