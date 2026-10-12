from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / "tools/python_source_audit.py"


def _load_audit():
    spec = importlib.util.spec_from_file_location("python_source_audit", AUDIT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_source_audit_rejects_only_substantial_exact_duplicates(
    tmp_path: Path,
) -> None:
    audit = _load_audit()
    repeated = "\n".join(f"    value += {index}" for index in range(16))
    (tmp_path / "sample.py").write_text(
        f"def first(value):\n{repeated}\n    return value\n\n"
        f"def second(value):\n{repeated}\n    return value\n\n"
        "def tiny(value):\n    return value\n",
        encoding="utf-8",
    )
    records = audit.function_bodies(tmp_path)
    groups = audit.duplicate_groups(records)
    assert [[item.name for item in group] for group in groups] == [
        ["first", "second"]
    ]


def test_repository_has_no_substantial_exact_function_body_duplicates() -> None:
    audit = _load_audit()
    assert audit.duplicate_groups(audit.function_bodies(ROOT / "zlang")) == ()
