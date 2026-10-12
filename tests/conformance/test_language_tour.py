"""One broad positive gate over every independent language-tour top."""

from __future__ import annotations

import pytest

from tests.conformance.catalog import (
    EXPECTED_LANGUAGE_TOUR_TOPS,
    LANGUAGE_TOUR_PATH,
    language_tour_source,
    language_tour_tops,
)
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.opt import OptimizationStage, lower, restore


@pytest.mark.conformance
def test_every_language_tour_top_has_one_complete_positive_compiler_path() -> None:
    source = language_tour_source()
    assert language_tour_tops() == EXPECTED_LANGUAGE_TOUR_TOPS

    for top in EXPECTED_LANGUAGE_TOUR_TOPS:
        result = compile_source(
            source,
            top=top,
            source_unit=str(LANGUAGE_TOUR_PATH),
        )
        assert restore(
            lower(result.ir, stage=OptimizationStage.HIGH_LEVEL)
        ) == result.ir
        artifact = emit_artifact(result.ir)
        assert artifact.module == top
        assert artifact.to_json()
