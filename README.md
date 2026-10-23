# ZLang HDL

ZLang HDL is an experimental hardware description language with exact types,
explicit cycle/clock semantics, reusable protocols and verification-aware
implementation selection. Direct SystemVerilog is the production RTL backend.

**Alpha candidate: 0.1.0a24.** Unsupported combinations fail closed.
The supported native platform is Linux x86-64 (including WSL2), CPython
`>=3.12,<3.13`. Native simulation needs the matching runtime wheel.
A checked-in candidate is not a published tag or Release.

## Start here

| Manual | Use it for |
|---|---|
| [Language Reference](docs/language-reference.md) | Syntax, exact types, state, protocols, timing and verification constructs |
| [Standard Library Guide](docs/stdlib.md) | API signatures, numerical/stream contracts, component selection and recipes |
| [User Guide](docs/user-guide.md) | Install, projects, simulation, Python, debugger, waveform, RTL and formal workflows |

Each manual has a matching downloadable PDF linked from its entrypoint.
Keep the three PDFs together for offline inter-book navigation.
The [quick reference](docs/language-quick-reference.md) is a syntax cheat sheet,
not a fourth manual. [Documentation map](docs/README.md) separates current guides
from historical engineering evidence.

## Try a checkout

```sh
git clone https://github.com/postoroniy/zlang-hdl.git
cd zlang-hdl
make venv
source .venv/bin/activate
zlang examples/counter.zhl --top Counter --check
zlang sim examples/counter.zhl --top Counter --events examples/counter.events.jsonl --trace build/counter.fst
```

[Two complete workflows](docs/tutorial.md) take Counter through saved waveforms
and RuleAction through F9 → Continue → source highlight → Add to Waveform.
The [installation chapter](docs/installing-toolchain.md) covers wheel installs,
external EDA tools, editor refresh and bootstrap troubleshooting.

`make venv` uses this checkout's environment and matching reviewed native wheel.
On supported local hosts with VS Code and Node.js 22 it also refreshes the VSIX.
Use `INSTALL_EDITOR=0` to leave the editor unchanged; `make editor-install`
refreshes it later. `make -n venv` performs no installation.
The universal VSIX is editor/LSP-only; native debugging/waveforms use Linux-x64.

## Validation and contribution

<a id="projects-and-lock-files"></a>
For project manifests and reproducible dependencies, see
[projects and lock files](docs/projects-dependencies.md).

The compiler-owned support registry and [exact release metadata](release/status.json)
define executable support and reviewed artifact identities. New source/docs work
requires a new freeze and acceptance run; old green results do not qualify it.
The [release process](docs/release-process.md) describes non-publishing validation
and the separately authorized signed-tag/publication steps.
Read the [stdlib qualification boundaries](docs/stdlib-formal-coverage.md) before
treating source availability as proof for a chosen specialization.

See [CONTRIBUTING.md](CONTRIBUTING.md) for DCO and test expectations,
[SUPPORT.md](SUPPORT.md) for community help and [SECURITY.md](SECURITY.md)
for private vulnerability reporting. Optional support:
[Buy Me a Coffee](https://buymeacoffee.com/zlanghdl).

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
