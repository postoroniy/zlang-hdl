<a id="reference-implementation-profiles"></a>
<a id="reference-profiles-targets"></a>
# Implementation profiles


Implementation profiles keep physical policy outside portable `.zhl` source.
They are named tables in `zlang.toml` and are selected explicitly:

```toml
[profiles.release]
backend = "systemverilog"
backend-mode = "required"
target = "xc7z030ffg676-1"
allowed-transforms = ["pipeline", "dsp", "reduction"]
avoided-transforms = ["reassociate"]
objective = "maximize fmax"
architecture = "Xilinx7SymmetricDSPCascade"
architecture-mode = "preferred"
evidence-policy = "measured_required"
formal-policy = "required_bmc"

[profiles.release.constraints]
latency = { maximum = 8 }
ii = { exact = 1 }
fmax = { minimum = 100 }
dsp = { maximum = 8 }
```

`maximize fmax` prefers a legal choice with the strongest compatible frequency
measurement available under the selected evidence policy. It does not
guarantee the frequency achieved on an FPGA or ASIC. An estimate is never
reported as measured routed timing.

Schematic use inside a project containing the shown `src/fir.zhl` and profile:

```bash
zlang src/fir.zhl --profile release --systemverilog build/Fir.sv
```

A profile may select pinned scalar external-module mappings with
`external-mappings = ["vendor-add"]`.  Mapping declarations live in the
top-level `external-mappings` tables of `zlang.toml`; their source files and
hashes are fixed by `zlang.lock`. They may change generated SystemVerilog but
do not change the source-language behavior of the design.

Only the selected profile is parsed strictly. This permits a project to carry
profiles for newer compilers without making unrelated builds fail. Selecting
an unknown profile, selecting a profile without a project, or using an unknown
key is an error. Profile data is excluded from dependency resolution and lock
contents.

<a id="reference-implementation-profiles-semantic-regions"></a>
## Semantic regions

The compiler reports one stable identity for each public scalar wire-output
region. A profile can address an exact subset with `regions = ["DIGEST", ...]`.
Generate `--implementation-policy-report` once to obtain the 64-character
digests.

Region identity is derived from the logical module specialization, typed public
output binding, canonical expression identity, and exact result type. Source
paths, spans, physical instances, and generated RTL names are excluded.
Stale and duplicate identities fail explicitly. Profile-selected regions are
root-module-only and scalar-only; recursive protocol regions are unsupported.

<a id="reference-implementation-profiles-normalization-and-conflicts"></a>
## Normalization and conflicts

`choice(auto)` remains accepted for user-supplied alternatives. Scalar
`architecture(auto)`, `pipeline(auto)`, and `explore` are retired and produce
migration diagnostics. Canonical `implement` source policy is represented
through the same `ImplementationRequest` model as the selected profile and
explicit compiler options. Equal normalized contributions are harmless. Different values for
the same target, transform set, objective, constraint, evidence policy, formal
policy, or architecture are rejected with both origins in the diagnostic.

For an ordinary typed scalar output, profile transforms/constraints/objectives
run through bounded exploration. Canonical `implement` regions
have already run during semantic analysis, so their retained candidate table is
compared but never run a second time. Retained legacy records are handled the
same way for replay only; no new transform or equivalence rule is introduced.

An exact module `timing` block is immutable public behavior. Profile bounds may
equal or contain that exact latency/II, but cannot weaken or contradict it.

<a id="reference-implementation-profiles-backend-plans"></a>
## Reading the backend selection report

`--backend-implementation-report PATH` writes a JSON explanation of what the
Direct-SV backend could actually emit for the requested physical
implementation. This is **not** another source-language plan or an additional
backend. Inspect it when a target/profile request does not produce the resource
you expected.

| Report status | Meaning |
| --- | --- |
| `selected` | Requested physical implementation was selected and emitted. |
| `generic_fallback` | `preferred` route was unavailable; generic RTL was emitted instead. |
| `unsupported` | Requested physical route is unsupported; `required` fails compilation. |
| `not_requested` | No physical implementation route was requested. |

Current boundaries are deliberate: one backend is selected per named profile,
II is limited to existing semantics, and profiles cannot introduce protocol,
CDC, memory, retiming, or formal capabilities that the typed compiler does not
already support.
