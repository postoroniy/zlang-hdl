# Community release process

ZLang Community releases use one reviewed source tree and two modes of the
same GitHub `Release` workflow. The candidate mode validates and retains
artifacts but cannot publish. Only a signed annotated tag at the exact protected
`main` commit enables the publish job.

This document also owns branch lifecycle, durable regression, release
inclusion, and post-publication retention policy. In particular, a fix is not
release evidence until it is represented in the candidate's
`release/regressions.json` with permanent tests.

## Operator checklist

This is the canonical operator procedure, including the hosted-policy detail.
Do not substitute
an ambient Python, an old installed wheel, an arbitrary Git branch, or an
untracked reproducer for one of these checks.

### 1. Establish the correct release tree

- Integrate and prove fixes in the private integration repository first. Preserve
  each accepted fix on a named branch with a permanent regression before it is
  considered for a Community release.
- Create the Community candidate only in `zlang-public/`, starting from the
  current public `origin/main` on a named `codex/community-<version>-release`
  branch. Transfer only reviewed public source slices; never merge private Git
  history or copy a dirty private worktree wholesale.
- Before review, verify the candidate parent is public `origin/main`, the
  previous published tag is an ancestor, and the public object database cannot
  resolve private-only commits. `tools/public_tree.py check-source` validates
  the allow-listed file surface; Git ancestry is an additional human review.
- Each checkout owns its `.venv`. Run `make venv`, use `make` targets or
  `tools/run_local_env.sh`, and let them create unique scratch directories below
  that checkout's ignored `build/tmp`. Never borrow a sibling checkout's venv,
  run release checks against an ambient `zlang`, or use a shared `/tmp` tree as
  a fixture or artifact location. Use `KEEP_TMP=1` only to retain one failed
  run's worktree-local evidence.

### 2. Make the candidate internally consistent

Update all of these together, then commit the public candidate:

| Release fact | Required evidence |
| --- | --- |
| version | `zlang/_version.py`, native optional dependency, native wheel, `release/status.json`, and the prospective tag agree |
| notes | the exact `## <version>` section in `CHANGELOG.md` contains every shipped change and the approved publication date; `Unreleased` contains no shipped release notes, and no entry may predate the candidate commit |
| fixes | `release/regressions.json` accounts for every accepted public fix since the previous tag, with permanent source paths and exact selectors |
| documentation | language-reference Markdown, final PDF body, and the **rendered cover** identify the same version; status records the PDF, cover, builder and source digests |
| release surface | `make public-check` exports and validates only the Community file set; no private handoff, credentials, host paths, untracked witness, or excluded experiment enters it |

The PDF builder is host-owned. Build the final PDF against the exact exported
public snapshot, record the builder and cover digests, and visually inspect the
rendered cover plus representative code-heavy pages before committing it. A
matching PDF hash proves the reviewed bytes are retained; it does not replace
that human visual check.

### 3. Run the pre-merge review gate

On the clean, committed public review branch, run:

```bash
make venv
make release-review \
  TAG=v0.1.0a20 \
  PREVIOUS_TAG=v0.1.0a19
```

`release-review` rejects local modifications and untracked files, validates the
ledger, PDF/status, clean public export, static checks, and two independent
zero-unexpected-skip runs against a freshly exported source tree and its own
venv. It deliberately does **not** claim candidate readiness and does not
publish, tag, push, install a global package, or use a sibling checkout.

### 4. Merge, then run the exact-main candidate gate

After review and required protected-main checks, merge the public candidate.
The manual hosted Release workflow must be dispatched with the exact new tag and
previous tag. Candidate mode explicitly requires checked-out `main` to equal
`origin/main`; running `make release-preflight` or `make release-candidate` on
a review branch is expected to fail. This prevents an arbitrary feature branch
from being certified as releasable.

### 5. Publish only after hosted evidence

Inspect the hosted `release-preflight.json`, regression identities, PDF/native
wheel/VSIX identities, security evidence, package inventories, and retained
test/EDA artifacts. Only then create and push one signed annotated tag at that
exact accepted `main` commit. The tag workflow repeats validation and is the
only workflow path that attests and publishes. Never rewrite a tag; a failed
tag requires a later reviewed alpha.

Keep named implementation and recovery branches until the published artifact
replay, exact-tag checks, durable evidence archive, and handoff/ledger review
are complete. Do not delete branches, worktrees, or source fixtures merely
because a PR merged.

## Command reference

Create the public release branch from the latest public `main`. Update the
compiler, native dependency, native wheel, changelog, reference/version metadata
and release status to the same prospective version, then commit the complete
public candidate. Bootstrap that checkout's own environment and run the
pre-merge review gate:

```bash
make venv
make release-review \
  TAG=v0.1.0a20 \
  PREVIOUS_TAG=v0.1.0a19
```

After merge, exact protected `main` may run the stricter local candidate command
when its required host tools are available:

```bash
make release-candidate \
  TAG=v0.1.0a20 \
  PREVIOUS_TAG=v0.1.0a19 \
  BUILD_ROOT=build/a20-local \
  EDITOR_VSIX=build/a20-editor/zlang-hdl-0.1.0.vsix
```

Neither command commits, tags, pushes, or publishes. The candidate command runs
the exact-main identity preflight plus native audits, external-tool inventory,
an installed-VSIX host smoke, and reproducible package construction in addition
to the review checks.

Each regression pass runs the ordinary deterministic suite and the six
isolated `performance` regressions as separate JUnit reports. Release status
validates both partitions; neither report may contain a skip, failure, or error.

Release commands never borrow a sibling worktree's interpreter. Every run uses
the candidate's `.venv` and creates collision-safe transient files below that
candidate's `build/tmp`; `KEEP_TMP=1` retains a failed run's scratch directory.

`tools/release_preflight.py` composes existing authoritative validators. Its
deterministic JSON binds the package version, prospective tag, previous tag,
Git commit/tree, changelog notes, reviewed PDF, editor lock/package, release
status, regression ledger and audited native wheel. It never creates a tag or
release.

The Community tree carries the Markdown reference sources and the reviewed PDF
artifact. `make community-pdf-check` validates the PDF digest, page count,
metadata and source identities recorded in `release/status.json`. PDF authoring
is host-owned: build the final PDF against the exact exported public snapshot,
recording the audited host builder digest, before committing that snapshot.
Assets are release-maintainer inputs and are not advertised as a public build
command when they are absent from the Community tree.

## Hosted pre-tag gate

After the release PR is reviewed and its required checks pass, merge it. Run the
`Release` workflow manually on the exact `main` commit with:

- `tag`: the prospective tag, for example `v0.1.0a20`;
- `previous_tag`: the exact prior release, for example `v0.1.0a19`.

Manual dispatch runs validation and EDA jobs, uploads review artifacts and does
not attest or publish. It checks out `main` explicitly and fails if the
prospective tag already exists, is not the immediate alpha successor, the
selected ref is not `main`, `HEAD` differs from `origin/main`, or the selected
commit is not descended from the previous tag.

## Signed publication

Only after the hosted candidate gate passes, create a signed annotated tag at
the exact reviewed `main` commit and push that tag. The tag-triggered workflow
repeats all release checks, verifies GitHub's signature and exact-main identity,
attests the accepted checksums, and publishes the GitHub prerelease.

Never rewrite an existing release tag. If a tag-triggered gate fails after the
tag exists, fix the cause through a reviewed commit and prepare the next alpha.

## Repository and worktree roles

The private integration repository, public mirror, and candidate worktree have
different owners and must not be used interchangeably:

- `zlang/` is the private integration and handoff repository. It may contain
  private coordination records and work not eligible for Community.
- `zlang-public/` is the only Community Git checkout. It mirrors
  published/protected `main`, carries a named public review branch while a
  release is prepared, and owns its own `.venv` plus ignored `build/` scratch.
  Do not create sibling `zlang-public-*` development or test worktrees.
- a `codex/community-<version>-release` branch in `zlang-public/` is the only
  place where a Community release candidate is assembled. It starts at exact
  public `main` and contains only reviewed Community files.
- feature and recovery work uses a named, durable Git branch. A temporary
  directory is only a checkout location; it is never the sole record of work.

Community never imports or vendors private implementation. A candidate is
built by an explicit public projection or reviewed commits, not by copying a
dirty private root wholesale.

## Durable fix identity

Before editing, record the repository, branch, `HEAD`, worktree path, and dirty
state in the task handoff. Each fix receives a stable issue/regression identity.
The private handoff may record private commit identities; public files use only
the safe issue identity and public evidence.

An implementation is not durable until its source fix, required permanent
fixture, focused regression test, and scope/validation handoff are committed on
a named branch. Untracked reproducers, shell history, chat rollouts, generated
output, and a temporary worktree do not satisfy this rule. Recover such work
onto a named branch and commit it before continuing release preparation.

## Release inclusion ledger

Every candidate owns `release/regressions.json`. Its baseline is the previous
published tag, and it enumerates every fix considered since that tag. Each
entry is one of:

- `included`: accepted for this release and present with permanent tests;
- `deferred`: not accepted for this release, with a concrete reason and durable
  follow-up identity;
- `private_only`: intentionally outside Community, with a product-boundary
  reason; or
- `excluded_experiment`: intentionally absent from the supported release, with
  explicit inventory/diagnostic treatment.

A fix marked accepted in the task handoff must be `included`; it must never be
silently converted to another disposition. A release must not claim a fix
merely because it existed in a private branch. The public source, tests,
changelog, and ledger are the evidence of inclusion.

Release preflight validates the ledger version, release and baseline tags,
unique ordered identities, required source paths, and exact test selectors. It
binds the ledger digest and included identities into the release manifest. A
missing or renamed regression therefore fails before tagging.

## Regression requirements

- Bug fixes test the first incorrect boundary, not only a broad end-to-end path.
- Compiler and native-runtime fixes include an independent behavioral oracle
  where practical, normally Direct SystemVerilog with Verilator or Icarus.
- LSP regressions exercise real JSON-RPC and current compiler tooling results;
  mocked protocol tests alone are insufficient.
- Performance fixes use bounded permanent fixtures and assert a completion
  limit or deterministic fail-closed budget diagnostic.
- Tests never depend on a developer's untracked file, mutable `/tmp` tree,
  deleted branch, session log, or previously installed ZLang version.
- Fixed source-count totals are not feature-inclusion evidence. Inventory gates
  classify discovered sources and explicit exclusions.
- A skipped release test is a failure. Tool-dependent tests run with the pinned
  release tools.

## Candidate acceptance

Before a release PR is eligible for merge:

1. compare the private handoff with `release/regressions.json` and account for
   every item;
2. run every ledger selector;
3. run the complete zero-skip regression twice on the exact committed tree;
4. run the required static, dependency, license, package, native, EDA,
   editor-host, and reproducibility gates; and
5. inspect the exact diff from the previous tag and generated preflight
   manifest.

After merge, run the non-publishing hosted candidate workflow on exact `main`.
Only its successful commit may be signed and tagged.

## Branch retention and incident recovery

No implementation, recovery, or release branch is deleted merely because a PR
merged. It remains recoverable until the public PR and protected-main checks,
signed exact-main tag, tag validation/EDA, downloaded artifact replay, local
backup reachability, and published handoff/ledger identity all pass.

Deletion uses an exact expected-tip guard. Never bulk-delete branches by name
pattern, delete a branch with unique commits, or delete another agent's active
worktree. Archiving a managed worktree is separate from deleting its branch and
happens only after the work is accounted for.

If a branch, fixture, or claimed fix cannot be found, stop release work. Inspect
registered worktrees, local refs, reflogs, dangling objects, archives, and task
handoffs. Recover content immediately onto a named branch and commit the source
plus regression. Until it appears in the public candidate ledger and exact-tree
tests, report it as recovered but **not included**.
