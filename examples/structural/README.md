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
  --json build/tmp/zlang-structural-small.json \
  --markdown build/tmp/zlang-structural-small.md
```

Use `--yosys-stages` to run fresh cumulative Yosys processes through
`read_verilog`, `hierarchy`, `proc`, `opt_expr`, and `opt_clean; check`. A
`stat` snapshot after every stage records wire/cell growth. A stage failure is
recorded in the JSON; it is not converted into a guessed QoR result. The runner
never enables egglog or changes implementation policy.

QoR measurements are observations. Stable correctness, determinism, single-top
emission, Verilator lint, and the reduced Yosys path are regression gates;
machine-dependent wall time and RSS are not.

## Witness catalog

Each source keeps one structural question isolated so compile/emission growth
can be attributed to that shape rather than to a mixed benchmark:

| Source | Structural question |
| --- | --- |
| `barrel_shifter.zhl` | Width-scaled runtime shifting. |
| `byte_lane_aligner.zhl` | Correlated runtime selection of a byte window. |
| `cam.zhl` | Lowest-match lookup across runtime key/value storage images. |
| `crc_parallel.zhl` | Bounded parallel CRC expansion. |
| `crossbar.zhl` | Independent runtime input selection for each output. |
| `dynamic_permute.zhl` | General runtime vector permutation. |
| `fir.zhl` | Compile-time-tap FIR structure. |
| `generic_explosion.zhl` | Bounded nested generic/compile-time expansion. |
| `matrix_transpose.zhl` | Compile-time-only multidimensional permutation. |
| `multidimensional_index.zhl` | Static and runtime multidimensional indexing. |
| `one_hot_mux.zhl` | OR-combined one-hot selection. |
| `packet_compactor.zhl` | Stable byte compaction. |
| `prefix_network.zhl` | Reused overlapping prefix reductions. |
| `priority_encoder.zhl` | Lowest-index priority selection. |
| `reduction_tree.zhl` | Associative bitwise and modular-add reductions. |
| `scatter_12x4x4.zhl` | Wide functional scatter with bounded lowering. |
| `scatter_gather.zhl` | Runtime gather and explicit scatter collisions. |
| `variable_slice.zhl` | Range-proven packed dynamic slice. |
| `wide_arbiter.zhl` | Width-scaled fixed-priority grant. |

`test.zhl` is a private large-emission performance witness. It is deliberately
excluded from the public example corpus and release projection.
