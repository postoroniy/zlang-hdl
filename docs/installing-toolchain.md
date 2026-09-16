# Installing ZLang HDL and the external EDA toolchain

ZLang HDL supports Linux x86-64 with CPython `>=3.12,<3.13` for the current
alpha release. You do not need to find a distribution package for that exact
runtime: the recommended `uv` workflow can download and manage it independently
of the system Python. Parsing, semantic checking and direct-SystemVerilog
generation need only the Python package. Verilator, Yosys, SymbiYosys and a
solver are external programs used only when their corresponding lint,
synthesis or formal flow is requested.

The exact versions used by release acceptance are recorded in
[`release/status.json`](../release/status.json). Other versions may work, but
they are not evidence for the published release.

## Recommended installation with uv

Install [`uv`](https://docs.astral.sh/uv/getting-started/installation/) using
its official platform instructions. Confirm that the executable is visible in
the same terminal that will run ZLang:

```sh
uv --version
```

For ordinary use, install the wheel downloaded from the matching GitHub release
as an isolated command-line tool. `uv` provisions the requested interpreter if
it is not already present:

```sh
uv python install 3.12
uv tool install --python 3.12 /path/to/zlang_hdl-VERSION-py3-none-any.whl
zlang --version
zlang-lock --version
zlang-verify --version
```

If `uv` reports that its tool directory is not on `PATH`, run `uv tool
update-shell`, then start a new shell. `uv tool dir --bin` prints the command
directory for manual PATH configuration. To replace an installation made from
a downloaded wheel, repeat `uv tool install --force ...` with the new wheel.

For development from a Git checkout, keep the environment local to the
repository:

```sh
git clone https://github.com/postoroniy/zlang-hdl.git
cd zlang-hdl
uv python install 3.12
uv venv --python 3.12
uv pip install -e '.[test]'
```

The remainder of this guide uses `.venv/bin/zlang` so commands work in an
editable checkout. With a `uv tool` installation, omit the `.venv/bin/` prefix.

## Conventional venv and pip alternative

If a compatible Python is already available, use whatever executable name the
host provides; the example deliberately avoids assuming a version-suffixed
command:

```sh
python3 -c 'import sys; assert (3, 12) <= sys.version_info < (3, 13), sys.version'
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install /path/to/zlang_hdl-VERSION-py3-none-any.whl
```

For an editable development checkout, replace the final line with:

```sh
.venv/bin/python -m pip install -e '.[test]'
```

This fallback needs a compatible interpreter and `venv` support from the host.
Do not replace or upgrade the operating system's own Python merely to install
ZLang; use `uv` or an isolated virtual environment.

## Windows through WSL2

The current release is tested as a Linux application, not as a native Windows
Python package. On Windows 10/11, install WSL2 with an Ubuntu distribution,
open the Ubuntu shell, and perform the Linux installation entirely inside it.
For example:

```sh
sudo apt-get update
sudo apt-get install -y curl git build-essential
```

Then install `uv` using its official Linux instructions and follow the `uv`
steps above. Keep active projects under the WSL Linux filesystem, for example
`~/src/zlang-hdl`, rather than `/mnt/c/...`; this generally gives much better
compiler, Git, pytest and EDA-tool filesystem performance. Start VS Code through
its WSL extension so the editor, `zlang-lsp`, Python environment and EDA
executables all see the same Linux paths.

Inside a development checkout, these forms are equivalent:

```sh
.venv/bin/zlang examples/add.zhl --check
uv run --no-sync zlang examples/add.zhl --check
```

`uv run --no-sync` uses the already-created project environment without
changing dependencies. The direct executable form is preferable in scripts
because it makes the selected environment explicit.

## Check and run ZLang

Check the installed commands:

```sh
.venv/bin/zlang --version
.venv/bin/zlang-lock --version
.venv/bin/zlang-verify --version
```

`zlang-lsp` is a JSON-RPC stdio server and is normally started by the bundled
VS Code extension; it is not an interactive shell command. Point the extension
at the environment's executable when automatic discovery is not appropriate:

```json
{
  "zlang.lsp.path": "/absolute/path/to/.venv/bin/zlang-lsp"
}
```

Check a design and emit production direct SystemVerilog without installing any
external EDA program:

```sh
.venv/bin/zlang examples/add.zhl --check
mkdir -p build
.venv/bin/zlang examples/add.zhl --systemverilog build/Add.sv
```

## Choose which external tools you need

| Task | Required external programs |
| --- | --- |
| Parse, type-check, emit SystemVerilog, use LSP | None |
| Strict RTL lint and selected RTL simulation | `verilator` |
| Generic synthesis | `yosys` |
| Formal BMC, proof and cover | `yosys`, `sby`, `yosys-smtbmc`, and `z3` |
| Selected event-driven simulation tests | `iverilog` and `vvp` |

`yosys-smtbmc` is the Yosys SMT model-checking driver. It is the executable
sometimes informally shortened to “BMC”; there is no ZLang dependency named
`bmct`. ZLang's recorded formal route invokes the external `z3` executable
through this driver. Installing only the Python `z3-solver` module is therefore
not a substitute for putting `z3` on `PATH`.

## Recommended: OSS CAD Suite

The simplest reproducible route is the official YosysHQ OSS CAD Suite. Its
release archive provides Yosys, SymbiYosys, solvers and related open-source EDA
tools together. Download the archive for the host platform from the
[OSS CAD Suite releases](https://github.com/YosysHQ/oss-cad-suite-build/releases),
extract it, then either source its environment file for each shell:

```sh
source /absolute/path/to/oss-cad-suite/environment
```

or add its `bin` directory to `PATH`:

```sh
export PATH="/absolute/path/to/oss-cad-suite/bin:$PATH"
```

Use an absolute installation path. Do not copy individual binaries out of the
suite because Yosys and SymbiYosys also use adjacent data and support files.
See the upstream
[OSS CAD Suite installation instructions](https://github.com/YosysHQ/oss-cad-suite-build/blob/main/README.md)
for supported archives and platform details.

Install Verilator separately when it is not supplied by the selected suite. A
distribution package is convenient:

```sh
sudo apt-get update
sudo apt-get install verilator
```

Distribution versions vary. Follow the upstream
[Verilator installation guide](https://verilator.org/guide/latest/install.html)
when the release-tested version or a source build is required.

## Alternative: install components separately

Component installation is useful for distribution packaging or when a pinned
tool is maintained independently:

- install Yosys using the upstream
  [Yosys build/install instructions](https://yosyshq.readthedocs.io/en/latest/install.html);
- install SymbiYosys using the upstream
  [SymbiYosys documentation](https://symbiyosys.readthedocs.io/en/stable/index.html);
- install the `z3` command from the official
  [Z3 releases](https://github.com/Z3Prover/z3/releases) or a suitable
  distribution package;
- install Verilator using its official guide linked above;
- install Icarus Verilog from the host distribution when the selected test or
  simulation flow requires `iverilog`/`vvp`.

Keep all selected tool `bin` directories on the same `PATH` used to start
ZLang, the terminal, CI job or VS Code. Mixed host/container installations are
a common reason for a tool being visible interactively but unavailable to the
compiler.

## Verify the installation

```sh
verilator --version
yosys -V
sby --version
command -v yosys-smtbmc
z3 --version
iverilog -V
```

The following are **verified versions**: they are the versions used to produce
this release's acceptance evidence. They are not universal minimums, maximums,
or a claim that adjacent upstream versions are incompatible.

| Component | Verified version | Purpose |
| --- | --- | --- |
| Host | Linux x86-64 | Release and regression host |
| CPython | 3.12.3 (package range `>=3.12,<3.13`) | Compiler, CLI and LSP runtime |
| Verilator | 5.052 | Strict RTL lint and behavioral simulation |
| Yosys | 0.69 | Generic synthesis and SMT preparation |
| SymbiYosys (`sby`) | 0.69 | Formal job orchestration |
| `yosys-smtbmc` | from verified Yosys installation | SMT model-checking driver |
| Z3 | 4.8.12 | Verified external SMT solver |
| Icarus Verilog / `vvp` | 14.0 | Selected event-driven simulations |

The machine-readable authority is [`release/status.json`](../release/status.json).
Consult it instead of copying this table into automation because it is updated
with each release candidate. Other versions may work; validate them with the
smoke tests relevant to the flow you intend to use. A detected executable is
not, by itself, evidence that a formal or synthesis result is valid.

Run a real formal smoke test:

```sh
.venv/bin/zlang examples/verification/bounded_counter.zhl \
  --verify --verify-require proven \
  --formal-depth 16 --formal-timeout 45 \
  --verification-bundle build/verify/counter \
  --verification-work-dir build/verify-work/counter \
  --verification-report build/verify/counter-result.txt
```

Expected exit status is zero. The `capacity` assertion should be `proven`, the
`reaches_capacity` cover should be `witnessed`, and `exceeds_capacity` should be
`bounded_unreached`. A bounded cover miss is not a proof of unreachability.
See the [runnable formal examples](../examples/verification/README.md) for a
deliberate counterexample, scoped assumptions, immutable bundle replay and
ready/valid checking.

## Verification architecture and troubleshooting

- safety verification provides compiler-owned safety properties and source verification goals.
- semantic-reference equivalence compares supported selected implementations with the semantic reference.
- backend binding is the BackendArtifact and semantic-binding foundation used to connect
  formal observations to direct SystemVerilog.
- retired cross-backend equivalence was the former Clash-to-direct-SV comparison. Clash is retired, so current
  releases preserve historical retired cross-backend equivalence records only and do not execute retired cross-backend equivalence.
- formal-aware selection can require semantic-reference equivalence evidence while selecting supported implementation
  candidates.

Formal execution is opt-in. `--check`, normal compilation and SystemVerilog
emission do not invoke a solver. `bounded_pass` is bounded evidence and is never
reported as `proven`; unavailable tools or bindings remain explicit
`unknown`/`skipped` according to the requested policy.

If a tool is not found, run the version commands above in the same environment
that starts ZLang. If a proof is `unknown`, inspect the retained report and
solver logs under `--verification-work-dir`; increasing depth or timeout does
not turn an unsupported binding or reset model into an executable property.
