# Platform clock-constraint publication

Status: bounded unnumbered slice implemented. This work does not create another
numbered milestone and does not change ZLang source semantics.

## Decision

The current synchronous `Memory` is already a one-read/one-write simple
dual-port resource: one read and one write may be accepted on the same edge.
A genuinely distinct same-domain true-dual-port memory would require two
symmetric read/write ports and a frozen same-address dual-write contract. No
current real-design validation requires more than one read or one write per
cycle, so that slice is rescheduled until a concrete design supplies evidence.
Byte masks, initialized writable memory, independent memory clocks and automatic
BRAM mapping remain separate concerns.

Physical clock publication is justified now because the existing QoR helpers
otherwise duplicate handwritten `create_clock` commands. A selected project
profile may therefore add exactly one explicitly named clock period:

```toml
[profiles.release.platform.clocks.clk]
period-ns = 10.0
```

The clock name must match the one typed `ClockDomain` on the selected top. The
period is physical build configuration: it is not semantic IR, is not inferred
from `fmax`, and is not merged into `ImplementationRequest`.

The CLI publishes a constraint only together with exactly one backend artifact.
The following commands are schematic and assume a project-local `top.zhl` plus
the `release` profile shown above:

```text
zlang top.zhl --profile release --systemverilog top.sv \
  --constraints-xdc top.xdc

zlang top.zhl --profile release -o Top.hs \
  --constraints-sdc top.sdc
```

The compiler resolves the RTL port through the artifact's typed `CLOCK` binding,
never through generated-name guessing. XDC and SDC initially contain only:

```tcl
create_clock -name clk -period 10 [get_ports {clk}]
```

Each version-2 constraint artifact retains its backend hash, selected-IR
identity, clock edge, and complete reset mode/polarity/release-cycle/power-up
metadata. For a non-default contract, publication also requires an exact
version-10 physical-domain record in the BackendArtifact; stale or mismatched
reset semantics are rejected. Whole-build manifests
publish the file as a backend companion and include the platform profile and
constraint identities. XDC/SDC output is deterministic and atomically published
without following symlinks.

No reset false path is emitted: asynchronous-reset recovery/removal timing must
not be hidden. Generated clocks, multiple clock domains, pin/package/IO
properties, input/output delays, CDC constraints and formal properties are
outside this bounded slice.
