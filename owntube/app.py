# -*- coding: utf-8 -*-
"""Главное окно: pywebview (на Windows это Edge WebView2)."""
from __future__ import annotations

import argparse
import logging
import sys
import threading

from . import __version__, motw
from .api import Api
from .settings import data_dir
from .validate import RedactFilter
from .tools import resource_dir

UI_DIR = resource_dir() / "owntube" / "ui"


def setup_logging(debug: bool = False) -> None:
    """Журнал own-tube.log в папке данных; секреты вырезает RedactFilter. Нужен и окну входа (отдельный процесс)."""
    log_dir = data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(log_dir / "own-tube.log", encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO,
                        format="%(asctime)s %(name)s: %(message)s", handlers=handlers)
    _redact = RedactFilter()
    logging.getLogger().addFilter(_redact)
    for handler in logging.getLogger().handlers:
        handler.addFilter(_redact)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="own-tube")
    parser.add_argument("--debug", action="store_true", help="включить инструменты разработчика")
    parser.add_argument("--version", action="version", version=f"own-tube {__version__}")
    parser.add_argument("--selftest", metavar="FILE", help="проверить поиск, потоки и программы без окна; отчёт в FILE")
    parser.add_argument("--login", choices=("google", "vk"), help=argparse.SUPPRESS)
    parser.add_argument("--apply-update", metavar="PLAN", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    # до любого импорта .NET: снять пометку «из интернета» с файлов программы (см. motw.py)
    strip_result = motw.strip_app_dir()
    if args.apply_update:
        from .updater import apply_main
        return apply_main(args.apply_update)
    if args.selftest:
        from .selftest import run
        return run(args.selftest)
    setup_logging(args.debug)
    if args.login:
        from .login_window import run as login_run
        return login_run(args.login)

    try:
        import webview  # импорт здесь: тесты и скриншоты обходятся без окна
        return _run_window(webview, args, strip_result)
    except Exception as exc:
        if not getattr(sys, "frozen", False):
            raise
        logging.getLogger("owntube.app").exception("Окно не запустилось")
        motw.show_error(motw.friendly_message(exc, strip_result))
        return 1


def _run_window(webview, args, strip_result) -> int:
    if getattr(sys, "frozen", False):
        from .updater import cleanup_staging
        threading.Thread(target=cleanup_staging, daemon=True).start()
    api = Api()
    window = webview.create_window(f"own-tube {__version__}", str(UI_DIR / "index.html"), js_api=api,
                                   width=1440, height=900, min_size=(960, 620), background_color="#09081a")
    api._attach(window)
    api.warmup()
    webview.start(debug=args.debug, http_server=True, private_mode=True)
    api._shutdown()
    return 0
