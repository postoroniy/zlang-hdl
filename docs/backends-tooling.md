<a id="reference-generate-systemverilog"></a>
# Generate SystemVerilog

Write the selected top to an explicit file:

```sh
mkdir -p build
zlang design.zhl --top Top --systemverilog build/Top.sv
```

The command writes only the requested artifact and keeps stdout empty.
Diagnostics and `--verbose` progress messages go to stderr. A bare
`zlang design.zhl --top Top` invocation writes SystemVerilog to stdout, which is
convenient for a pipe but less suitable for a reproducible project script.

Use `--check` when no RTL should be created:

```sh
zlang design.zhl --top Top --check
```

Lint the generated module with Verilator:

```sh
verilator --lint-only --top-module Top build/Top.sv
```

Project profiles can select a target and clock constraint:

```sh
zlang top.zhl --profile release \
  --systemverilog build/Top.sv \
  --constraints-xdc build/Top.xdc \
  --constraints-sdc build/Top.sdc
```

Generated RTL preserves declared widths, signedness, reset modes, hierarchy,
and protocol behavior. Unsupported combinations stop with a diagnostic rather
than emitting a partial module. Public port names are stable; private helper
names are not a source-language interface and should not be referenced by user
code.

Use `--systemverilog` for file output or the bare command for stdout.
