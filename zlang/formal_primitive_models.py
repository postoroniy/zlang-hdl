"""Reviewed primitive-model boundary for executable and frozen RTL evidence."""

from hashlib import sha256
import re


PRIMITIVE_MODEL_POLICY_SCHEMA = "zlang-formal-primitive-models-v4"


def primitive_model_policy():
    from zlang.backend.systemverilog.target import _dsp48e1_simulation_model
    from zlang.backend.systemverilog.fpga_primitives import multiplier_models
    return {
        "schema": PRIMITIVE_MODEL_POLICY_SCHEMA,
        "dsp48e1": sha256(_dsp48e1_simulation_model().encode()).hexdigest(),
        "gowin_mult18x18": sha256(multiplier_models("gowin").encode()).hexdigest(),
        "ecp5_mult18x18d": sha256(multiplier_models("ecp5").encode()).hexdigest(),
        **{family: sha256(multiplier_models(family).encode()).hexdigest()
           for family in ("gowin9", "ice40", "nexus", "gowin_v")},
    }


def validate_primitive_models(artifact):
    """A self-consistent checksum cannot approve an obsolete DSP model."""
    if re.search(r"\bDSP48E1\s*(?:#\s*\(|[A-Za-z_][A-Za-z0-9_$]*\s*\()", artifact.text):
        from zlang.backend.systemverilog.target import _dsp48e1_simulation_model
        if _dsp48e1_simulation_model() not in artifact.text:
            raise ValueError("unreviewed or stale DSP48E1 formal primitive model")
    from zlang.backend.systemverilog.fpga_primitives import multiplier_models
    groups = (("MULT18X18", ("gowin", "nexus")), ("MULT18X18D", ("ecp5",)),
              ("MULT9X9", ("gowin9",)), ("SB_MAC16", ("ice40",)),
              ("MULT12X12", ("gowin_v",)))
    for primitive, families in groups:
        if re.search(r"\b" + primitive + r"\s*(?:#\s*\(|[A-Za-z_][A-Za-z0-9_$]*\s*\()", artifact.text):
            implementation = getattr(artifact, "implementation", None)
            bindings = getattr(implementation, "physical_binding_identities", ())
            if primitive == "MULT18X18" and bindings:
                families = ("nexus",) if any(":nexus_dsp_fabric" in b for b in bindings) else ("gowin",)
            if not any(multiplier_models(family) in artifact.text for family in families):
                raise ValueError(f"unreviewed or stale {primitive} formal primitive model")
