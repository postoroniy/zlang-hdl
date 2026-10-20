# Public Language Reference sources

The numbered Markdown files in this directory are the authoritative public
inputs for `ZLang-HDL-Language-Reference.pdf`. Their filenames define the
reviewed chapter order; `appendix-quick-reference.md` is the final appendix.

Cross-chapter `#reference-...` links resolve in that assembled document, not
in an individual chapter file. For web reading, use the complete
[language reference](../language-reference.md) and
[quick reference](../language-quick-reference.md).

The PDF builder is host-owned and is not shipped in the public source tree.
It accepts only its explicit source list. Before Pandoc runs, it assembles
one deterministic Markdown projection, checks the
15-chapter contract, and rejects known local-path, worktree, branch, task,
unfinished-note, and credential markers. Other documentation may retain
engineering or historical material, but it is not implicitly included in the
PDF.

Use `make community-pdf-check` in a public checkout to verify the reviewed PDF
and its source/geometry identities against `release/status.json`. PDF building
and visual review occur separately; hash validation is not visual validation.
