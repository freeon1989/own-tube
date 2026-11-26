# -*- coding: utf-8 -*-
"""Главное окно: pywebview (на Windows это Edge WebView2)."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .api import Api

UI_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "owntube" / "ui"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="own-tube")
    parser.add_argument("--debug", action="store_true", help="включить инструменты разработчика")
    parser.add_argument("--version", action="version", version=f"own-tube {__version__}")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(name)s: %(message)s")

    import webview  # импорт здесь: тесты и скриншоты обходятся без окна

    api = Api()
    webview.create_window("own-tube", str(UI_DIR / "index.html"), js_api=api,
                          width=1320, height=840, min_size=(900, 600), background_color="#09081a")
    webview.start(debug=args.debug, http_server=True)
    api._player.stop()
    return 0
