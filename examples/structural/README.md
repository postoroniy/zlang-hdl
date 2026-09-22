# Structural and synthesis witnesses

These sources are natural ZLang descriptions of hardware structures that can
stress elaboration, selected IR, RTL emission, or downstream synthesis. They
are compiler-independent witnesses, not preferred hand-written RTL recipes.

The checked-in default parameters form the fast correctness gate. The shared
catalog defines `small`, `medium`, `large`, and `stress` parameter profiles.
Only the first two are used for the recorded baseline; larger profiles are
explicit investigations and are not routine test requirements.

Run the correctness and tool acceptance gate with:

```sh
.venv/bin/pytest -q tests/structural/test_structural_witnesses.py
```

Regenerate a metrics report with:

```sh
.venv/bin/python tools/structural_synthesis_baseline.py \
  --profile small \
  --json /tmp/zlang-structural-small.json \
  --markdown /tmp/zlang-structural-small.md
```

Use `--yosys-stages` to run fresh cumulative Yosys processes through
`read_verilog`, `hierarchy`, `proc`, `opt_expr`, and `opt_clean; check`. A
`stat` snapshot after every stage records wire/cell growth. A stage failure is
recorded in the JSON; it is not converted into a guessed QoR result. The runner
never enables egglog or changes implementation policy.

QoR measurements are observations. Stable correctness, determinism, single-top
emission, Verilator lint, and the reduced Yosys path are regression gates;
machine-dependent wall time and RSS are not.
