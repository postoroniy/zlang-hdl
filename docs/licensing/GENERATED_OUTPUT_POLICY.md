# Generated output and included material

This document explains the existing [NOTICE](../../NOTICE) policy. It is not a
new license or an exception to a third-party license.

## User designs

Compiling user-authored ZLang source does not, by itself, impose Apache-2.0 on
the resulting HDL. ZLang does not automatically add a copyright or Apache SPDX
header to a user's generated design. A compiler provenance comment identifies
the tool; it does not determine the design's owner or license.

This is not a promise that every generated file is free of other obligations.
Input sources, elaborated libraries, copied helper implementations, ROM
contents and externally supplied models retain their own provenance. Review
the material actually included in the distribution, not just its extension or
the tool that produced it.

## Output categories

| Material | Current implementation | Distribution consideration |
|---|---|---|
| User design translated from typed IR | Direct-SystemVerilog emission | The compiler's license does not automatically license the user's design |
| Source-authored standard-library components | `stdlib/**/*.zhl`, elaborated into the design | Preserve the applicable input/library notices; translation is not evidence that all obligations disappear |
| Reusable compiler helper bodies | FIFO, reset, protocol, wrapper and simulation helper rendering | Distinguish generated design structure from implementation material carried into the output |
| ROM companion images | Exact bits computed from typed constants | Retain the provenance of the initializer, coefficients and imported data |
| External-tool-generated HDL or models | Separately supplied vendor models or synthesis products | Review the applicable tool component, template and model terms |
| Committed generated examples and test fixtures | Files distributed in this repository | Follow their recorded repository or adjacent license, rather than treating them as arbitrary user output |

Where a distribution includes Apache-licensed material or derivative material
subject to that license, comply with its redistribution requirements, including
the license copy, applicable notices and modification notices. This is a
conditional statement about included material, not a declaration that every
generated HDL file is a derivative of the compiler. See
[Apache-2.0, sections 1 and 4](https://www.apache.org/licenses/LICENSE-2.0).

## Helpers and physical resources

Direct-SV renders ordinary RTL for state, interfaces and CDC, and can instantiate
source-described physical resources. Instantiating a named primitive such as
`DSP48E1` is distinct from copying its vendor simulation library.

The direct-SV [target emitter](../../zlang/backend/systemverilog/target.py)
contains a small optional `DSP48E1` behavioral simulation model. It is repository
source under the existing REUSE metadata; it is not presented as an AMD UNISIM
distribution or a complete vendor model. The supplied simulation model and any
separately supplied vendor library need their own provenance review. A familiar
primitive name alone establishes neither copying nor permission to redistribute
a vendor implementation.

The existing [Verilator state-access C++ header generator](../../zlang/backend/systemverilog/simulation_state.py)
is also Community functionality. Future CSR C/C++ software or UVM helper
generators are separately classified, not implemented, and have no announced
generated-helper license. Do not extrapolate future terms to existing output.

## Practical redistribution checklist

1. Record the source/dependency identities and actual tool/component versions.
2. Inventory the files and implementation/data material included in the bundle.
3. Retain applicable LICENSE, NOTICE, attribution and modification information.
4. Review separately supplied libraries or models before copying them into the
   bundle; do not distribute an entire tool installation as a shortcut.
5. Check source maps, logs, traces and ROM images for confidential design data.
   A verification bundle can contain a design even when it is not called RTL.
6. If provenance or applicable terms remain unclear, resolve that question with
   the relevant rights holder or a qualified adviser before distributing the
   affected material. Do not invent a license label or strip existing notices.

See [IP classification](IP_CLASSIFICATION.md) for the repository's reference
designs and fixtures, and [RELEASING.md](../../RELEASING.md) for release checks.
