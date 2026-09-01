from __future__ import annotations

from pathlib import Path

from zlang.common.content_cache import load_json_object, publish_json_atomically


def test_atomic_json_publication_replaces_complete_object(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "entry.json"
    publish_json_atomically(path, {"generation": 1, "values": [3, 2, 1]})
    first, diagnostic = load_json_object(path)
    assert diagnostic is None
    assert first == {"generation": 1, "values": [3, 2, 1]}

    publish_json_atomically(path, {"generation": 2})
    second, diagnostic = load_json_object(path)
    assert diagnostic is None
    assert second == {"generation": 2}
    assert tuple(path.parent.glob(f".{path.name}.*.tmp")) == ()


def test_json_cache_load_distinguishes_missing_corrupt_and_non_object(
    tmp_path: Path,
) -> None:
    missing, diagnostic = load_json_object(tmp_path / "missing.json")
    assert missing is None and diagnostic is None

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{\n", encoding="utf-8")
    value, diagnostic = load_json_object(corrupt)
    assert value is None
    assert diagnostic is not None and "invalid JSON" in diagnostic

    array = tmp_path / "array.json"
    array.write_text("[]\n", encoding="utf-8")
    value, diagnostic = load_json_object(array)
    assert value is None
    assert diagnostic == "cache entry must be a JSON object with string keys"

