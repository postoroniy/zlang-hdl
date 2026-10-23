# ZLang HDL documentation

The public a24 documentation has three manuals. Each uses the same canonical
Markdown chapters as its PDF edition; there are no separately edited PDF chapters.

| Manual | Question |
|---|---|
| [Language Reference](language-reference.md) | What does this code mean? |
| [Standard Library Guide](stdlib.md) | Which component should I use, and how do I connect it? |
| [User Guide](user-guide.md) | How do I install, run and debug a project? |

Start with [two complete workflows](tutorial.md). Use the
[quick reference](language-quick-reference.md) for syntax reminders, not as a
fourth manual. AI guidance routes to these same sources.

[The manifest](manuals.toml) fixes book IDs, source order, entrypoints and stable
PDF names. Keep the three PDFs in one folder for offline inter-book links.
[Previous chapter paths](reference/README.md) are navigation aliases.

## Support and evidence

The [support matrix](syntax-support-matrix.md) and
[known limitations](known-limitations.md) define current fail-closed boundaries.
Source availability does not imply synthesis or universal formal qualification.
[Formal workflows](formal-verification.md) distinguish proven, bounded and
unknown evidence. [Stdlib qualification notes](stdlib-formal-coverage.md) retain
their explicit profile/tool boundaries.

[Release metadata](../release/status.json) owns exact candidate/artifact/tool
identities; it does not establish that a Release exists.
[The release process](release-process.md) requires a fresh review/freeze and
validation of the exact documentation tree. Historical engineering records
remain evidence for their recorded revision, not a second current specification.
