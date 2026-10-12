from __future__ import annotations

from pathlib import Path




ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples" / "indexed_instance_array.zhl").read_text()
