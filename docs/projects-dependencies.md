<a id="reference-projects-dependencies"></a>
# Projects and dependencies


ZLang source imports are logical names. Physical paths, Git URLs, and revisions
belong to project metadata, never to `.zhl` source:

```zlang
import acme.dsp.filters
import std.math.complex
```

An import may introduce a source-local qualifier without changing the logical
module or dependency identity:

```zlang
import std.math.complex as cx

in sample : cx.Complex<u8>
out total : cx.Complex<u9>
total = cx.complex_sum([sample, sample])
```

The qualifier applies to types, functions, and struct constructors declared by
that exact logical module (`cx.Complex { re = ... im = ... }` is valid).  It is
erased before semantic typing, so the qualified and historical unqualified
spellings have the same canonical identity.  An aliased import does not expose
those declaration names unqualified and does not re-export declarations from a
transitive dependency.  Aliases are not runtime values or filesystem names;
wildcards, member renaming, and re-export remain unsupported.

The compiler-shipped `std` namespace keeps its existing resolver. Other package
names are resolved only when the source belongs to a locked project.

<a id="reference-projects-dependencies-project-manifest"></a>
## Project manifest

A project is rooted by a versioned `zlang.toml`:

```toml
schema = 1

[project]
name = "demo"
version = "0.1.0"
source-root = "src"

[dependencies]
acme = { path = "../acme-dsp" }

[dependencies.bus_models]
git = "https://example.invalid/hardware/bus-models.git"
rev = "0123456789abcdef0123456789abcdef01234567"
```

The `schema` field is required project metadata: it tells `zlang lock update`
how to interpret the manifest. Use the value shown for the current project
format.

Imports use logical package and module names rather than absolute checkout or
cache paths. A file `filters.zhl` directly below package `acme`'s source root
is imported as `acme.filters`; nested directories append dotted components.

Path locators are relative to the manifest containing them. Git revisions must
be full lowercase 40- or 64-digit hexadecimal object IDs. Branch names, tags,
and abbreviated revisions are deliberately rejected because they are mutable.

<a id="reference-projects-dependencies-lock-update"></a>
## Lock update

Dependency resolution and fetching are explicit:

```sh
zlang lock update --project zlang.toml
```

The command validates the complete transitive graph before publishing a
deterministic `zlang.lock`. It records each package manifest, exact source-module
index, imports, content digests, and dependency edges. Git content is populated
in the project cache during this command. Path dependencies remain at their
manifest-relative location and are checked byte-for-byte against the lock.

Resolution rejects:

- dependency cycles and conflicting package/module declarations;
- source-root traversal or symlink escape;
- a package whose declared identity does not match its dependency key;
- missing, added, removed, or modified locked `.zhl` modules;
- modified dependency-resolution fields in dependency manifests (profile-only
  edits are intentionally outside the resolution identity);
- missing Git cache content or a revision mismatch.

The lock is written only after the entire graph validates. Failed updates leave
the previously accepted lock usable.

| Scenario | What to do | Without it |
| --- | --- | --- |
| Standalone source or compiler-shipped `std.*` imports | Compile directly; no project lock is needed. | No consequence. |
| Project with local path packages | Run `zlang lock update` after declaring dependencies and whenever their source or resolution fields change. Keep `zlang.lock` with the project. | Compilation rejects a missing or stale lock; it never silently uses changed dependency files. |
| Project with pinned Git packages | Run `zlang lock update` while Git/network access is available; retain the exact revision and local cache for later offline builds. | Ordinary compilation does not fetch; missing cache or changed revision fails. |
| CI or another machine | Supply the project manifest, its lock, exact path sources and cached Git revisions (or update the lock before the offline build). | A lock file alone does not provide Git source bytes; compilation fails closed. |

`zlang lock update` resolves dependencies; it is not a package installer or a
step required for every single-file invocation.

Bounded scalar `extern module` implementations may also be declared under
`[external-mappings.NAME]` and selected by a profile's
`external-mappings = ["NAME"]`.  The lock records every HDL source path and
SHA-256.  Paths are project-relative, must remain inside the project, and may
not be symlinks. The mapping changes generated SystemVerilog without changing
the behavior expressed by the ZLang source.

## Optional exact compiler and stdlib lock

Project schema 1 and lock schema 2 remain supported. A project may instead use
schema 2 to pin the compiler and bundled standard library as well as its source
dependencies. Inspect the SDK you actually intend to use:

```sh
zlang lock toolchain
zlang lock toolchain --json
```

The first command prints the three requirement fields. Copy their exact values
into a `[toolchain]` table: `compiler-version`, `compiler-identity`, and
`stdlib-identity`; then run `zlang lock update`. This produces lock schema 3
with the checked compiler/runtime and stdlib inventories. Both the version
and identities must match. A pinned dependency also requires a schema-2 root
with compatible requirements. The reserved `std` namespace remains bundled
with the SDK, rather than becoming a separately fetched package.

Missing or changed SDK inputs reject compilation and cached tooling reuse.
Restart the compiler/LSP after replacing compiler files. Selecting a different
SDK is an explicit requirement update and requalification, followed by lock
regeneration; the lock command does not install or upgrade a compiler.
Earlier a24 binaries may predate this opt-in contract: use a matching build of
the current source SDK, not merely another binary with the same version label.

<a id="reference-projects-dependencies-offline-compilation"></a>
## Offline compilation

Ordinary compilation never fetches dependencies and never updates project
metadata:

```sh
zlang src/top.zhl --project zlang.toml --check
zlang src/top.zhl --project zlang.toml --systemverilog build/top.sv
```

`--project` may name a manifest or its directory. Without it, `zlang` searches
the source file's parent directories for `zlang.toml`. If no project is found,
single-file and compiler-shipped `std.*` compilation retain their existing
behavior; arbitrary external imports remain an explicit error.

Compilation checks the current manifest, lock, every path dependency, and every
cached Git dependency before checking the source. It is read-only: unavailable or
dirty content is a lock mismatch, not an invitation to fetch or rewrite files.
Build outputs and ZLang output/cache directories must remain outside
the resolved root, manifest, lock, dependency, and stdlib inputs.

<a id="reference-projects-dependencies-identity-and-artifacts"></a>
## Identity and artifacts

The exact locked dependency closure is carried through semantic and canonical
IR and BackendArtifact JSON. It contributes to selected/build identities,
formal proof-cache keys, and synthesis-cache keys. Changing dependency content
therefore invalidates results even when emitted RTL text happens to remain
identical.

`artifact_hash` keeps its narrower meaning: the hash of emitted backend text.
The separate build identity combines that text with the logical root module and
locked semantic closure. This distinction permits byte-identical RTL to be
recognized while preventing evidence from one dependency closure being reused
for another.

<a id="reference-projects-dependencies-deliberate-boundaries"></a>
## Deliberate boundaries

The current project model has no registry or semantic-version solver, editable
global packages, implicit network access, Git submodules/LFS/subdirectories,
wildcard imports, member renaming, or re-export. Implementation profiles are a
separate set of build settings and do not change dependency resolution.
Path dependencies declared by a Git package are also deferred in this bounded
slice; use another pinned Git dependency instead of reaching outside a fetched
checkout.

Ordinary compilation remains offline; resolved module contents and their
recorded digests define the accepted dependency contents.
