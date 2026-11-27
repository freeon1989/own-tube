# -*- coding: utf-8 -*-
"""Секреты (пароль прокси, ключи API): на Windows — DPAPI, иначе keyring или только память.

В settings.json и в журнал не попадают. На диске — только зашифрованные байты.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("owntube.secretstore")

SERVICE = "own-tube"
PROXY_KEY = "proxy-password"
SECRETS_NAME = "secrets.dpapi"
CRYPTPROTECT_UI_FORBIDDEN = 0x01


class MemoryBackend:
    """Никуда не пишет. Секреты живут до закрытия программы."""
    can_persist = False
    note = ("На этой системе нет Windows DPAPI и хранилища ключей: секреты будут действовать "
            "до закрытия программы и в файл не записываются.")

    def __init__(self):
        self._data: dict[str, str] = {}

    def get(self, name: str) -> str:
        return self._data.get(name, "")

    def set(self, name: str, value: str) -> None:
        self._data[name] = value

    def delete(self, name: str) -> None:
        self._data.pop(name, None)


def _dpapi_protect(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), "own-tube secrets", None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
    ):
        raise OSError("не удалось зашифровать секрет (DPAPI)")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
    ):
        raise OSError("не удалось расшифровать секрет (DPAPI)")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


class FileBackend:
    """Один файл: JSON имя→секрет, целиком прогнанный через protect()."""
    can_persist = True

    def __init__(self, path: Path, protect, unprotect, note: str):
        self.path = path
        self._protect = protect
        self._unprotect = unprotect
        self.note = note

    def _read_all(self) -> dict[str, str]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        if not raw:
            return {}
        data = json.loads(self._unprotect(raw).decode("utf-8"))
        if isinstance(data, str):
            return {PROXY_KEY: data}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}

    def _write_all(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        blob = self._protect(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix="secrets.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get(self, name: str) -> str:
        return self._read_all().get(name, "")

    def set(self, name: str, value: str) -> None:
        data = self._read_all()
        data[name] = value
        self._write_all(data)

    def delete(self, name: str) -> None:
        data = self._read_all()
        if name not in data:
            return
        data.pop(name, None)
        if data:
            self._write_all(data)
        else:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


class KeyringBackend:
    can_persist = True
    note = "Секреты сохранены в хранилище ключей системы и не записываются в settings.json."

    def get(self, name: str) -> str:
        import keyring
        return keyring.get_password(SERVICE, name) or ""

    def set(self, name: str, value: str) -> None:
        import keyring
        keyring.set_password(SERVICE, name, value)

    def delete(self, name: str) -> None:
        import keyring
        try:
            keyring.delete_password(SERVICE, name)
        except Exception:
            try:
                keyring.set_password(SERVICE, name, "")
            except Exception:
                pass


def _keyring_available() -> bool:
    try:
        import keyring
        from keyring.errors import NoKeyringError
        kr = keyring.get_keyring()
        name = type(kr).__name__.lower()
        if "fail" in name or "null" in name:
            return False
        try:
            keyring.get_password(SERVICE, PROXY_KEY)
        except NoKeyringError:
            return False
        except Exception:
            return False
        return True
    except Exception:
        return False


def default_backend(directory: Path):
    if sys.platform.startswith("win"):
        return FileBackend(
            Path(directory) / SECRETS_NAME,
            _dpapi_protect, _dpapi_unprotect,
            "Секреты зашифрованы средствами Windows (DPAPI) и не записываются в settings.json.",
        )
    if _keyring_available():
        return KeyringBackend()
    return MemoryBackend()


class SecretStore:
    def __init__(self, directory: Path, backend=None):
        self.dir = Path(directory)
        self._backend = backend if backend is not None else default_backend(self.dir)
        self._memory: dict[str, str] = {}
        self._loaded: set[str] = set()

    @property
    def note(self) -> str:
        return getattr(self._backend, "note", MemoryBackend.note)

    @property
    def can_persist(self) -> bool:
        return bool(getattr(self._backend, "can_persist", False))

    def get(self, name: str = PROXY_KEY) -> str:
        if name in self._loaded:
            return self._memory.get(name, "")
        try:
            value = self._backend.get(name) or ""
        except Exception:
            log.warning("Не удалось прочитать сохранённый секрет")
            value = ""
        self._memory[name] = value
        self._loaded.add(name)
        return value

    def set(self, value: str, name: str = PROXY_KEY) -> bool:
        """True — удалось записать на диск или в хранилище ключей."""
        if not value:
            self.delete(name)
            return True
        self._memory[name] = value
        self._loaded.add(name)
        if not self.can_persist:
            return False
        try:
            self._backend.set(name, value)
            return True
        except Exception:
            log.warning("Не удалось сохранить секрет в защищённое хранилище — останется до закрытия программы")
            return False

    def delete(self, name: str = PROXY_KEY) -> None:
        self._memory[name] = ""
        self._loaded.add(name)
        try:
            self._backend.delete(name)
        except Exception:
            pass

    def reload(self, name: str) -> None:
        """Перечитать секрет с диска (его мог записать другой процесс — окно входа)."""
        self._loaded.discard(name)
        self._memory.pop(name, None)

    def has(self, name: str = PROXY_KEY) -> bool:
        return bool(self.get(name))
