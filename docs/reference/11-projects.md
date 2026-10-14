<a id="reference-projects-dependencies"></a>
## Projects, dependencies, and reproducibility

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

### Project manifest

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

### Lock update

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

<a id="reference-projects-dependencies-offline-compilation"></a>

### Offline compilation

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
