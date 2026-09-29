# Branch, release, and regression policy

This policy prevents accepted Community work from existing only in a temporary
checkout, being lost with a branch, or being omitted from the release tree.
It applies to compiler, native-simulator, standard-library, LSP, editor, test,
documentation, and release fixes.

## Repository and folder roles

The local folders have different owners and must not be used interchangeably:

- `zlang/` is the private integration and handoff repository. It may contain
  private coordination records and work that is not eligible for Community.
- `zlang-public/` is the clean mirror of published/protected Community `main`.
  It is a synchronization and inspection checkout, not a scratch worktree.
- a named `codex/community-<version>-release` worktree is the only place where
  a Community release candidate is assembled. It starts at the exact public
  `main` commit and contains only reviewed Community files.
- feature and recovery work uses a named, durable Git branch. A temporary
  directory is only a checkout location; it is never the sole record of work.

Community never imports or vendors private implementation. A release candidate
is built by an explicit public projection or reviewed commits, not by copying a
dirty private root wholesale.

## Required identity record

Before editing, record the repository, branch, `HEAD`, worktree path, and dirty
state in the task handoff. Each fix receives a stable issue/regression identity.
The private handoff may record private commit identities; public files use only
the safe issue identity and public evidence.

An implementation is not durable until all of the following exist on a named
branch:

1. the source fix;
2. a permanent regression fixture when one is required;
3. a focused regression test that fails on the affected baseline and passes on
   the fix;
4. the handoff entry describing scope and validation.

Untracked reproducers, shell history, chat rollouts, generated output, and a
temporary worktree do not satisfy this rule. Recover such work onto a named
branch and commit it before continuing release preparation.

## Release inclusion ledger

Every candidate owns `release/regressions.json`. Its baseline is the previous
published tag, and it enumerates every fix considered since that tag. Each
entry is either:

- `included`: accepted for this release and present with permanent tests;
- `deferred`: not accepted for this release, with a concrete reason and durable
  follow-up identity;
- `private_only`: intentionally outside Community, with a product-boundary
  reason; or
- `excluded_experiment`: intentionally absent from the supported release,
  with its explicit inventory/diagnostic treatment recorded.

A fix marked accepted in the task handoff must be `included`; it must never be
silently converted to another disposition. Conversely, a release must not
claim a fix merely because it existed in a private branch. The public candidate
source, tests, changelog, and ledger are the evidence of inclusion.

The release preflight validates the ledger version, release and baseline tags,
unique ordered identities, required source paths, and exact test selectors. It
binds the ledger digest and included identities into the release manifest. A
missing or renamed regression therefore fails before tagging.

## Regression rules

- Bug fixes need a test for the first incorrect boundary, not only a broad
  end-to-end test.
- Compiler and native-runtime fixes include an independent behavioral oracle
  where practical, normally Direct SystemVerilog with Verilator or Icarus.
- LSP regressions exercise the real JSON-RPC path and compiler-owned tooling
  facts; mocked protocol tests alone are insufficient.
- Performance/scaling fixes use bounded permanent fixtures and assert either a
  completion limit or a deterministic fail-closed budget diagnostic.
- Tests must not depend on a developer's untracked file, mutable `/tmp` tree,
  deleted branch, session log, or previously installed ZLang version.
- Fixed source-count totals are not evidence of feature inclusion. Inventory
  checks classify discovered sources and explicit exclusions.
- A skipped release test is a failure. Tool-dependent tests must run in the
  release environment with the pinned tools.

## Candidate and publication gates

The release candidate must be a clean commit descended from the previous tag.
Before its PR is eligible for merge:

1. compare the private handoff with `release/regressions.json` and account for
   every item;
2. run each ledger selector directly;
3. run the complete zero-skip regression twice on the exact committed tree;
4. run static, dependency, license, package, native, EDA, editor-host, and
   reproducibility gates required by `docs/release-process.md`;
5. inspect the exact diff from the previous tag and the generated preflight
   manifest.

After merge, run the non-publishing hosted candidate workflow on exact `main`.
Only its successful commit may be signed and tagged. A tag-triggered failure is
fixed in the next alpha; published or pushed release tags are never rewritten.

## Branch retention and cleanup

No implementation, recovery, or release branch is deleted merely because a PR
merged. It remains recoverable until all of these are true:

1. the public PR merged and protected-main checks passed;
2. the signed tag points to that exact `main` commit;
3. tag-triggered validation and EDA passed;
4. published artifacts were downloaded and replayed successfully;
5. the commit and tag are reachable from the configured local backup; and
6. the handoff and release ledger record the published identity.

Deletion uses an exact expected-tip guard. Never bulk-delete branches by name
pattern, never delete a branch with unique commits, and never delete another
agent's active worktree. Archiving a managed worktree is separate from deleting
its branch and happens only after the work is accounted for.

## Incident recovery

If a branch, fixture, or claimed fix cannot be found, stop release work. Inspect
registered worktrees, local refs, reflogs, dangling objects, archives, and task
handoffs. As soon as content is recovered, create a named recovery branch and
commit the source plus regression. Until it appears in the public candidate
ledger and exact-tree tests, report it as recovered but **not included**.
