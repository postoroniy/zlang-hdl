# Community release process

ZLang Community releases use one reviewed source tree and two modes of the
same GitHub `Release` workflow. The candidate mode validates and retains
artifacts but cannot publish. Only a signed annotated tag at the exact protected
`main` commit enables the publish job.

This document also owns branch lifecycle, durable regression, release
inclusion, and post-publication retention policy. In particular, a fix is not
release evidence until it is represented in the candidate's
`release/regressions.json` with permanent tests.

## Local candidate

Create a release branch from the latest public `main`. Update the compiler,
native dependency, native wheel, changelog, reference/version metadata and
release status to the same prospective version. Then commit the complete
candidate and run:

```bash
make release-candidate \
  TAG=v0.1.0a19 \
  PREVIOUS_TAG=v0.1.0a18 \
  BUILD_ROOT=build/a19-local \
  EDITOR_VSIX=build/a19-editor/zlang-hdl-0.1.0.vsix
```

This target does not commit, tag, push or publish. It requires a clean checkout
and runs the release identity preflight, public-tree/status checks, static and
native audits, two no-skip suites, external-tool inventory, an installed-VSIX
host smoke, and reproducible package construction.

Each regression pass runs the ordinary deterministic suite and the six
isolated `performance` regressions as separate JUnit reports. Release status
validates both partitions; neither report may contain a skip, failure, or error.

`tools/release_preflight.py` composes existing authoritative validators. Its
deterministic JSON binds the package version, prospective tag, previous tag,
Git commit/tree, changelog notes, reviewed PDF, editor lock/package, release
status, regression ledger and audited native wheel. It never creates a tag or
release.

The Community tree carries the Markdown reference sources and the reviewed PDF
artifact. `make community-pdf-check` validates the PDF digest, page count,
metadata and source identities recorded in `release/status.json`. PDF authoring
assets are release-maintainer inputs and are not advertised as a public build
command when they are absent from the Community tree.

## Hosted pre-tag gate

After the release PR is reviewed and its required checks pass, merge it. Run the
`Release` workflow manually on the exact `main` commit with:

- `tag`: the prospective tag, for example `v0.1.0a19`;
- `previous_tag`: the exact prior release, for example `v0.1.0a18`.

Manual dispatch runs validation and EDA jobs, uploads review artifacts and does
not attest or publish. It fails if the prospective tag already exists, is not
the immediate alpha successor, or the selected commit is not descended from
the previous tag.

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
- `zlang-public/` is the clean mirror of published/protected Community `main`.
  It is a synchronization and inspection checkout, not a scratch worktree.
- a named `codex/community-<version>-release` worktree is the only place where
  a Community release candidate is assembled. It starts at exact public
  `main` and contains only reviewed Community files.
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
