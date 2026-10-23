<a id="reference-platform-constraint-publication"></a>
# Platform constraints


<a id="reference-platform-constraint-publication-decision"></a>
## Clock constraints

A selected project profile can declare one explicitly named clock period:

```toml
[profiles.release.platform.clocks.clk]
period-ns = 10.0
```

The clock name must match the selected top's clock declaration. The period is a
physical build setting and is not inferred from an `fmax` intent.

The CLI writes constraints only together with exactly one SystemVerilog output.
The following command assumes a project-local `top.zhl` and the `release`
profile shown above:

```text
zlang top.zhl --profile release --systemverilog build/Top.sv \
  --constraints-xdc build/Top.xdc --constraints-sdc build/Top.sdc
```

XDC and SDC initially contain only:

```tcl
create_clock -name clk -period 10 [get_ports {clk}]
```

The clock edge and reset mode must match the source declaration. A stale or
mismatched constraint request is rejected. XDC/SDC output is deterministic and
is written atomically without following symlinks.

No reset false path is emitted: asynchronous-reset recovery/removal timing must
not be hidden. This constraint publication supports only the exact single-clock
profile shown here. Multiple clock domains are supported in language semantics
and Direct-SV emission, but generated clocks, multi-clock XDC/SDC publication,
pin/package/IO properties, input/output delays, CDC constraints, and formal
properties are not emitted by this facility.
