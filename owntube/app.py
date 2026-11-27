# -*- coding: utf-8 -*-
"""Главное окно: pywebview (на Windows это Edge WebView2)."""
from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .api import Api
from .settings import data_dir
from .tools import resource_dir

UI_DIR = resource_dir() / "owntube" / "ui"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="own-tube")
    parser.add_argument("--debug", action="store_true", help="включить инструменты разработчика")
    parser.add_argument("--version", action="version", version=f"own-tube {__version__}")
    parser.add_argument("--selftest", metavar="FILE", help="проверить поиск, потоки и программы без окна; отчёт в FILE")
    args = parser.parse_args(argv)
    if args.selftest:
        from .selftest import run
        return run(args.selftest)
    log_dir = data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(log_dir / "own-tube.log", encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(name)s: %(message)s", handlers=handlers)

    import webview  # импорт здесь: тесты и скриншоты обходятся без окна

    api = Api()
    window = webview.create_window(f"own-tube {__version__}", str(UI_DIR / "index.html"), js_api=api,
                                   width=1440, height=900, min_size=(960, 620), background_color="#09081a")
    api._attach(window)
    webview.start(debug=args.debug, http_server=True, private_mode=True)
    api._shutdown()
    return 0
