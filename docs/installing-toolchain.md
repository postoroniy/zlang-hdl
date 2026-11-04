<a id="reference-installing-toolchain"></a>
# Installation, WSL2, and external tools


ZLang HDL supports CPython `>=3.12,<3.13`. The source compiler is release-tested
on Linux x86-64. Native-simulation wheels are supplied for Linux x86-64
(including WSL2). macOS native wheels are not part of this release. You do not
need to find a distribution package for that exact Python runtime: the recommended `uv`
workflow can download and manage it independently of the system Python.
Parsing, semantic checking and Direct-SV generation need only the
Python package. Verilator, Yosys, SymbiYosys and a solver are external programs
used only when their corresponding lint, synthesis or formal flow is requested.

The exact versions used by release acceptance are recorded in
[`release/status.json`](../release/status.json). Other versions may work, but
they are not evidence for the published release.

<a id="reference-installing-toolchain-recommended-installation-with-uv"></a>
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
uv tool install --python '>=3.12,<3.13' \
  --with /path/to/zlang_native_sim-VERSION-cp312-abi3-PLATFORM.whl \
  /path/to/zlang_hdl-VERSION-py3-none-any.whl
zlang --version
zlang lock --version
zlang verify --version
```

If `uv` reports that its tool directory is not on `PATH`, run `uv tool
update-shell`, then start a new shell. `uv tool dir --bin` prints the command
directory for manual PATH configuration. To replace an installation made from
a downloaded wheel, repeat `uv tool install --force ...` with the new wheel.

For Python testbenches or an isolated library installation rather than a tool,
create a local environment and install both matching wheels into it:

```sh
uv venv --python '>=3.12,<3.13' .venv
source .venv/bin/activate
uv pip install \
  /path/to/zlang_native_sim-VERSION-cp312-abi3-PLATFORM.whl \
  /path/to/zlang_hdl-VERSION-py3-none-any.whl
```

For use from a Git checkout, install these host prerequisites first:

- A glibc-based Linux x86-64 host, including WSL2. The reviewed native runtime
  wheel requires glibc 2.28 or newer; the Linux-x64 debugger/viewer executables
  require glibc 2.34 or newer.
- Git, GNU Make, Bash 4 or newer, and standard shell utilities.
- Either `uv`, which can provision CPython 3.12, or an existing CPython 3.12
  with working `venv` and `ensurepip` support. First setup needs network access
  for Python packages and, with managed Python, the interpreter.
- For optional editor installation: VS Code's `code` CLI, Node.js 22, npm and
  `readelf` from binutils. ZLang does not install these host applications.

Without Node.js/npm or the VS Code CLI, compiler/native/LSP setup can complete
without changing the editor. `INSTALL_EDITOR=0` explicitly selects that route.
EDA tools are separate prerequisites for the flows that use them. A normal
checkout installation uses reviewed native binaries and needs no Rust or C++
build toolchain.

Keep the environment local to the repository:

```sh
git clone https://github.com/postoroniy/zlang-hdl.git
cd zlang-hdl
make install
source .venv/bin/activate
zlang --version
```

`make install` installs the checkout compiler, LSP and its matching reviewed native
wheel into one local environment, without Python test or release tools.
No profile selection is needed. `make venv` retains the complete maintainer setup. On a supported host with VS Code and Node.js
22, it also installs the matching extension. Use `INSTALL_EDITOR=0` to leave
the installed extension unchanged. Installing only `.[test]` does not install
the native runtime and is not equivalent to this complete setup.

`make -n install` prints the bootstrap recipe without installing anything. A real
bootstrap rejects an active sibling environment or an incompatible local
interpreter before installation. Deactivate the foreign environment and retry;
move an incompatible `.venv` aside explicitly, since bootstrap does not delete it.
Stale checkout distribution metadata is preserved below ignored build output
rather than allowed to shadow the installed compiler. If optional editor setup
fails, Python/native/LSP setup may already be complete: retry `make editor-install`,
or use `make install INSTALL_EDITOR=0`. With missing Node.js/npm, install editor
prerequisites separately. `KEEP_TMP=1 make install` retains bootstrap failure logs.

To replace an old waveform/editor UI after a pull, run `make editor-install`
with Node.js 22 and reload VS Code. Close/reopen waveform tabs: existing webviews
keep their loaded JavaScript. Set `VSCODE=/path/to/code` for an alternate executable.

Install the matching extension and set `"zlang.lsp.path"` in VS Code to the
absolute path of the environment's `zlang-lsp` executable. In a checkout this
is `${workspaceFolder}/.venv/bin/zlang-lsp`. See the
[language-server guide](zlang-lsp.md) for diagnostics/navigation and the
[debugger guide](native-debugging.md) for the Linux-x64 native-debug requirements.

The commands below use `zlang` directly: it is on `PATH` after `uv tool
install`, or after activating the checkout's virtual environment with
`source .venv/bin/activate` in a Bash/Zsh terminal. Alternatively, in the
checkout use `uv run --no-sync zlang ...` without activating it. Open a new
terminal and activate the environment again when returning to the checkout.

<a id="reference-installing-toolchain-python-dependency-profiles"></a>
### Checkout Python dependency profiles

For ordinary use, `make install` selects runtime dependencies and refreshes the
VS Code extension when prerequisites are available. The existing bootstrap also
supports explicit additive profiles for development and release work:

| Profile | Python dependencies | Editor default |
| --- | --- | --- |
| Runtime | Compiler, native runtime and LSP; no test or release tools | Unchanged |
| Development | Runtime plus the complete test/formal dependencies and Ruff | Unchanged |
| Release | Development plus all pinned release, license and dependency-audit tools | Refresh when prerequisites are available |

For explicit profile selection:

```sh
make venv PYTHON_PROFILE=runtime
make venv PYTHON_PROFILE=dev
make venv PYTHON_PROFILE=release
```

All profiles retain the pinned pip/setuptools bootstrap for editable installation.
`release` remains the default. No tests, security checks or release gates are
removed by choosing a smaller installation. Use the release profile before
running release and audit commands; smaller environments do not bypass missing
prerequisites. `INSTALL_EDITOR=0` always leaves the extension unchanged;
`INSTALL_EDITOR=1` explicitly enables its existing build/install workflow.

Profiles never uninstall or synchronize packages already in an environment.
Selecting `runtime` in an existing development environment will retain its
existing packages. For a separate runtime environment below the same checkout:

```sh
make venv PYTHON_PROFILE=runtime VENV=.venv-runtime
source .venv-runtime/bin/activate
```

Pass that same `VENV=.venv-runtime` to subsequent Make commands. Deactivate it
before using another environment. Each environment remains owned by its checkout.

The optional `formal` extra installs the checkout's pinned Python Z3 bindings
without pytest or release tooling. After activating a runtime checkout environment:

```sh
python -m pip install -e '.[formal]'
```

The complete `test` extra already includes the same solver package, so development
and release profiles retain it. External Yosys/SymbiYosys/SMT executables still
need separate provisioning. Without Python Z3, eligible arithmetic proofs retain
the raw SMT route; missing tools, timeouts and inconclusive results do not become
proofs. See [formal verification](formal-verification.md).

These commands configure a source checkout. Existing downloaded wheels and
editor packages retain their original dependency metadata.

<a id="reference-installing-toolchain-conventional-venv-and-pip-alternative"></a>
## Conventional venv and pip alternative

If a compatible Python is already available, use whatever executable name the
host provides; the example deliberately avoids assuming a version-suffixed
command:

```sh
python3 -c 'import sys; assert (3, 12) <= sys.version_info < (3, 13), sys.version'
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install \
  /path/to/zlang_native_sim-VERSION-cp312-abi3-PLATFORM.whl \
  /path/to/zlang_hdl-VERSION-py3-none-any.whl
zlang --version
```

For an editable development checkout, replace the two-wheel installation
command with:

```sh
python -m pip install \
  /path/to/zlang_native_sim-VERSION-cp312-abi3-PLATFORM.whl \
  -e '.[test]'
```

The native wheel must match this checkout's required version and platform.
This fallback needs a compatible interpreter and `venv` support from the host.
ZLang should be installed with `uv` or an isolated virtual environment, leaving
the operating system's own Python unchanged.

<a id="reference-installing-toolchain-windows-through-wsl2"></a>
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
`~/src/zlang-hdl`, rather than a Windows-mounted drive; this generally gives much better
compiler, Git, tests and EDA-tool filesystem performance.

Inside a development checkout, these forms are equivalent:

```sh
source .venv/bin/activate
zlang examples/add.zhl --check
uv run --no-sync zlang examples/add.zhl --check
```

`uv run --no-sync` uses the already-created project environment without
changing dependencies. Scripts should either activate the environment first or
use `uv run --no-sync`, making them independent of interactive shell state.

<a id="reference-installing-toolchain-check-and-run-zlang"></a>
## Check and run ZLang

Check the installed commands:

```sh
zlang --version
zlang lock --version
zlang verify --version
```

Check a design and emit production Direct-SV without installing any
external EDA program:

```sh
zlang examples/add.zhl --check
mkdir -p build
zlang examples/add.zhl --systemverilog build/Add.sv
```

<a id="reference-installing-toolchain-choose-which-external-tools-you-need"></a>
## Choose which external tools you need

Only a compatible Python runtime and the `zlang-hdl` package are mandatory for
checking source and emitting SystemVerilog. `uv` is an optional installer: it
can obtain that Python runtime, but ZLang does not need `uv` after installation.
Install other tools only for the workflows you actually use:

| Workflow | Additional requirement | If it is missing |
| --- | --- | --- |
| `zlang SOURCE --check` and SystemVerilog emission | None beyond Python and `zlang-hdl` | Neither Verilator nor Yosys is needed; a missing compiler package or incompatible Python prevents `zlang` from starting. |
| `zlang sim` (native simulation) | Matching `zlang-native-sim` wheel for the host | Simulation fails explicitly; compiler checking and RTL emission remain available. |
| Strict RTL lint or Verilator execution | `verilator` | RTL can still be emitted; lint and Verilator simulation cannot run. |
| Generic synthesis | `yosys` | RTL can still be emitted; synthesis cannot run. |
| Formal BMC, proof, or cover | `yosys`, `sby`, `yosys-smtbmc`, and one selected solver executable | Checking and RTL emission still work; requested formal jobs cannot complete. |
| Icarus-based test or simulation flow | `iverilog` and `vvp` | Only that selected flow is unavailable. |
| `zlang lock update` with Git dependencies | `git` and access to the pinned revision during update | Path-only projects and locked offline compilation still work; fetching a Git dependency does not. |

`yosys-smtbmc` is the Yosys SMT model-checking driver. It is the executable
sometimes informally shortened to “BMC”; there is no ZLang dependency named
`bmct`. The default recorded formal route invokes the external `z3` executable
through this driver. Bundle replay may instead select another supported
SMTBMC solver, such as `boolector`, `bitwuzla`, `yices`, or `cvc5`, with
`zlang verify BUNDLE --solver NAME`. Installing only a Python solver module is
not a substitute for putting the corresponding executable on `PATH`.

<a id="reference-installing-toolchain-recommended-oss-cad-suite"></a>
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

Use an absolute installation path and keep the suite intact because Yosys and
SymbiYosys also use adjacent data and support files.
See the upstream OSS CAD Suite release page linked above for supported
archives and platform details.

Install Verilator separately when it is not supplied by the selected suite. A
distribution package is convenient:

```sh
sudo apt-get update
sudo apt-get install verilator
```

Distribution versions vary. Follow the upstream
[Verilator installation guide](https://verilator.org/guide/latest/install.html)
when the release-tested version or a source build is required.

<a id="reference-installing-toolchain-alternative-install-components-separately"></a>
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

<a id="reference-installing-toolchain-verify-the-installation"></a>
## Verify the installation

```sh
verilator --version
yosys -V
sby --version
command -v yosys-smtbmc
z3 --version
boolector --version
bitwuzla --version
iverilog -V
```

The release-tested external-tool versions are recorded only in the
[release status](../release/status.json) under `eda_toolchain`; the supported
Python range is recorded under `platform.python`. These are evidence for that
release, not universal tool-version requirements. Other versions may work;
validate them for the flow you intend to use. Finding an executable on `PATH`
is not, by itself, evidence that a formal or synthesis result is valid.

Run a real formal smoke test:

```sh
zlang examples/verification/bounded_counter.zhl \
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

<a id="reference-installing-toolchain-verification-architecture-and-troubleshooting"></a>
## Troubleshoot formal runs

Formal execution is opt-in. `--check`, normal compilation and SystemVerilog
emission do not invoke a solver. `bounded_pass` is bounded evidence and is never
reported as `proven`; unavailable tools or bindings remain explicit
`unknown`/`skipped` according to the requested policy.

If a tool is not found, run the version commands above in the same environment
that starts ZLang. If a proof is `unknown`, inspect the retained report and
solver logs under `--verification-work-dir`; increasing depth or timeout does
not turn an unsupported binding or reset model into an executable property.
