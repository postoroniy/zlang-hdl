# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from tools.audit_review_commits import (
    ReviewCommit,
    ReviewCommitAuditError,
    validate_review_commit,
)


def _commit(*, signature: str = "G", trailer: str | None = None) -> ReviewCommit:
    author_name = "Release Maintainer"
    author_email = "release@example.invalid"
    signoff = trailer or f"Signed-off-by: {author_name} <{author_email}>"
    return ReviewCommit(
        identity="a" * 40,
        author_name=author_name,
        author_email=author_email,
        signature_status=signature,
        message=f"Prepare candidate\n\n{signoff}\n",
    )


@pytest.mark.parametrize("signature", ("G", "U"))
def test_review_commit_accepts_good_signature_and_exact_dco(signature: str) -> None:
    validate_review_commit(_commit(signature=signature))


@pytest.mark.parametrize("signature", ("N", "B", "E", "X", "Y", "R"))
def test_review_commit_rejects_missing_or_invalid_signature(signature: str) -> None:
    with pytest.raises(ReviewCommitAuditError, match="cryptographic signature"):
        validate_review_commit(_commit(signature=signature))


def test_review_commit_rejects_nonmatching_dco_identity() -> None:
    with pytest.raises(ReviewCommitAuditError, match="exact DCO trailer"):
        validate_review_commit(
            _commit(trailer="Signed-off-by: Someone Else <else@example.invalid>")
        )
