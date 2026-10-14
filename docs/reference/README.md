# Public Language Reference sources

The numbered Markdown files in this directory are the authoritative public
inputs for `ZLang-HDL-Language-Reference.pdf`. Their filenames define the
reviewed chapter order; `appendix-quick-reference.md` is the final appendix.

`tools/build_community_pdf.py` accepts only its explicit source list. Before
Pandoc runs, it assembles one deterministic Markdown projection, checks the
15-chapter contract, and rejects known local-path, worktree, branch, task,
unfinished-note, and credential markers. Other documentation may retain
engineering or historical material, but it is not implicitly included in the
PDF.
