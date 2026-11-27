# -*- coding: utf-8 -*-
"""0.3.1: снятие пометки «из интернета», own-tube.exe.config и понятная ошибка при сбое pythonnet."""
import os
import re
import sys
from pathlib import Path

import pytest

from owntube import motw, updater

ROOT = Path(__file__).resolve().parents[1]


def tree(base: Path):
    (base / "_internal" / "pythonnet" / "runtime").mkdir(parents=True)
    (base / "tools").mkdir()
    for rel in ("own-tube.exe", "_internal/pythonnet/runtime/Python.Runtime.dll", "tools/ffmpeg.exe", "own-tube.exe.config"):
        (base / rel).write_bytes(b"x")


def test_strip_visits_every_file_and_counts(tmp_path):
    tree(tmp_path)
    seen = []
    marked = {"own-tube.exe", "Python.Runtime.dll", "own-tube.exe.config"}

    def fake(path):
        seen.append(Path(path).name)
        n = Path(path).name
        return True if n in marked else (False if n == "ffmpeg.exe" else None)
    res = motw.strip(tmp_path, fake)
    assert sorted(seen) == sorted(["own-tube.exe", "Python.Runtime.dll", "ffmpeg.exe", "own-tube.exe.config"])
    assert res == {"removed": 3, "failed": [str(Path("tools") / "ffmpeg.exe")], "files": 4}


def test_strip_never_leaves_the_folder(tmp_path):
    app = tmp_path / "app"
    outside = tmp_path / "outside"
    app.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("x")
    (app / "a.dll").write_bytes(b"x")
    os.symlink(outside, app / "link_dir")
    os.symlink(outside / "secret.txt", app / "link_file")
    seen = []
    motw.strip(app, lambda p: seen.append(p))
    assert [Path(p).name for p in seen] == ["a.dll"]


def test_real_remover_on_plain_files(tmp_path):
    (tmp_path / "f.dll").write_bytes(b"x")
    if sys.platform.startswith("win"):
        (tmp_path / "f.dll:Zone.Identifier").write_text("[ZoneTransfer]\r\nZoneId=3\r\n")
        assert motw._remove_stream(str(tmp_path / "f.dll")) is True
    assert motw._remove_stream(str(tmp_path / "f.dll")) is None  # пометки нет — ничего не ломается
    assert (tmp_path / "f.dll").exists()


def test_strip_app_dir_only_when_frozen_on_windows(monkeypatch):
    assert motw.strip_app_dir() is None  # из исходников и не на Windows — не трогает ничего


def test_strip_app_dir_when_frozen(tmp_path, monkeypatch):
    tree(tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "own-tube.exe"))
    calls = []
    real = motw.strip
    monkeypatch.setattr(motw, "strip", lambda root: calls.append(root) or real(root, lambda p: None))
    assert motw.strip_app_dir()["files"] == 4 and calls == [tmp_path]


def test_updater_strips_extracted_files(tmp_path, monkeypatch):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("own-tube-9.9.9-win-x64/own-tube.exe", b"MZ")
        z.writestr("own-tube-9.9.9-win-x64/_internal/x.dll", b"MZ")
    zp = tmp_path / "u.zip"
    zp.write_bytes(buf.getvalue())
    roots = []
    monkeypatch.setattr(updater.motw, "strip", lambda root: roots.append(Path(root)) or {"removed": 0, "failed": [], "files": 2})
    staged = updater.extract(zp, tmp_path / "files")
    assert roots == [tmp_path / "files"] and (staged / "own-tube.exe").is_file()


def test_exe_config_enables_remote_sources():
    assert motw.has_runtime_config(ROOT / "packaging")


def test_exe_config_missing_or_wrong(tmp_path):
    assert not motw.has_runtime_config(tmp_path)
    (tmp_path / motw.CONFIG_NAME).write_text('<configuration><runtime><loadFromRemoteSources enabled="false"/></runtime></configuration>')
    assert not motw.has_runtime_config(tmp_path)
    (tmp_path / motw.CONFIG_NAME).write_text("not xml")
    assert not motw.has_runtime_config(tmp_path)


def test_build_script_packages_config_and_pins():
    sh = (ROOT / "scripts" / "build_windows.sh").read_text(encoding="utf-8")
    assert "packaging/own-tube.exe.config" in sh and 'own-tube.exe.config' in sh
    assert "has_runtime_config" in sh or "loadFromRemoteSources" in sh  # проверка наличия в собранной папке
    req = (ROOT / "requirements.txt").read_text()
    for pkg in ("pywebview", "pythonnet", "clr_loader"):
        assert re.search(rf"^{pkg}==\d+\.\d+\.\d+$", req, re.M | re.I), pkg


def test_friendly_error_for_pythonnet_failure():
    try:
        try:
            raise RuntimeError("Failed to resolve Python.Runtime.Loader.Initialize from C:\\x\\Python.Runtime.dll")
        except RuntimeError:
            raise ImportError("clr")
    except ImportError as exc:
        text = motw.friendly_message(exc, {"failed": ["a.dll"], "removed": 0, "files": 3})
    assert "Unblock-File" in text and "Разблокировать" in text and "a.dll" in text
    assert motw.is_dotnet_failure(RuntimeError("x")) is False
    assert "Unblock-File" not in motw.friendly_message(ValueError("boom"))


def test_main_shows_dialog_and_returns_1(monkeypatch):
    from owntube import app
    shown = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(app, "setup_logging", lambda debug: None)
    monkeypatch.setattr(motw, "show_error", lambda t, title="": shown.append(t))
    monkeypatch.setattr(motw, "strip_app_dir", lambda: {"failed": [], "removed": 0, "files": 0})
    import types
    fake = types.ModuleType("webview")
    sys.modules["webview"] = fake
    monkeypatch.setattr(app, "_run_window", lambda *a: (_ for _ in ()).throw(RuntimeError("Failed to resolve Python.Runtime.Loader.Initialize")))
    try:
        assert app.main([]) == 1
    finally:
        sys.modules.pop("webview", None)
    assert shown and "Failed to resolve" in shown[0]
