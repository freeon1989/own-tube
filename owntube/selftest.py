# -*- coding: utf-8 -*-
"""Самопроверка сборки без окна: own-tube.exe --selftest report.json"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from . import __version__, player, search, sponsorblock, validate
from .db import Store
from .network import Route
from .tools import find_tool


def _version(exe: str | None, arg: str) -> str:
    if not exe:
        raise FileNotFoundError("не найден ни в tools\\, ни в PATH")
    flags = 0x08000000 if sys.platform.startswith("win") else 0
    out = subprocess.run([exe, arg], capture_output=True, timeout=60, shell=False, creationflags=flags)
    text = (out.stdout or out.stderr).decode("utf-8", "replace").strip().splitlines()
    return text[0][:120] if text else f"код {out.returncode}"


def run(path: str) -> int:
    report: dict = {"version": __version__, "python": sys.version.split()[0], "frozen": bool(getattr(sys, "frozen", False))}
    ok = True

    def step(name, fn):
        nonlocal ok
        t = time.time()
        try:
            report[name] = {"ok": True, "result": fn(), "sec": round(time.time() - t, 1)}
        except Exception as exc:
            ok = False
            report[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400], "trace": traceback.format_exc()[-800:]}

    for tool, arg in (("mpv", "--version"), ("ffmpeg", "-version"), ("deno", "--version")):
        step(tool, lambda t=tool, a=arg: {"path": find_tool(t), "version": _version(find_tool(t), a)})
    route = Route()
    step("search", lambda: [i["title"][:40] for i in search.search("lofi hip hop", route, 3)])
    step("stream_720_h264", lambda: validate.is_stream_url(player.resolve_streams("yt", "jNQXAC9IVRw", "720", route, "h264")["video"]))
    step("sponsorblock", lambda: len(sponsorblock.segments("jNQXAC9IVRw", ["sponsor"], route)) >= 0)
    step("ui_files", lambda: sorted(p.name for p in (player.resource_dir() / "owntube" / "ui").glob("*.*")))
    step("skip_script", lambda: player.SKIP_SCRIPT.is_file())

    def dbtest():
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            s.add_history({"source": "yt", "id": "jNQXAC9IVRw", "title": "t"})
            n = len(s.history())
            s.close()
            return n

    step("sqlite", dbtest)
    report["ok"] = ok
    Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ok else 1
