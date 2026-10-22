"""Exact initialized ROM observations for compiler regression proofs."""

import re


def assert_readonly_cells(rtl, *, module, name, width, words):
    """Assert exact initialized-cell invariants; never assume or replace cells.

    Needed by temporal induction, whose arbitrary induction state otherwise
    forgets initialization of readonly memories. Basecase checks the real image.
    """
    assert 0 < len(words) <= 4096 and width >= 1
    assert re.fullmatch(r"[A-Za-z_]\w*", name)
    assert all(0 <= word < 1 << width for word in words)
    matches = list(re.finditer(rf"module {re.escape(module)} \(.*?endmodule", rtl, re.S))
    assert len(matches) == 1
    match = matches[0]
    body = match.group()
    declaration = f"logic [{width-1}:0] {name} [0:{len(words)-1}];"
    assert body.count(declaration) == 1
    checks = "always @* begin " + " ".join(
        f"assert({name}[{i}]=={width}'h{word:x});" for i, word in enumerate(words)) + " end\n"
    return rtl[:match.start()]+body.removesuffix("endmodule")+checks+"endmodule"+rtl[match.end():]
