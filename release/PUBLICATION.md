# Public snapshot publication

The private development checkout is never pushed to GitHub.  GitHub `main` is a
compact, linear public projection created from `release/public-tree.toml`.  It
has its own orphan history: no private development commit is an ancestor of a
public commit.

## One-time public repository

Create a separate directory with a fresh `git init --initial-branch=main`; never
use a linked worktree of the private repository. A linked worktree shares its
object database, refs, configuration, remotes, and ancestry and is therefore not
an isolation boundary. Make the first public commit as the fresh repository's
single root. Do not add the GitHub remote to the private checkout. Review the
exported file manifest before the first commit, then configure only the separate
repository to use the exact public origin URL. This setup is intentionally
manual; the exporter never creates repositories, branches, remotes, commits,
tags, or pushes.

Before the first push, require all of the following in the public repository:

```bash
test -d .git
test "$(git remote get-url origin)" = \
  "https://github.com/postoroniy/zlang-hdl.git"
test "$(git rev-list --max-parents=0 main | wc -l)" -eq 1
test "$(git remote | wc -l)" -eq 1
test "$(git for-each-ref --format='%(refname)' refs/heads/)" = refs/heads/main
test -z "$(git for-each-ref --format='%(refname)' refs/tags/)"
test -z "$(git for-each-ref --format='%(refname)' refs/remotes/ | \
  grep -Ev '^refs/remotes/origin/(main|HEAD)$' || true)"
```

Inspect `git fsck --no-reflogs --unreachable` before publishing; the fresh
repository must contain no private-development object or ref.

After the initial commit and before the one-time root push, scan the complete new Git
history with the pinned Gitleaks 8.24.3 release. Verify the downloaded archive
against the vendor checksum
`9991e0b2903da4c8f6122b5c3186448b927a5da4deef1fe45271c3793f4ee29c`,
and require `gitleaks version` to print `8.24.3`. Before trusting the scanner,
run it against a temporary fixture containing a fake PEM private-key block and
require the configured detector exit status `99`. Then run:

```bash
gitleaks git . --redact --no-banner
```

Record the clean result in the release evidence. The exporter's regex scan is a
narrow fail-closed backstop, not a substitute for this history scan. Do not use
Gitleaks 8.30.1: its detector regression can return a false clean result for a
seeded GitHub-token fixture.

The explicit `HEAD:refs/heads/main` push is permitted only for this initial
root. Once that root is public, it is immutable history: never recreate the
repository, replace the root, force-push `main`, or reset public history to fix
a projection defect.

## Promote a reviewed development snapshot

1. Run the complete release checks in the private development checkout.
2. Export into an empty directory:

   ```bash
   python tools/public_tree.py export --source . --destination /tmp/zlang-public
   python /tmp/zlang-public/tools/public_tree.py check-export \
       --source /tmp/zlang-public
   ```

3. Review the manifest and the complete diff against the separate public
   checkout. Create a named review branch from the **current public `main`
   HEAD**, replace that branch's tracked snapshot, and apply it as one normal
   signed-off linear commit. This preserves every public contribution and its
   DCO audit trail; never reset or rewrite public history to an older release
   commit.
4. Run `tools/public_tree.py check-export --source .` in that final public
   checkout after replacing the old snapshot and before committing. This rejects
   stale files left from an earlier release.
5. Push only the named review branch from the separate public repository, open
   a pull request, and merge it only after the required hosted checks, DCO, and
   review policy pass. Never push a later snapshot directly to `main`; never use
   `--all`, `--mirror`, blanket tag pushes, merge, rebase, or cherry-pick from
   the private repository.

Contributions received on public `main` are first imported into private
development as reviewed patches. A later full snapshot promotion publishes the
result atop the still-current public history without importing private ancestry
or discarding the original public commits.

The exporter rejects symlinks, host-specific paths, secrets, broken local links,
missing Python/ZLang imports, omitted fixtures, and files outside the allow-list.
Its JSON manifest is deterministic and contains no timestamp or local path.

Release automation accepts only a GitHub-verified signed annotated tag.  The
signing private key stays with the maintainer and is never copied into the
repository or Actions secrets.  The self-hosted EDA runner must be Actions
Runner 2.327.1 or newer because the pinned official actions use Node.js 24.
