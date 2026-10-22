# ZLang HDL

ZLang HDL is an experimental hardware description language and compiler focused on
strong types, explicit cycle semantics, reusable protocols, deterministic
artifacts, and verification-aware implementation selection.

> **Alpha software:** this release is intended for evaluation and
> real-design feedback. Source syntax, the provisional Python API, and
> non-versioned tooling may change before 1.0. Unsupported combinations fail
> closed rather than publishing guessed RTL.

The initial supported development and release environment is Linux x86-64 with
CPython `>=3.12,<3.13`. You do not need that interpreter preinstalled: the
recommended `uv` workflow can provision it for the project. Direct
SystemVerilog is the sole supported production RTL backend;
external synthesis and formal tools are optional unless their corresponding
flow is requested. The retired Clash backend is not installed, discovered, or
executed by the compiler or its regression suite.

## Quick start

For a release wheel, editor setup, and the optional Verilator/Yosys/SBY/Z3
toolchain, use the
[installation chapter](docs/language-reference.md#reference-installing-toolchain).

Clone the repository and create the checkout-local development environment:

```bash
git clone https://github.com/postoroniy/zlang-hdl.git
cd zlang-hdl
make venv
source .venv/bin/activate
```

Every checkout owns its own `.venv`; the Make gates reject an ambient or sibling
environment. `uv` is used when available to provision CPython 3.12, otherwise
`make venv` validates a local Python 3.12 interpreter before creating the
environment. The target installs the compiler from the current checkout and the
matching reviewed native-runtime wheel into that same environment. After this
one command, checking, RTL generation, LSP and native simulation are available;
there is no separate simulator installation step on supported Linux x86-64.
On a local host with the `code` command and Node.js 22 available, `make venv`
also builds, audits and installs this checkout's VS Code extension. CI skips
that host installation. Use `make venv INSTALL_EDITOR=0` to leave your installed
extension unchanged. If Node.js 22 or npm is unavailable, Python/LSP/native
installation still completes; install those editor prerequisites and run
`make editor-install` afterward.
Initial provisioning needs access to the Python package index and, for editor
packaging, the npm registry, or suitable prepopulated local caches.

`make -n venv` only prints the bootstrap command; it does not provision an
environment, install packages, or update VS Code. A real bootstrap rejects an
active sibling environment, an environment outside this checkout, or an
existing non-CPython-3.12 interpreter before installing packages. For a foreign
active environment, run `deactivate`, then retry `make venv`. An incompatible
local `.venv` must be moved aside before retrying; it is not deleted automatically.
Stale checkout distribution metadata is preserved below `build/tmp` rather
than allowed to shadow the installed version. If the optional VS Code install
fails after Python setup, the error identifies that stage: retry
`make editor-install`, or use `make venv INSTALL_EDITOR=0` for Python/native/LSP
setup only. `KEEP_TMP=1 make venv` retains and prints bootstrap scratch paths
for troubleshooting.

### VS Code and the language server

`make venv` installs the `zlang-lsp` executable into this checkout whether or
not VS Code is already installed. Verify it before configuring the editor:

```bash
.venv/bin/zlang-lsp --version
```

The VSIX contains the VS Code client, syntax support and snippets; it does not
embed the Python compiler or language-server executable. VS Code must be pointed
at the `zlang-lsp` installed by the release wheel or this checkout's `.venv`.

To refresh the editor without reinstalling the Python environment, use Node.js
22 and run:

```bash
make editor-install
```

This always creates a fresh VSIX below `build/tmp` and installs it with
`--force`, including when the extension version is unchanged. On Linux x86-64
it includes the reviewed debugger and waveform binaries; other hosts receive
the editor/LSP-only package. Set `VSCODE=/path/to/code` if the CLI has another
name or location. Then run **Developer: Reload Window** and close/reopen existing
waveform tabs: an open webview keeps its old JavaScript until reloaded.

After a version is published, the matching GitHub Release provides the same
Linux-x64 VSIX as a downloadable asset. Do not substitute a VSIX from another
ZLang version.

This repository's tracked workspace settings point the extension at:

```text
${workspaceFolder}/.venv/bin/zlang-lsp
```

For another ZLang workspace, set **ZLang HDL: LSP Path** to that project's
environment, or add this property to its existing `.vscode/settings.json`
without replacing other settings:

```json
{
  "zlang.lsp.path": "${workspaceFolder}/.venv/bin/zlang-lsp"
}
```

Reload the VS Code window and open a `.zhl` file. Diagnostics, hover, completion,
F12 definition, Shift+F12 references and rename are provided by the compiler-owned
language server. Startup failures are reported in **View: Output → ZLang HDL**.
The extension requires VS Code 1.141 or newer.

To build the extension from the clone instead of downloading the reviewed VSIX,
install Node.js 22 and follow the
[local extension build instructions](editors/vscode/zlang-hdl/README.md#build-and-install-locally).

For an ordinary published release, download both matching wheel assets and
install them together in an isolated environment:

```bash
uv tool install --python '>=3.12,<3.13' \
  --with /path/to/zlang_native_sim-VERSION-cp312-abi3-PLATFORM.whl \
  /path/to/zlang_hdl-VERSION-py3-none-any.whl
```

The native wheel must match the operating system and CPU; WSL2 uses the Linux
x86-64 wheel. A compiler-only installation may omit `--with`, but `zlang sim`
then fails explicitly because native simulation has no fallback executor. The
Rust/Cranelift source is not distributed in this Community repository; the
versioned plan/ABI remains compiler-owned and does not alter language or RTL
semantics. See the
[native simulation section](docs/language-reference.md#reference-native-simulation)
and the installation chapter for PATH, pip/venv and WSL2 details.

To verify the same two-wheel installation directly from a clean source clone,
build the reproducible package set and install only its two `.whl` files into a
fresh environment:

```bash
mkdir -p build/tmp
package_root="$(mktemp -d "$PWD/build/tmp/package.XXXXXX")"
make package \
  BUILD_ROOT="$package_root/build" \
  DIST_DIR="$package_root/dist"
python3 -m venv "$package_root/install"
"$package_root/install/bin/python" -m pip install \
  "$package_root/dist"/*.whl
"$package_root/install/bin/zlang" --version
"$package_root/install/bin/zlang-lsp" --version
"$package_root/install/bin/zlang" sim examples/add.zhl --top Add \
  --set a=200 --set b=55 --json
```

Check a design without emitting RTL:

```bash
.venv/bin/zlang examples/add.zhl --check --verbose
```

Emit direct SystemVerilog:

```bash
mkdir -p build
.venv/bin/zlang examples/add.zhl --systemverilog build/Add.sv --verbose
```

### Projects and lock files

Without a `zlang.toml`, standalone sources (including `std.*` imports) need no
project lock. When a manifest is present, compilation and LSP require its
`zlang.lock`, even for a project with zero dependencies. They validate it
read-only: neither fetches dependencies nor creates or rewrites the lock.

For a cloned project, restore its checked-in lock and exact dependency sources;
Git dependencies also need their local cache. `make venv` installs tools, not
project locks. For a **new** project without a lock, create the initial one:

```bash
.venv/bin/zlang lock update --project path/to/zlang.toml
```

After that, run `zlang lock update` only when dependency declarations, dependency
source contents, exact Git revisions or other resolution inputs change. A
missing/stale lock is an environment error, not a source error at line 1.
The VS Code client watches manifest/lock creation, changes and deletion and
rechecks open ZLang documents; no editor restart or dummy source edit is needed.

### Native simulation

Native simulation requires the matching `zlang-native-sim` a24 wheel. The
runtime is not an optional accelerator: it is the sole executable owner of
both ordinary two-state and opt-in `0`/`1`/`U`/`X` behavior. If the wheel is
missing or its ABI does not match the compiler, `zlang sim` fails explicitly;
it never falls back to a Python interpreter or silently invokes an RTL tool.

For a combinational top, assign inputs with repeatable `--set` options and ask
for JSON output:

```bash
.venv/bin/zlang sim examples/add.zhl --top Add \
  --set a=200 --set b=55 --json
```

For a single-clock sequential top, `--cycles` applies the named clock once per
event. Resettable designs should normally use the explicit JSONL event form
shown below so reset assertion and release are part of the reproducible input:

```bash
.venv/bin/zlang sim examples/counter.zhl \
  --top Counter --clock clk --cycles 100 --json
```

Each non-empty line in an event file is one atomic simulation event. `set`
changes binary inputs, `set_logic` supplies packed four-state input spelling,
`reset` changes physical reset pins, and `edges` lists all coincident clock
edges. `repeat` reuses the event lazily and is bounded by the one-million-event
run limit:

```bash
mkdir -p build
printf '%s\n' \
  '{"reset":{"rst":true},"edges":["clk"]}' \
  '{"reset":{"rst":false},"edges":["clk"],"repeat":9}' \
  > build/counter-events.jsonl
```

Run the schedule and record a selective FST waveform (clock/reset signals are
always included). FST is preferred for interactive native traces; `.vcd`
remains supported for external/formal-tool compatibility:

```bash
.venv/bin/zlang sim examples/counter.zhl --top Counter \
  --events build/counter-events.jsonl \
  --trace build/counter.fst --trace-signal y --json
```

Ordinary simulation starts uninitialized state from deterministic binary zero.
Use `--logic-state` when the distinction between never-written `U` and computed
unknown `X` matters. Use repeatable `--initial-reg NAME=01ux` overrides for a
specific initial state and `--set-logic NAME=01ux` for packed input values.
`--strict-uninitialized` implies four-state execution and turns an observed
`U` or `X` into a runtime failure. Standard FST/VCD spell both unresolved
states as `x`; ZLang adds hidden `__zlang_meta/*_u_mask` companions so its
waveform query service and viewer retain U provenance.

Simulation observes the selected implementation. Legal binary-equivalent
optimization may change U/X propagation; strict mode checks typed observations
and stateful controls, not every source operand. It does not reject a value
whose uncertainty has been resolved by an exact controlling value or cancellation.

For differential checking, the same event schedule can be replayed against
generated SystemVerilog in Icarus or Verilator. The comparison fails closed on
an output mismatch, missing sample, unresolved RTL value, tool error, or
timeout. Supply a new empty artifact directory only when the generated RTL,
testbench and logs should be retained:

```bash
mkdir -p build/tmp
compare_dir="$(mktemp -d "$PWD/build/tmp/add-compare.XXXXXX")"
.venv/bin/zlang sim examples/add.zhl --top Add \
  --set a=200 --set b=55 --compare-with verilator \
  --compare-artifacts "$compare_dir" --json
```

Multi-clock designs must use `--events`; clocks listed together in one `edges`
array commit atomically from the same pre-edge snapshot. See the
[native simulation chapter](docs/language-reference.md#reference-native-simulation)
for the Python API, reset semantics, four-state behavior, limits, tracing, and
differential-validation details. `zlang sim --help` is the exact CLI option
reference.

### Debug native simulation in VS Code

The Linux-x64 VSIX includes a native debugger for the same simulation engine
used by `zlang sim`. It does not generate test vectors. Give it either the same
JSONL event schedule used by the CLI, or a simple bounded `clock` and `cycles`
launch. Project-owned reusable schedules normally live under `simulation/`;
temporary or generated experiments belong under ignored `build/` output.

The clone includes a ready-to-run
[Debug RuleAction configuration](.vscode/launch.json) and
[event schedule](examples/rule_action.events.jsonl). No FST generation or
handwritten launch configuration is needed for this example.

#### Start the debugger

After `make venv`, open the repository **folder** in VS Code. To refresh an
already installed extension, run:

```bash
make editor-install
code .
```

Run **Developer: Reload Window** after installing. The workspace uses its own
`.venv/bin/zlang-lsp`; the Linux-x64 VSIX supplies the native debug adapter.

1. Open [`examples/rule_action.zhl`](examples/rule_action.zhl) and **Save All**.
2. Open **Run and Debug** (`Ctrl+Shift+D`) and select **Debug RuleAction**.
3. Press **F5**. `stopOnEntry` pauses before the first event.
4. Run **Ctrl+Shift+P → ZLang HDL: Open Live Waveform** and place the panel
   beside the source. Keep **Follow Live** enabled to follow the current event.
5. Press **F10 / Step Over** to execute one complete event at a time.

To stop when the rule fires, click the gutter to the left of line 10
(`when enable`) or put the caret on that line and press **F9**. Press
**F5 / Continue** after the entry stop: execution stops after the first enabled
event with `count = 1`; Continue again stops with `count = 2`. Breakpoints are
supported on rules, FSM transitions and verification clauses, not arbitrary
declarations. No `debug.allowBreakpointsEverywhere` setting is needed.

The checked-in configuration is:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "type": "zlang-hdl",
      "request": "launch",
      "name": "Debug RuleAction",
      "program": "${workspaceFolder}/examples/rule_action.zhl",
      "top": "RuleAction",
      "events": "${workspaceFolder}/examples/rule_action.events.jsonl",
      "logicState": true,
      "strictUninitialized": false,
      "initialRegisters": {},
      "traceSignals": ["enable", "fired", "count"],
      "stopOnEntry": true
    }
  ]
}
```

#### Test vectors and stepping

The debugger reads these checked-in events; it does not generate stimulus:

```jsonl
{"reset":{"rst":true},"set":{"enable":0},"edges":["clk"]}
{"reset":{"rst":false},"edges":["clk"]}
{"set":{"enable":1},"edges":["clk"],"repeat":2}
{"set":{"enable":0},"edges":["clk"]}
```

Inputs and resets retain their values until changed. `repeat: 2` executes that
event twice, so four JSONL lines describe five debugger steps:

| After F10 | Event | `count` |
|---|---|---:|
| 1 | Assert reset | 0 |
| 2 | Release reset | 0 |
| 3 | `enable = 1` | 1 |
| 4 | Repeat the enabled clock event | 2 |
| 5 | `enable = 0`; hold state | 2 |

This example declares `count` with `= 0`, so it also starts at zero before the
first event. Uninitialized registers without a reset value show `U` in this
four-state launch. One step executes an entire atomic simulation event, not one
source line. **F5 / Continue** runs until a
breakpoint, failure or the end of the schedule. Place a breakpoint on
`when enable` to stop after a firing rule; only executable rules, FSM transitions
and verification clauses accept source breakpoints. **Restart** replays the
schedule from the beginning; **Shift+F5** stops the session. Increase `repeat`
in the event file and relaunch for a longer run.

The same schedule can be checked without the editor:

```bash
.venv/bin/zlang sim examples/rule_action.zhl --top RuleAction \
  --events examples/rule_action.events.jsonl --logic-state --json
```

#### Debug without a JSONL file

For a short interactive experiment, add this entry to the `configurations`
array in `.vscode/launch.json`, without an `events` field:

```json
{
  "type": "zlang-hdl",
  "request": "launch",
  "name": "RuleAction - manual inputs",
  "program": "${workspaceFolder}/examples/rule_action.zhl",
  "top": "RuleAction",
  "clock": "clk",
  "cycles": 20,
  "logicState": true,
  "stopOnEntry": true,
  "traceSignals": ["enable", "fired", "count"]
}
```

Save the source, select **RuleAction - manual inputs**, and press **F5**.
While stopped, open **Variables → Ports**, right-click `enable`, choose
**Set Value**, and enter `1`. Two **F10** steps produce `count = 1`, then `2`
under **Registers**. Set `enable` to `0` and step again: `count` holds at `2`.
To test a source breakpoint, set `enable` back to `1`, press **F9** on
`when enable` or its `count <- ...` action, and **F5 / Continue**: the next
enabled event stops with `count = 3` and highlights that line. Open
**ZLang HDL: Open Live Waveform** to watch these same events.

This uses the register's declared `= 0` initial value, not a reset sequence.
Clocks and resets are read-only in Variables; use JSONL for explicit reset,
multiple clocks or repeatable input changes. `events` and `clock`/`cycles` are
mutually exclusive. The run is bounded to 20 clock events. Restart restores
initial state; it does not replay manual edits.

Python can also supply inputs directly through `zlang.sim`, without JSONL.
That creates a separate native instance: Python breakpoints work in its
testbench, but ZLang source breakpoints and the live debugger waveform are not
attached to it. See the [runnable Python example](docs/native-debugging.md#python-simulation-without-an-event-file)
and [manual-input walkthrough](docs/native-debugging.md#debug-without-an-event-file).

#### Observe and add waveform signals

`enable`, `fired` and `count` are recorded from event zero by `traceSignals`.
While paused, expand **Variables → Ports** or **Registers**, right-click a
value or field and choose **Add to Waveform**. The command also works in
**Watch** for an exact signal path. Structs/vectors expose child fields,
enums show their member names, tagged unions distinguish active and inactive
fields, and interfaces appear as channel groups. **Type** preserves these
typed displays; Hex/Binary show the packed bits.

A signal added during execution starts recording at the next event; earlier
history is not recorded. Use **Restart** for complete history. A saved `.fst`
or `.vcd` tab is independent of the debugger: use **Open Live Waveform** to
watch steps as they execute.

- Select a signal row, then use **Change ◀/▶** or **Left/Right** to inspect
  its transitions. Moving a waveform cursor does not execute the simulator.
- Use the mouse wheel to zoom, **Shift+wheel** to pan in time, or middle-button
  drag to pan horizontally and scroll signals vertically.
- Keep **Follow Live** on to move cursor A and its value column to the latest
  debugger event. Clocks and resets are traced automatically with their
  declared edge and polarity, and appear under **Variables → Clocks and resets**.
  Native trace ticks visualize events, not physical timing.

Live and saved waveform views share grouping, duplicate rows, Cut/Copy/Paste,
colors, row height, search, cursors, bookmarks, measurements and split viewports.
**Save Session / Load Session** preserve the layout; live sessions save a layout
template, not simulator state. **Refresh** rereads live history without stepping.
**Add signals** selects more live traces; **Export Trace** saves FST/VCD for
later inspection. Closing the live panel or restarting does not discard its
workspace arrangement.

See [Debugging native simulation in VS Code](docs/native-debugging.md) for
all launch fields, four-state inputs, data breakpoints, project profiles and
troubleshooting.

With Verilator installed, a strict lint smoke is:

```bash
verilator --lint-only -Wall build/Add.sv
```

A minimal ZLang module is deliberately small:

```zlang
module Add {
    in a, b : u8
    out y : u9

    y = a + b
}
```

Unsigned and signed addition preserve carry: `u8 + u8` is `u9`, and
`s8 + s8` is `s9`. ZLang does not silently resize, rescale fixed point, cross
clock domains, or adapt protocols.

## What is implemented

The validated language includes:

- exact-width integer, bit-vector, fixed-point, vector, struct, tuple, character,
  and fixed-string types;
- pure functions, generics, compile-time generation, reductions, ROM images, and
  deterministic specialization;
- [compile-time integer `%`](docs/compile-time-integers.md) for generic value
  arguments, static indices and compile-time `if`, with floor-modulo semantics;
  runtime remainder is unsupported;
- per-domain registers, rules, FSMs, fixed pipelines, FIFOs, synchronous
  memories, hierarchy, and instance arrays inside single- or multi-clock
  modules, including explicit stored `out reg` ports and transient rule/FSM
  `drive` actions;
- ready/valid, credit, request/response, aggregate protocols, explicit buffering,
  arbitration, and mandatory explicit named-domain CDC;
- source-authored RegBus, AHB-Lite, AXI4-Lite, APB, Wishbone, AXI-Stream, CSR,
  math, stream, storage, coding, and target-library components;
- direct-SystemVerilog emission with source maps and versioned BackendArtifact
  manifests;
- bounded equality saturation, structurally diverse site-local `implement`
  exploration with deterministic limits/session caching, synthesis evidence,
  safety checks, semantic-reference equivalence, formal-aware selection, and
  source-level verification goals.
- range-proven runtime packed indexing and fixed-width packed slices; and one
  deliberately narrow ready/valid temporal-sharing candidate for the integer
  kernel `a*b + c*d` (one shared multiplier, capacity one, latency four,
  II four). Its transaction-stream route supports bounded `required_bmc`
  evidence but not unbounded `required_proven`; it is not general HLS scheduling.

The executable [language tour](examples/all_syntax.zhl) is representative, not a
complete support contract. Use the
[language support matrix](docs/language-reference.md#reference-syntax-support-matrix)
and [known limitations](docs/language-reference.md#reference-known-limitations)
for the current, bounded surface.
The [example catalog](examples/README.md) explains why small feature witnesses
remain standalone, identifies the few real multi-file projects, and records the
preferred concise source forms used by new examples.
Qwen users can also rely on the tracked
[ZLang HDL project skill](.qwen/skills/zlang-hdl/SKILL.md), which routes work to
the same current guides and executable compiler contracts.
Other coding assistants should start with the vendor-neutral
[AI guide](AI_GUIDE.md); it shares the same sources of truth instead of
maintaining model-specific copies of the language rules.

Representative real-design validations include:

- hierarchical DMA and multi-outstanding request/response;
- source-authored AHB-Lite/AXI4-Lite/APB/Wishbone to RegBus/CSR paths;
- fixed-point FIR architectures and target-aware DSP mappings;
- a nine-stage FFT512 SDF reference;
- an attributed, bounded IEEE 802.11a-derived transmitter path covering framing,
  coding, interleaving, mapping, IFFT64, reorder, and cyclic prefix.

These are validation witnesses, not a claim that every possible feature
combination is supported or that measured FPGA timing is guaranteed.

## Backends and verification

| Flow | Status |
| --- | --- |
| Direct SystemVerilog | Sole supported production backend; fail-closed outside its validated subset |
| Verilator | Optional lint and behavioral RTL validation |
| Yosys/SymbiYosys/Z3 | Optional bounded/proven safety and equivalence execution |

The candidate external-tool pin is OSS CAD Suite 2026-10-21: Verilator 5.053,
Yosys 0.70, SymbiYosys 0.70, and Icarus Verilog/VVP 14.0, with Z3 5.3.1
from the repository virtual environment ahead of the suite's bundled solver. These are
evidence versions, not compatibility bounds; see the
[installation chapter](docs/language-reference.md#reference-installing-toolchain-verify-the-installation) and
machine-readable [`release/status.json`](release/status.json).

Emit direct SystemVerilog:

```bash
.venv/bin/zlang examples/add.zhl --systemverilog build/Add.sv
```

Create and replay an immutable verification bundle:

```bash
.venv/bin/zlang examples/contracted_add.zhl \
  --top ContractedAdd \
  --verification-bundle build/verify

.venv/bin/zlang verify build/verify \
  --mode bmc \
  --depth 20 \
  --format json \
  --report build/verification-report.json
```

`zlang` is the unified command surface: use `zlang sim`, `zlang verify`,
`zlang lock`, and `zlang lsp`. The installed `zlang-verify`, `zlang-lock`, and
`zlang-lsp` executables remain compatibility aliases for scripts and editor
configuration.

Bounded model checking is reported as `bounded_pass`, never promoted to
`proven`. Missing tools, bindings, reset semantics, or unsupported routes remain
explicit `unknown`/`skipped` results according to the requested policy.

Try the [runnable formal examples](examples/verification/README.md): prove a
counter invariant, find a rare 64-bit-triggered overflow, check scoped contracts,
and verify ready/valid stalls. The intentionally broken example passes shallow
BMC but fails at a deeper bound, with a source-attributed counterexample.
The [formal verification guide](docs/formal-verification.md) explains policies,
result statuses, immutable replay, and how typed source obligations are bound
to generated Direct-SV formal harnesses without guessing RTL names.

## Documentation

- [Complete language reference](docs/language-reference.md)
- [Concise language quick reference](docs/language-quick-reference.md)
- [Compile-time integer modulo and PSS sequence example](docs/compile-time-integers.md)
- [Printable PDF reference](docs/ZLang-HDL-Language-Reference.pdf)
- [Formal verification workflow and evidence](docs/formal-verification.md)
- [Stream composition, CRC and parallel FIR](docs/stdlib-stream-dsp.md)
- [Reloadable and one-MAC folded FIR](docs/stdlib-reloadable-fir.md)
- [CORDIC, NCO, multirate filters and coding](docs/stdlib-multirate-coding.md)
- [Extended-Hamming SECDED](docs/stdlib-secded.md)
- [Farrow fractional-delay filters](docs/stdlib-farrow.md)
- [DVB RS(204,188) encoding and decoding](docs/stdlib-rs-dvb.md)
- [Framed hard/soft K=7 Viterbi](docs/stdlib-viterbi.md)
- [Standard-library formal coverage](docs/stdlib-formal-coverage.md)
- [Non-publishing candidate and signed release process](docs/release-process.md)

The compiler-owned capability registry and release CI are authoritative for
executable support. Documentation should describe semantics and boundaries
without copying mutable pass totals into multiple files.

The a24 candidate includes the stream/DSP components, SECDED, Farrow,
DVB Reed–Solomon and Viterbi described in the Markdown guides and rebuilt
PDF. Candidate compiler packages and both VSIX variants are built from the
reviewed public source tree; the native runtime remains ABI v13. Local
candidate preparation does not publish a release or create a tag.
Editing or pulling the grammar does not update an installed VSIX. Run
`make editor-install` and reload VS Code to use the current highlighting.

## Development

Run focused tests serially while debugging. Run the complete suite in parallel:

```bash
.venv/bin/python -m pytest -q tests/test_sim_cli.py
.venv/bin/python -m pytest -n 16 --dist=loadscope -q
.venv/bin/python -m compileall -q zlang tests
git diff --check
```

External-tool tests discover tools from explicit CLI options or `PATH`.
Dedicated release jobs require pinned Verilator, Yosys, SymbiYosys,
yosys-smtbmc, and Z3 versions and reject unexpected skips. GHC and Clash are
not installation, development, CI, or release dependencies.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the DCO, test expectations, and
third-party provenance requirements. Community support is described in
[SUPPORT.md](SUPPORT.md), and vulnerabilities must be reported privately as
described in [SECURITY.md](SECURITY.md).

## Support ZLang HDL

If ZLang HDL is useful to you, you can voluntarily support its continued development:

☕ [Buy Me a Coffee](https://buymeacoffee.com/zlanghdl)

## License and attribution

ZLang HDL is licensed under the [Apache License 2.0](LICENSE).
Copyright 2026 Viacheslav Vinogradov.

The Community repository contains the complete compiler, standard library,
local verification flows, examples, tests, and editor integration documented
here. The machine-readable [`release/status.json`](release/status.json)
identifies the current release candidate. The frozen
[Community baseline](docs/licensing/COMMUNITY_BASELINE.md) records the minimum
public product boundary rather than the current version. See also the
[name and branding policy](TRADEMARKS.md).

The [licensing guide](docs/licensing/README.md) distinguishes compiler licensing,
reference-design provenance and obligations for included generated material.

The 802.11a validation project preserves the MIT attribution for Nirav Dave's
Bluespec reference implementation. The original material and ZLang port and
modifications in that project subtree are distributed under its MIT license.
See the root [NOTICE](NOTICE) and the nested
[Wi-Fi license](examples/projects/80211a_transmitter/LICENSE) and
[notice](examples/projects/80211a_transmitter/NOTICE).

Compiling user-authored ZLang source does not by itself impose Apache-2.0 on the
resulting HDL. Users remain responsible for input code, copied standard-library
material, third-party content, and other material included in generated output.
Generated fixtures committed to this repository remain part of the licensed
repository distribution unless an adjacent notice states otherwise.
