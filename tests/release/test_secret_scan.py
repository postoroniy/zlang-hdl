# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Keep the reviewed false-positive exception narrower than a secret rule."""
from __future__ import annotations

from pathlib import Path
import re
import tomllib

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
DIGEST = "0123456789abcdef" * 4


def _exceptions() -> list[dict]:
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
    # A global path allowlist would skip the whole file before rule matching.
    assert set(config) == {"extend", "rules"}
    assert config["extend"] == {"useDefault": True}
    # Merely adding allowlists must not override the inherited detector or
    # disable other default rules. Gitleaks 8.24.3 supports this rule extension.
    assert len(config["rules"]) == 1
    rule = config["rules"][0]
    assert set(rule) == {"id", "allowlists"}
    assert rule["id"] == "generic-api-key"
    assert len(rule["allowlists"]) == 2
    return rule["allowlists"]


def _doc_hash_exception() -> dict:
    exception = _exceptions()[0]
    assert set(exception) == {"description", "condition", "regexTarget", "paths", "regexes"}
    assert exception["condition"] == "AND"
    assert exception["regexTarget"] == "line"
    assert exception["paths"] == [r"^release/status\.json$"]
    assert len(exception["regexes"]) == 1
    return exception


@pytest.mark.parametrize(("path", "match", "allowed"), (
    ("examples/generated/CostMac.synthesis", f"cache_key={DIGEST} ", True),
    ("examples/generated/CostMac.synthesis", f"api_key={DIGEST}", False),
    ("examples/generated/CostMac.synthesis", f"cache_key={DIGEST} api_key={DIGEST}", False),
    ("examples/generated/CostMac.synthesis", f"cache_key={DIGEST}0", False),
    ("other/CostMac.synthesis", f"cache_key={DIGEST} ", False),
))
def test_synthesis_hash_exception_does_not_exempt_other_secrets(path: str, match: str, allowed: bool) -> None:
    exception = _exceptions()[1]
    assert set(exception) == {"description", "condition", "regexTarget", "paths", "regexes"}
    assert exception["condition"] == "AND"
    assert exception["regexTarget"] == "match"
    assert exception["paths"] == [r"^examples/generated/CostMac\.synthesis$"]
    assert len(exception["regexes"]) == 1
    matches = bool(re.search(exception["paths"][0], path) and re.search(exception["regexes"][0], match))
    assert matches is allowed


@pytest.mark.parametrize("name", ("stdlib-api", "tooling-integration-api"))
def test_source_hash_exception_accepts_only_the_documented_fields(name: str) -> None:
    exception = _doc_hash_exception()
    line = f'          "docs/{name}.md": "{DIGEST}",'
    assert re.search(exception["regexes"][0], line)


@pytest.mark.parametrize(("path", "line"), (
    ("release/other.json", f'"docs/stdlib-api.md": "{DIGEST}"'),
    ("other/release/status.json", f'"docs/stdlib-api.md": "{DIGEST}"'),
    ("release/status.json.bak", f'"docs/stdlib-api.md": "{DIGEST}"'),
    ("release/status.json", f'"api_key": "{DIGEST}"'),
    ("release/status.json", f'"docs/other-api.md": "{DIGEST}"'),
    ("release/status.json", '"docs/stdlib-api.md": "not-a-source-hash"'),
    ("release/status.json", f'"docs/stdlib-api.md": "{DIGEST}0"'),
    ("release/status.json", f'"docs/stdlib-api.md": "{DIGEST[:-1]}"'),
    ("release/status.json", f'"docs/stdlib-api.md": "{DIGEST}", "api_key": "{DIGEST}"'),
    ("release/status.json", f'"docs/stdlib-api.md": "{DIGEST}" // api_key'),
))
def test_source_hash_exception_does_not_hide_other_paths_fields_or_secrets(path: str, line: str) -> None:
    exception = _doc_hash_exception()
    path_match = any(re.search(pattern, path) for pattern in exception["paths"])
    line_match = any(re.search(pattern, line) for pattern in exception["regexes"])
    assert not (path_match and line_match)


def test_secret_scan_retains_full_history_self_test_pinning_and_redacted_locations() -> None:
    workflow = yaml.load((ROOT / ".github/workflows/secret-scan.yml").read_text(), Loader=yaml.BaseLoader)
    assert workflow["permissions"] == {"contents": "read"}
    steps = workflow["jobs"]["gitleaks"]["steps"]
    assert steps[0]["with"] == {"fetch-depth": "0", "persist-credentials": "false"}
    step = steps[1]
    assert step["env"]["GITLEAKS_VERSION"] == "8.24.3"
    assert step["env"]["GITLEAKS_ARCHIVE_SHA256"] == (
        "9991e0b2903da4c8f6122b5c3186448b927a5da4deef1fe45271c3793f4ee29c"
    )
    script = step["run"]
    assert "sha256sum -c -" in script
    assert '--exit-code 99 || scan_status=$?' in script
    assert 'if test "$scan_status" -ne 99;' in script
    assert 'exit 1' in script
    assert script.rstrip().endswith('"$binary_dir/gitleaks" git . --redact --no-banner --verbose')
