"""Reviewed primitive-model boundary for executable and frozen RTL evidence."""

from hashlib import sha256
import re


PRIMITIVE_MODEL_POLICY_SCHEMA = "zlang-formal-primitive-models-v2"


def primitive_model_policy():
    from zlang.backend.systemverilog.target import _dsp48e1_simulation_model
    return {
        "schema": PRIMITIVE_MODEL_POLICY_SCHEMA,
        "dsp48e1": sha256(_dsp48e1_simulation_model().encode()).hexdigest(),
    }


def validate_primitive_models(artifact):
    """A self-consistent checksum cannot approve an obsolete DSP model."""
    if re.search(r"\bDSP48E1\s*(?:#\s*\(|[A-Za-z_][A-Za-z0-9_$]*\s*\()", artifact.text):
        from zlang.backend.systemverilog.target import _dsp48e1_simulation_model
        if _dsp48e1_simulation_model() not in artifact.text:
            raise ValueError("unreviewed or stale DSP48E1 formal primitive model")
