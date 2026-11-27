# -*- coding: utf-8 -*-
"""Скачивает и проверяет внешние программы для сборки под Windows: mpv, ffmpeg, deno.

Версии и SHA-256 закреплены здесь (совпадают с digest ассетов GitHub-релизов и с файлами
контрольных сумм издателей). Если хеш не совпал — сборка останавливается.
Запуск: python scripts/fetch_tools.py <кэш> <папка tools>
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

TOOLS = [
    {
        "name": "mpv",
        "url": "https://github.com/shinchiro/mpv-winbuild-cmake/releases/download/20261111/mpv-x86_64-20261111-git-bcde6b93cc.7z",
        "sha256": "f259b5fd4b5c395617c948a4fadcdf55bba1c5282fb7c13e88626b3f71072baf",
        "files": {"mpv.exe": "mpv.exe"},
    },
    {
        "name": "ffmpeg",
        "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-11-26-12-56/ffmpeg-n8.1.3-41-ga8029b6528-win64-lgpl-8.1.zip",
        "sha256": "5eb24166e7f7103dcc12fa6d773674df9491005e78e8069c131e65b3661343da",
        "files": {"bin/ffmpeg.exe": "ffmpeg.exe", "LICENSE.txt": "ffmpeg-LICENSE.txt"},
    },
    {
        "name": "deno",
        "url": "https://github.com/denoland/deno/releases/download/v2.9.8/deno-x86_64-pc-windows-msvc.zip",
        "sha256": "cba84b26e0df5ae7f74fc66b86a95b866872a86292ebd84fa1e959927edd93bf",
        "files": {"deno.exe": "deno.exe"},
    },
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(tool: dict, cache: Path) -> Path:
    dest = cache / tool["url"].rsplit("/", 1)[1]
    if not dest.exists() or sha256(dest) != tool["sha256"]:
        print("скачиваю", tool["url"])
        tmp = dest.with_suffix(dest.suffix + ".partial")
        with urllib.request.urlopen(tool["url"], timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        tmp.replace(dest)
    got = sha256(dest)
    if got != tool["sha256"]:
        raise SystemExit(f"{tool['name']}: SHA-256 не совпал ({got}), сборка остановлена")
    print(f"{tool['name']}: SHA-256 совпал {got}")
    return dest


def extract(archive: Path, wanted: dict, out: Path) -> None:
    def match(name: str):
        norm = name.replace("\\", "/")
        for src, dst in wanted.items():
            if norm == src or norm.endswith("/" + src):
                return dst
        return None

    found = set()
    if archive.suffix == ".7z":
        import os
        import subprocess
        import tempfile

        seven = os.environ.get("SEVENZIP") or shutil.which("7zz") or shutil.which("7z")
        if not seven:
            raise SystemExit("нужен 7-Zip (7zz или 7z) для .7z; укажите путь в SEVENZIP")
        with tempfile.TemporaryDirectory() as tmp:
            for src, dst in wanted.items():
                subprocess.run([seven, "e", "-y", "-o" + tmp, str(archive), src], check=True,
                               stdout=subprocess.DEVNULL, shell=False)
                got = Path(tmp) / Path(src).name
                if got.is_file():
                    shutil.copyfile(got, out / dst)
                    found.add(dst)
    else:
        with zipfile.ZipFile(archive) as z:
            for n in z.namelist():
                dst = match(n)
                if dst:
                    (out / dst).write_bytes(z.read(n))
                    found.add(dst)
    missing = set(wanted.values()) - found
    if missing:
        raise SystemExit(f"в {archive.name} нет {missing}")


def main() -> None:
    cache, out = Path(sys.argv[1]), Path(sys.argv[2])
    cache.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    lines = []
    for t in TOOLS:
        a = fetch(t, cache)
        extract(a, t["files"], out)
        lines.append(f"{t['name']}: {t['url']}\n  sha256 архива: {t['sha256']}")
    for exe in sorted(out.glob("*.exe")):
        if exe.read_bytes()[:2] != b"MZ":
            raise SystemExit(f"{exe.name}: не похож на exe")
        lines.append(f"{exe.name}: sha256 {sha256(exe)}")
    (out / "SOURCES.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
