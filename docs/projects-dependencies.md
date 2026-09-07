# Reproducible projects and dependencies

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
bus_models = {
  git = "https://example.invalid/hardware/bus-models.git",
  rev = "0123456789abcdef0123456789abcdef01234567"
}
```

Package and module identities contain logical names and content digests, not
absolute checkout or cache paths. A file `filters.zhl` directly below package
`acme`'s source root is imported as `acme.filters`; nested directories append
dotted components.

Path locators are relative to the manifest containing them. Git revisions must
be full lowercase 40- or 64-digit hexadecimal object IDs. Branch names, tags,
and abbreviated revisions are deliberately rejected because they are mutable.

## Lock update

Dependency resolution and fetching are explicit:

```sh
zlang-lock update --project zlang.toml
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

Bounded scalar `extern module` implementations may also be declared under
`[external-mappings.NAME]` and selected by a profile's
`external-mappings = ["NAME"]`.  The lock records every HDL source path and
SHA-256.  Paths are project-relative, must remain inside the project, and may
not be symlinks.  This is a physical direct-SystemVerilog input: it does not
replace the pure ZLang model or enter semantic IR.

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
cached Git dependency before semantic analysis. It is read-only: unavailable or
dirty content is a lock mismatch, not an invitation to fetch or rewrite files.
Build outputs and compiler-owned output/cache directories must remain outside
the resolved root, manifest, lock, dependency, and stdlib inputs. The CLI checks
the complete physical input set before publication; those host paths are safety
guards only and never become semantic or build identity.

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

## Deliberate boundaries

This first project slice has no registry or semantic-version solver, editable
global packages, implicit network access, Git submodules/LFS/subdirectories,
wildcard imports, member renaming, or re-export. Implementation profiles are a
separate compiler-policy layer and do not change dependency resolution identity.
Path dependencies declared by a Git package are also deferred in this bounded
slice; use another pinned Git dependency instead of reaching outside a fetched
checkout.

A future optional resolver or package registry may populate the same lock
model. It must not become an implicit compilation-time network dependency:
ordinary compilation remains offline, and the resolved module contents and
their recorded digests remain the authoritative dependency identity regardless
of where the content was obtained.
