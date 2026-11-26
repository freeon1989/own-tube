# -*- coding: utf-8 -*-
"""Снимок реальной выдачи для макета: owntube/ui/mockup-data.js (только публичные данные поиска)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from owntube.search import search  # noqa: E402

items = search(sys.argv[1] if len(sys.argv) > 1 else "аниме опенинг", limit=18)
(ROOT / "owntube" / "ui" / "mockup-data.js").write_text(
    "window.MOCK_FEED = " + json.dumps(items, ensure_ascii=False, indent=1) + ";\n", encoding="utf-8")
print(len(items), "items")
