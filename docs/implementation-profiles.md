# Implementation profiles

Implementation profiles keep physical policy outside portable `.zl` source.
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

Schematic use inside a project containing the shown `src/fir.zl` and profile:

```bash
zlangc src/fir.zl --profile release --systemverilog build/Fir.sv
```

A profile may select pinned scalar external-module implementations with
`external-mappings = ["vendor-add"]`.  Mapping declarations live in the
top-level `external-mappings` tables of `zlang.toml`; their source files and
hashes are fixed by `zlang.lock`.  They affect direct-SV artifact text, never
the backend-independent model or implementation-policy identity.

Only the selected profile is parsed strictly. This permits a project to carry
profiles for newer compilers without making unrelated builds fail. Selecting
an unknown profile, selecting a profile without a project, or using an unknown
key is an error. Profile data is excluded from dependency resolution and lock
identity, but the normalized selected request participates in implementation
and later whole-build identities.

## Semantic regions

The compiler reports one stable identity for each public scalar wire-output
region. A profile can address an exact subset with `regions = ["DIGEST", ...]`.
Generate `--implementation-policy-report` once to obtain the 64-character
digests.

Region identity is derived from the logical module specialization, typed public
output binding, canonical expression identity, and exact result type. Source
paths, spans, physical instances, generated Haskell, and RTL names are excluded.
Stale and duplicate identities fail explicitly. The first slice is root-module
only and scalar-only; recursive/profile-selected protocol regions are deferred.

## Normalization and conflicts

`choice(auto)`, `architecture(auto)`, `pipeline(auto)`, and `explore` remain
accepted. Their frozen source policy is represented through the same
`ImplementationRequest` model as the selected profile and explicit compiler
options. Equal normalized contributions are harmless. Different values for
the same target, transform set, objective, constraint, evidence policy, formal
policy, or architecture are rejected with both origins in the diagnostic.

For an ordinary typed scalar output, profile transforms/constraints/objectives
run through the existing bounded M34 explorer. A legacy source auto/explore
region has already run during semantic analysis, so it is compared but never
run a second time. No new transform or equivalence rule is introduced.

An exact module `timing` block is immutable public behavior. Profile bounds may
equal or contain that exact latency/II, but cannot weaken or contradict it.

## Backend plans

`--backend-implementation-report` contains stable, independent `clash` and
`systemverilog` slots. Each is `selected`, `generic_fallback`, `unsupported`,
or `not_requested`. A preferred unsupported physical route may fall back to a
technology-independent graph. A required route fails compilation. A physical
direct-SystemVerilog graph is never relabelled as a Clash graph; Clash remains
the independent generic/reference route for those target-specific plans.

Current boundaries are deliberate: one backend is selected per named profile,
II is limited to existing semantics, and profiles cannot introduce protocol,
CDC, memory, retiming, or formal capabilities that the typed compiler does not
already support.
