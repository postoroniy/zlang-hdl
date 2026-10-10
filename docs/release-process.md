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

This is the canonical operator procedure. `RELEASING.md` records additional
hosted-policy detail, but does not replace these ordered gates. Do not substitute
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

### 3. Fail fast before the long regressions

Run the bounded release sanity gate as soon as the public projection is
internally consistent:

```bash
make release-sanity \
  TAG=v0.1.0a21 \
  PREVIOUS_TAG=v0.1.0a20
```

This validates the regression ledger, PDF/status identity, exported Community
surface, workflow structure and execution mode, static checks, locked editor
tests, and a fresh complete npm advisory report. It deliberately runs before
the two full zero-skip suites. A failure here is cheaper to diagnose and must
not be bypassed by starting the long regression manually.

On the public review branch, `make review-commits` additionally requires every
commit after `origin/main` to have a valid cryptographic signature and an exact
author DCO trailer. Private integration commits are outside this rule; private
Git history never enters the public projection.

### 4. Run the pre-merge review gate

On the clean, committed public review branch, run:

```bash
make venv
make release-review \
  TAG=v0.1.0a21 \
  PREVIOUS_TAG=v0.1.0a20
```

`release-review` rejects local modifications and untracked files, verifies the
review-branch signatures and DCO trailers, runs `release-sanity`, and then runs
two independent zero-unexpected-skip suites against a freshly exported source
tree and its own venv. It deliberately does **not** claim candidate readiness
and does not publish, tag, push, install a global package, or use a sibling
checkout.

### 5. Merge, then run the exact-main candidate gate

After review and required protected-main checks, merge the public candidate.
The manual hosted Release workflow must be dispatched with the exact new tag and
previous tag. Candidate mode explicitly requires checked-out `main` to equal
`origin/main`; running `make release-preflight` or `make release-candidate` on
a review branch is expected to fail. This prevents an arbitrary feature branch
from being certified as releasable.

### 6. Publish only after hosted evidence

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
  TAG=v0.1.0a21 \
  PREVIOUS_TAG=v0.1.0a20
```

After merge, exact protected `main` may run the stricter local candidate command
when its required host tools are available:

```bash
make release-candidate \
  TAG=v0.1.0a21 \
  PREVIOUS_TAG=v0.1.0a20 \
  BUILD_ROOT=build/a21-local \
  EDITOR_VSIX=build/a21-editor/zlang-hdl-0.1.0.vsix
```

Neither command commits, tags, pushes, or publishes. The candidate command runs
the exact-main identity preflight plus native audits, external-tool inventory,
an installed-VSIX host smoke, and reproducible package construction in addition
to the review checks.

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

## Release incident guard matrix

These guards encode failures observed during recent alpha preparation. Add a
new row and a permanent regression whenever another release-only failure is
found; do not leave the recovery solely in shell history or chat notes.

| Observed failure | Root cause | Preventive owner/gate | Recovery |
| --- | --- | --- | --- |
| A hosted job could not import a repository `tools` module | a package-aware tool was executed as a script; local `PYTHONPATH` masked the error | workflows and Make use module execution; the workflow audit rejects the direct hosted form; public checks run from the exported root | change the invocation, run `make release-sanity`, and rerun the failed hosted job |
| GitHub rejected `release.yml` before jobs started | duplicate top-level YAML key | the workflow structure audit runs in `make static` and `release-sanity` | remove the duplicate key and rerun static checks before pushing |
| A split deterministic/performance run passed but failed the aggregate test floor | the full-suite minimum was applied to the non-performance JUnit alone | `release_status` combines distinct JUnit reports; CI, EDA, daily and Release gates validate both partitions together | retain both reports and validate them in one aggregate command; never lower the release floor |
| The VSIX lock contained a newly disclosed vulnerable transitive package | editor tests did not perform a complete current advisory query | `editor-advisory-audit` validates npm exit semantics, report schema, dev-tool coverage and exact lock inventory locally and in Release | update the lock intentionally, rerun editor tests/audit, then rebuild the VSIX |
| The extension was packaged against a stale VS Code host assumption | editor unit tests did not exercise the pinned stable host | `editor-host-test` installs the exact built VSIX into the pinned host and checks navigation/LSP startup | update the engine/test host deliberately, rerun unit plus installed-host tests, then rebuild the VSIX |
| `npm run package` failed with `EEXIST` or reused stale bytes | output path already existed | packaging targets reject an existing `EDITOR_VSIX` or `BUILD_ROOT` | choose a fresh ignored build path; never overwrite review evidence |
| The wrong ZLang version appeared in PDF or release metadata | Markdown, PDF, status and package versions were updated independently | PDF/status, staging and preflight checks bind source, cover, builder, package and tag identities | rebuild from the exact final public tree and update all bound metadata together |
| Shipped changes remained under `Unreleased`, or the release date predated the candidate | notes were prepared before the exact release commit | preflight requires an empty `Unreleased`, an exact dated version heading and a date matching the candidate commit | move shipped entries into the version section and make the final metadata commit on the release date |
| A public PR commit failed DCO or signature validation | the public commit used private/default Git settings | `make review-commits` checks every commit after `origin/main`; hosted checks remain authoritative | recreate the public commit with the maintainer key and exact author `Signed-off-by` trailer |
| A command imported an installed or sibling ZLang | an ambient or sibling venv was active | `env-check`, the local runner and hosted local-venv audit require the checkout-owned `.venv` | deactivate, run `make venv`, activate this checkout's `.venv`, and rerun |
| Concurrent tests collided or an EDA tool hung on an unsuitable path | shared hard-coded `/tmp` names or uncontrolled long paths | the local runner creates unique `build/tmp/<purpose>.XXXXXX`; EDA gates use bounded worktree-local scratch | rerun with `KEEP_TMP=1` only for diagnosis |
| OSV findings were mistaken for infrastructure failure | exit code 1 was rejected before parsing findings | the native vulnerability audit validates report/exit consistency, coverage and exact reviewed exceptions | fix the dependency or add an exact reviewed non-expired exception; never normalize the scanner exit |
| Candidate validation ran on a feature branch or stale commit | ancestry was treated as equivalent to protected exact `main` | preflight requires selected ref `main`, `HEAD == origin/main`, immediate alpha sequence and no prospective tag | merge the reviewed PR, fetch, and dispatch on exact protected `main` |
| Release artifacts became stale after a final source or metadata fix | wheel, PDF, VSIX, checksum or SBOM bytes preceded final HEAD | fresh-path guards, exact-tree identities, reproducible builds and hosted preflight bind artifacts to final HEAD | discard ignored outputs and rebuild every artifact from the new exact tree |
| A public projection carried private history or files | a private checkout was treated as the public repository | history-isolated public checkout plus the public-tree allow-list, manifest and content scan | recreate from public `origin/main` and transfer reviewed source slices only |
| A projection restored old god modules and lost accepted owner splits | it started from a stale public layout | private source-of-truth review, architecture audits and exact public-tree comparison precede projection | discard the stale projection and transfer the reviewed current owner slice |
| A private witness or local artifact appeared in release review | an untracked/private path was copied instead of selected by policy | public-tree exclusions, closure checks and generated manifest define the release surface | remove the path, regenerate the manifest and rerun `public-check` |
| EDA behavior differed between local and hosted runs | ambient tools replaced the pinned OSS CAD Suite identity | release status, EDA workflows and formal evidence bind exact tool/config identities | activate the pinned suite, remove ambient overrides and rerun the affected gate |
| Feature-branch preflight failed | candidate mode correctly requires protected exact `main` | `release-review` is the pre-merge gate; `release-candidate` is post-merge only | use `release-review` before merge; do not weaken exact-main preflight |
| A full suite was not replayed after a final change | validated and proposed trees differed | pre-merge and hosted Release gates retain exact-HEAD JUnit evidence | rerun the affected gate and both complete suites on final exact HEAD |

The matrix describes recovery, not exemptions. A failure stays failed until its
authoritative gate passes on the exact candidate tree.

## Hosted pre-tag gate

After the release PR is reviewed and its required checks pass, merge it. Run the
`Release` workflow manually on the exact `main` commit with:

- `tag`: the prospective tag, for example `v0.1.0a21`;
- `previous_tag`: the exact prior release, for example `v0.1.0a20`.

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
- LSP regressions exercise real JSON-RPC and compiler-owned tooling facts;
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
