# Examples, reference designs and IP

This inventory classifies the role of current repository material. It records
existing license metadata and provenance statements; it does not independently
establish ownership, patent clearance or the right to relicense contributions.
Community membership does not turn every file into Apache-2.0 material.

## Current source inventory

| Material | Role | Recorded license / treatment |
|---|---|---|
| Small `examples/*.zhl` designs and `all_syntax.zhl` | Language examples and runnable feature witnesses | Apache-2.0 through repository REUSE metadata |
| FFT/FIR, DMA and streaming packet examples | Open reference designs validating numerical, state and hierarchy behavior | Apache-2.0 through repository REUSE metadata; not automatically commercial IP because they are substantial |
| `stdlib` bus, math, coding, stream, storage and target descriptions | Compiler-shipped source libraries | Apache-2.0 through repository REUSE metadata; all current libraries remain Community |
| `examples/projects/80211a_transmitter` | Bounded Wi-Fi reference implementation | MIT, with its own LICENSE and NOTICE |
| Wi-Fi tests, oracles, validation documents and tooling outside that project | Independent validation material according to root NOTICE | Existing REUSE metadata; copied expressive material must instead retain its applicable upstream terms |
| ZTPU-related memory, AXI and first-fault witnesses | Minimal compiler/composition regression designs | Apache-2.0 through repository REUSE metadata; no license claim over the external ZTPU project |
| Committed generated HDL/Clash examples | Reproducible output examples and regression evidence | Existing repository/adjacent metadata; generation alone does not remove attribution obligations |
| External vendor models and complete external projects | Not supplied by the Python compiler distribution | Separate source and redistribution review before inclusion |

The [license index](README.md), [REUSE.toml](../../REUSE.toml) and adjacent
notices are the starting point for determining a specific file's license.

## Wi-Fi provenance

The [Wi-Fi project NOTICE](../../examples/projects/80211a_transmitter/NOTICE)
records the Bluespec reference at
`freecores/bluespec-80211atransmitter`, revision
`d654bfd4c2ffabc61437c131770beff58dc55b04`, with copyright 2006 Nirav Dave.
It also records the 2026 ZLang port and modifications. The original material
and port in that subtree are distributed under its
[MIT license](../../examples/projects/80211a_transmitter/LICENSE).

The project's source follows its documented IEEE-oriented behavioral contract
where the historical reference differs. That engineering decision does not
remove the historical attribution. Nor is the reference design a certification
of complete IEEE 802.11 compliance, radio suitability or patent clearance.

Root [NOTICE](../../NOTICE) separately identifies the tests, documents and
tooling outside the MIT subtree as independently authored validation material.
Retain that distinction. New copied algorithms, source text, vectors or data
require a provenance review rather than an assumption that every nearby file
has the same license.

## ZTPU regression boundary

The included runnable witnesses are:

```text
examples/ztpu_async_memory.zhl
examples/ztpu_banked_memory.zhl
examples/ztpu_axi_burst.zhl
tests/fixtures/ztpu_first_fault_nested_when.zhl
```

They exercise preserved-memory reset behavior, banked/replicated storage,
source-authored AXI hierarchy and atomic first-fault state updates. Related
tests use bounded traces and compiler invariants. They are not a complete TPU
implementation, a redistribution of the full external project, or an assertion
of ownership over that project.

The recorded Apache metadata is the current distribution statement. A bug
report, handoff, project name or locally available source is not by itself
permission to copy external implementation material. Maintainers must confirm
the provenance and submission rights of any added reproducer, oracle or data;
minimal size and useful test coverage do not replace that check.

## Target libraries and embedded models

Target descriptions identify hardware capabilities and primitive interfaces.
They are not a bundle of vendor IP libraries. In particular, a primitive
instantiation and the optional repository-provided behavioral simulation model
are different artifacts. See the
[generated-output policy](GENERATED_OUTPUT_POLICY.md) for the DSP48E1 model and
Clash template-output review boundaries.

Names such as IEEE 802.11, Arm AMBA, AMD/Xilinx and Intel are used descriptively.
The project does not claim affiliation, certification or rights over those
marks; see [NOTICE](../../NOTICE) and [TRADEMARKS.md](../../TRADEMARKS.md).

## Future additions and release review

Keep existing reference designs under their recorded licenses. Do not move
previously licensed material into an exclusive commercial category merely
because it has potential commercial value. The
[Community Baseline](COMMUNITY_BASELINE.md) retains current functionality;
future Enterprise features and independently supplied IP are separately scoped.

Before introducing third-party or commercial material, record its source,
revision, applicable license, copyright/attribution, modifications, intended
distribution and evidence of permission. Check coefficients, ROM images,
fixtures, copied helper bodies and verification bundles as well as main HDL
files. Unresolved ownership or confidential-input questions require human
review; a green compiler test, DCO line, REUSE check or SBOM is not an independent
legal clearance.
