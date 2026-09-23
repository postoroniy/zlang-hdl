from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "audit_python_architecture", ROOT / "tools/audit_python_architecture.py"
)
assert SPEC is not None and SPEC.loader is not None
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def test_architecture_audit_is_deterministic_and_reports_cycles(tmp_path: Path) -> None:
    package = tmp_path / "fixture"
    package.mkdir()
    (package / "a.py").write_text(
        "from fixture import b\n"
        "def duplicate(value):\n    return value + 1\n"
        "def _unused():\n    return 2\n"
        "class First:\n"
        "    def shared(self, value):\n        return value * 2\n",
        encoding="utf-8",
    )
    (package / "b.py").write_text(
        "from fixture import a\n"
        "def duplicate(value):\n    return value + 1\n"
        "class Second:\n"
        "    def shared(self, value):\n        return value * 2\n",
        encoding="utf-8",
    )

    first = AUDIT.audit(package)
    second = AUDIT.audit(package)

    assert first == second
    assert first["summary"] == {
        "classes": 2,
        "files": 2,
        "functions": 3,
        "lines": 14,
        "methods": 2,
    }
    assert first["import_cycles"] == [["fixture.a", "fixture.b"]]
    assert first["duplicate_function_names"]["duplicate"] == [
        "fixture.a:duplicate",
        "fixture.b:duplicate",
    ]
    assert len(first["duplicate_bodies"]) == 1
    assert first["duplicate_method_bodies"] == [[
        "fixture.a:First.shared",
        "fixture.b:Second.shared",
    ]]
    assert first["potential_dead_private_definitions"] == [
        {"lines": 2, "name": "fixture.a:_unused"}
    ]


def test_compiler_architecture_regressions_remain_closed() -> None:
    report = AUDIT.audit(ROOT / "zlang")

    assert report["duplicate_bodies"] == []
    assert not any(
        "zlang.ir.traversal" in component
        and "zlang.ir.functional" in component
        for component in report["import_cycles"]
    )

    traversal = (ROOT / "zlang/ir/traversal.py").read_text(encoding="utf-8")
    arena = (ROOT / "zlang/ir/expression_arena.py").read_text(encoding="utf-8")
    assert "zlang.ir.functional" not in traversal
    assert "return repr(value)" not in arena
    assert report["unallowlisted_repr_identity_sites"] == ()

    private_candidates = {
        item["name"] for item in report["potential_dead_private_definitions"]
    }
    # Kept as a tested backend formatter despite its private historical name.
    assert private_candidates <= {"zlang.backend.systemverilog.contracts:_emit_constant"}
