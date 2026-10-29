# Community release process

Community releases of ZLang HDL use one reviewed source tree and two modes of
the same GitHub `Release` workflow. The candidate mode validates and retains
artifacts but cannot publish. Only a signed annotated tag at the exact protected
`main` commit enables the publish job.

This document defines the public candidate, regression, artifact and publication
gates. A fix is not
release evidence until it is represented in the candidate's
`release/regressions.json` with permanent tests.

## Operator checklist

Use the following ordered gates. Do not substitute
an ambient Python, an old installed wheel, an arbitrary Git branch, or an
untracked reproducer for one of these checks.

### 1. Establish the correct release tree

- Prepare the candidate from current public `origin/main` on a review branch.
  Include only reviewed public changes and permanent regression fixtures.
- Before review, verify the candidate's base and previous published-tag
  ancestry. Review the exact diff, dependency inventory and publication surface;
  exclude credentials, local host paths, internal reports and untracked work.
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
| documentation | all three manifest-owned manuals and their **rendered covers** identify the same version; schema-3 status records each PDF, cover, sources and common build recipe |
| release surface | `make public-check` validates the Community metadata; the maintainer's projection gate additionally verifies the approved file set and content before publication |
| reviewability | the exact public diff, regression ledger and retained test artifacts account for the candidate's changes |

The PDF builder is host-owned. `docs/manuals.toml` owns the three book IDs,
canonical chapter order, Markdown entrypoints and stable PDF filenames.
Build all three PDFs twice against the exact exported public snapshot with
the same recipe and epoch, require byte identity, and record builder/cover/source
digests plus page/cover geometry. Render every page with Poppler at no less than
150 DPI. Human review must
cover the cover, contents, ordinary prose, every table and code block, logic
state, registered outputs, implementation intent/II, formal evidence,
capability matrices, and the quick-reference appendix. The deterministic render
smoke checks representative pages and obvious edge overflow; a matching PDF
hash proves only that the reviewed bytes were retained and never replaces this
full visual review. Keep the three PDFs together and check offline named-destination
links between them. JavaScript and Launch actions are forbidden. A missing,
stale or wrongly mapped book blocks `community-pdf-check`, preflight and staging.

The stable asset names are `ZLang-HDL-Language-Reference.pdf`,
`ZLang-HDL-Standard-Library-Guide.pdf` and `ZLang-HDL-User-Guide.pdf`.
Do not rename assets in existing published releases. Version and exact build
identities belong inside the new books and release metadata, not their filenames.

### 3. Fail fast before the long regressions

Run the bounded release sanity gate as soon as the public projection is
internally consistent:

```bash
make release-sanity
```

The Makefile derives `TAG` from the checked-out package version. After the
history-isolated repository bootstrap, it derives `PREVIOUS_TAG` from the
nearest reachable release tag. The one-commit bootstrap candidate records a
null baseline and leaves `PREVIOUS_TAG` empty; preflight rejects that mode for
any repository with more than one commit.

`release-sanity` also runs `release-dependency-floor`. That check compares
exact Python, editor lockfile, pinned GitHub Action, build-tool, and EDA
versions with `PREVIOUS_TAG` and fails on a downgrade. It is explicitly not
applicable to the one-commit bootstrap because no Git baseline exists. A later
source update therefore cannot silently replace a newer public
dependency update with an older pin. An intentional downgrade
requires a separately reviewed release-policy change; it must never be
smuggled through by projection.

This validates the regression ledger, PDF/status identity, exported Community
surface, workflow structure and execution mode, static checks, locked editor
tests, and a fresh complete npm advisory report. It deliberately runs before
the two full zero-skip suites. A failure here is cheaper to diagnose and must
not be bypassed by starting the long regression manually.

Hosted jobs do not own compiler commands. They provision the requested host,
install pinned external EDA actions, and transport retained artifacts; the
repository-owned `ci-*` Make targets own dependency installation, test
partitioning, status validation, random seeds, editor validation, and the
split-suite floor. `make ci-contract-smoke` checks that each migrated hosted job
invokes its exact target and rejects direct pip, pytest, or release-status
commands in those jobs. This keeps a command reproducible locally instead of
maintaining a second implementation inside workflow YAML.

On the review branch, `make review-commits` additionally requires every
commit after `origin/main` to have a valid cryptographic signature and an exact
author DCO trailer.

### 4. Run the pre-merge review gate

On the clean, committed public review branch, run:

```bash
make venv
make release-review
```

`release-review` rejects local modifications and untracked files, verifies the
review-branch signatures and DCO trailers, runs `release-sanity`, replays the
exact `ci-fast-core` and `ci-editor` pull-request lanes, and then runs
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
replay, exact-tag checks, durable evidence archive, and regression-ledger review
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
make release-review
```

After merge, exact protected `main` may run the stricter local candidate command
when its required host tools are available:

```bash
make release-candidate
```

Neither command commits, tags, pushes, or publishes. The candidate command runs
the exact-main identity preflight plus native audits, external-tool inventory,
an installed-VSIX host smoke, and reproducible package construction in addition
to the review checks.

For waveform changes, run `make editor-waveform-test` and review its retained
screenshots under `build/tmp/waveform-browser.*`. Human visual acceptance of
empty, normal, dense, U/X/Z, and split-viewport views is mandatory before
publication; passing pixel/query assertions is not visual acceptance. The
Linux-x64 installed-host gate also exercises saved VCD/FST and live stepping.

Release commands never borrow a sibling worktree's interpreter. Every run uses
the candidate's `.venv` and creates collision-safe transient files below that
candidate's `build/tmp`; `KEEP_TMP=1` retains a failed run's scratch directory.

`tools/release_preflight.py` composes existing authoritative validators. Its
deterministic JSON binds the package version, prospective tag, previous tag,
Git commit/tree, changelog notes, the manual manifest and three reviewed PDFs, editor lock/package, release
status, regression ledger and audited native wheel. It never creates a tag or
release.

The Community tree carries the canonical Markdown chapters, manifest and three
reviewed PDF artifacts. `make community-pdf-check` validates every PDF digest,
page count, metadata and source identity recorded in schema-3 `release/status.json`.
PDF authoring is host-owned: build the complete set against the exact exported
public snapshot, recording the audited build-source digests, before committing it.
Assets are release-maintainer inputs and are not advertised as a public build
command when they are absent from the Community tree.

## Release incident guard matrix

These guards encode failures observed during recent alpha preparation. Add a
new row and a permanent regression whenever another release-only failure is
found; do not leave the recovery solely in shell history or chat notes.

| Observed failure | Root cause | Preventive owner/gate | Recovery |
| --- | --- | --- | --- |
| GitHub rejected `release.yml` before jobs started | duplicate top-level YAML key, previously a second `env:` | `audit_workflow_structure.py`, run by `make static` and `release-sanity` | remove the duplicate key and rerun static checks before pushing |
| A split deterministic/performance run passed but failed the aggregate test floor | the full-suite minimum was applied to the non-performance JUnit alone | `ci-test-floor` combines two distinct JUnit reports through the dependency-free `check-junit` command; CI, EDA, daily and Release use that same target | retain both reports and call `make ci-test-floor`; never lower or apply the full floor to one partition |
| The aggregate test-floor job could not import ZLang, or the narrow validator leaked into a prepared lane | workflow YAML duplicated visually similar full-status and report-only commands | `ci-full-regression` owns prepared compiler/tool validation; `ci-test-floor` runs under `python -S`; `ci-contract-smoke` enforces their distinct job ownership | fix the Make target or its contract test, not a one-off workflow command |
| Hosted and local release lanes used different dependency pins or commands | pip/pytest commands were copied into several workflows and drifted independently | project test dependencies live in `pyproject.toml`, release-tool pins live in `Makefile`, and migrated jobs may invoke only canonical `ci-*`/release Make targets | update one authoritative pin/target, run `make ci-contract-smoke`, then rerun the affected hosted lane |
| An update downgraded egglog, VS Code LSP dependencies, or pinned workflow actions | stale dependency pins replaced newer accepted versions | `release-dependency-floor`, `release-sanity`, and release preflight compare all release-owned exact pins with the previous public tag | restore at least the previous public floors and rebuild affected artifacts from the corrected exact tree |
| Every hosted lane failed while bootstrapping `reuse` with `Cannot import 'poetry.core.masonry.api'` | one `--no-build-isolation` pip invocation combined the local editable project with external release-tool sdists, so the external Poetry backend was unavailable in a clean venv | `make venv` installs the local editable project and external release tools in separate commands; the bootstrap contract requires normal PEP 517 isolation for the latter, and a clean-snapshot bootstrap is the release rehearsal | keep `--no-build-isolation` scoped only to the local setuptools-backed editable install; reproduce from a checkout with no `.venv` before rerunning hosted lanes |
| The VSIX lock contained newly disclosed vulnerable transitive packages | editor tests did not perform a complete current advisory query | `editor-advisory-audit` plus `audit_editor_vulnerabilities.py` validate npm exit semantics, report schema, dev-tool coverage, and exact lock inventory locally and in Release | update the lock intentionally, rerun editor tests/audit, then rebuild the VSIX |
| The extension was packaged against a stale VS Code host assumption | editor unit tests did not exercise the currently pinned stable host | `editor-host-test` installs the exact built VSIX into the pinned hosted stable version and checks navigation/LSP startup; release status binds the reviewed host/tool identity | update the engine/test host deliberately, rerun unit plus installed-host tests, then rebuild the VSIX |
| `npm run package` failed with `EEXIST` or reused stale bytes | output path already existed | packaging targets reject an existing `EDITOR_VSIX` or `BUILD_ROOT`; examples use versioned fresh paths | choose a fresh ignored build path; never overwrite review evidence |
| The wrong ZLang version appeared in the PDF or release metadata | Markdown/PDF/status/package versions were updated independently | `community-pdf-check`, `release_status`, `stage_release_pdf`, and preflight bind source, cover, builder, PDF, package and tag identities | rebuild the PDF from the exact final public tree and update all bound metadata together |
| The PDF cover changed while hashes and reproducibility remained green | a rasterized prior PDF cover was supplied as the next release's source asset and then cropped as if it were the reviewed banner | `release_status` extracts the sole page-one raster and checks its digest/dimensions, reads every page MediaBox through Poppler, and retains the mandatory all-page visual review | restore the reviewed banner asset, rebuild twice, render every page, and update metadata only after visual acceptance |
| Shipped changes remained under `Unreleased`, or the release date predated the final candidate | release notes were prepared before the exact release commit | release preflight requires an empty `Unreleased`, an exact dated version heading, and a date matching the exact candidate commit | move shipped entries into the exact version section and make a final metadata commit dated for the release candidate |
| A public PR commit failed DCO or was not cryptographically signed | the commit used incorrect Git signing settings | `make review-commits` checks every commit after `origin/main`; hosted DCO/signature checks remain authoritative | amend or recreate the public commit with the maintainer's signing key and exact author `Signed-off-by` trailer |
| A command imported an installed or sibling ZLang | ambient or sibling venv was active | `env-check`, `run_local_env.sh`, and workflow local-venv audit require the checkout-owned `.venv` and disable user site packages | deactivate, run `make venv`, activate this checkout's `.venv`, and rerun |
| Concurrent tests collided or an EDA tool hung on an unsuitable temp path | shared hard-coded `/tmp` names or long uncontrolled paths | the local runner creates unique `build/tmp/<purpose>.XXXXXX`; EDA gates use bounded worktree-local scratch | rerun with `KEEP_TMP=1` only for diagnosis, then remove retained ignored evidence |
| OSV Scanner findings were mistaken for infrastructure failure | exit code 1 was rejected before parsing the report | `audit_native_vulnerabilities.py` parses exact coverage/findings first and validates exit/report consistency plus reviewed exceptions | fix the dependency or add an exact reviewed non-expired exception; never normalize the scanner exit code |
| Candidate validation ran on a feature branch or stale commit | ancestry was treated as equivalent to protected exact `main` | `release_preflight.py` requires selected ref `main`, `HEAD == origin/main`, immediate alpha sequence, no prospective tag, and previous-tag ancestry | merge the reviewed PR, fetch, and dispatch the non-publishing workflow on exact protected `main` |
| Release artifacts became stale after a last-minute source or metadata fix | wheels, PDF, VSIX, checksums or SBOM were built before final HEAD | fresh-path guards, exact-tree identities, reproducible double builds and hosted preflight bind artifacts to final HEAD | discard ignored build output and rebuild every artifact from the new exact tree |
| Internal reports, credentials or local artifacts appeared in a candidate | the publication inventory was not reviewed | approved file inventory, content scanning and exact public diff review | remove the unintended content, refresh the inventory and rerun the publication checks; assess any exposed credentials separately |
| EDA behavior differed between local and hosted runs | an ambient Yosys, Verilator, Icarus, SymbiYosys, Z3 or other solver replaced the pinned OSS CAD Suite/tool identity | `release/status.json`, `release-tools`, EDA workflow and formal evidence bind exact tool/config identities | activate the pinned suite, remove ambient overrides, and rerun the affected EDA/formal gate |
| A feature-branch `release-preflight` failed | candidate mode correctly requires protected exact `main` | `release-review` is the pre-merge gate; `release-candidate` is post-merge only | use `release-review` before merge; do not weaken exact-main preflight |
| A full suite passed before a final metadata or tooling change but was not replayed | the validated tree differed from the proposed release tree | `release-review` and hosted Release execute the exact committed projection; two zero-skip suites are retained as JUnit evidence | rerun the focused affected gate and both complete suites on final exact HEAD |
| Installed wheel/sdist smoke rejected valid compatibility CLIs after all earlier gates passed | release YAML duplicated CLI display-name expectations and expected executable spellings (`zlang-lock`, `zlang-verify`) instead of the canonical supported display identities (`zlang lock`, `zlang verify`) | `installed_distribution_smoke.py` owns the exact installed CLI contract; the workflow invokes that tool and regression tests reject raw duplicated assertions | prepare the next reviewed alpha without rewriting the failed tag, run the package smoke locally, then repeat the hosted candidate and tagged gates |

The matrix describes recovery, not exemptions. A failure stays failed until its
authoritative gate passes on the exact candidate tree.

## Hosted pre-tag gate

After the release PR is reviewed and its required checks pass, merge it. Run the
`Release` workflow manually on the exact `main` commit with:

- `tag`: `v` followed by `release.version` from `release/status.json`;
- `previous_tag`: the exact non-null baseline from `release/regressions.json`,
  or empty for the one-commit bootstrap candidate.

Manual dispatch runs validation and EDA jobs, uploads review artifacts and does
not attest or publish. It checks out `main` explicitly and fails if the
prospective tag already exists, the selected ref is not `main`, or `HEAD`
differs from `origin/main`. With a baseline it also requires the immediate
alpha successor and ancestry; without one it requires exactly one commit.

## Signed publication

Only after the hosted candidate gate passes, create a signed annotated tag at
the exact reviewed `main` commit and push that tag. The tag-triggered workflow
repeats all release checks, verifies GitHub's signature and exact-main identity,
attests the accepted checksums, and publishes the GitHub prerelease.

Never rewrite an existing release tag. If a tag-triggered gate fails after the
tag exists, fix the cause through a reviewed commit and prepare the next alpha.

## Durable fix identity

Each accepted fix receives a stable issue/regression identity and public
evidence that identifies the exact source and tests.

An implementation is not durable until its source fix, required permanent
fixture, focused regression test, and validation record are committed on
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

A fix accepted for the candidate must be `included`; it must never be
silently converted to another disposition. A release must not claim a fix
merely because it existed elsewhere. The public source, tests,
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

1. compare the accepted public changes with `release/regressions.json` and
   account for every fix;
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
backup reachability, and published regression-ledger identity all pass.

Deletion uses an exact expected-tip guard. Never bulk-delete branches by name
pattern or delete a branch with unique commits. Cleaning up a local checkout
must not discard uncommitted changes or the only copy of a regression fixture.

If a branch, fixture, or claimed fix cannot be found, stop release work. Inspect
registered worktrees, local refs, reflogs, dangling objects, archives, and task
validation records. Recover content onto a named branch and commit the source
plus regression. Until it appears in the public candidate ledger and exact-tree
tests, report it as recovered but **not included**.

Before publication, `make community-boundary-check` validates the Community
source payload and reachable history. Check every candidate archive with
`python -m tools.community_boundary --history --artifact PATH`; this supplements
artifact identity/security checks and never replaces them. Unknown executable
bytes and unavailable implementation owners reject. A source review pass does
not close a stale documentation or release-artifact gate.
