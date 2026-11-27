# -*- coding: utf-8 -*-
"""Встроенный VPN AmneziaWG только для own-tube.

Туннель поднимает вспомогательная программа tools\\awgproxy.exe (amneziawg-go + userspace-стек gVisor):
без системного сетевого адаптера и без прав администратора. Наружу она открывает только SOCKS5 на
127.0.0.1 со случайным портом и логином/паролем на один сеанс; через него идёт весь трафик программы
(yt-dlp, обложки, видео, mpv, VK, ИИ вне локальной сети). DNS — серверами из конфигурации, через туннель.

Конфигурация (ссылка vpn:// из AmneziaVPN или файл .conf AmneziaWG) хранится только в зашифрованном
хранилище секретов (DPAPI). Ключи не попадают в settings.json, журнал, командную строку и текст ошибок.
"""
from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import logging
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import zlib

from .network import Route, RouteError
from .tools import find_tool

log = logging.getLogger("owntube.awg")

SECRET_NAME = "awg_config"
TOOL = "awgproxy"
DEVICE_INT = ("jc", "jmin", "jmax", "s1", "s2", "s3", "s4")
DEVICE_RANGE = ("h1", "h2", "h3", "h4",
                # AmneziaWG 3.x: время в секундах, одно число или диапазон «от-до» (клиент выбирает значение сам)
                "rekey_after_time", "rekey_timeout", "reject_after_time", "keepalive_timeout", "max_handshake_attempts",
                "content_padding_addition")
DEVICE_CHAIN = ("i1", "i2", "i3", "i4", "i5")
DEVICE_BOOL = ("random_trailers", "disable_cookies")
DEVICE_KEY = ("header_protection_key",)
OBF_KEYS = DEVICE_INT + DEVICE_RANGE + DEVICE_CHAIN + DEVICE_KEY + DEVICE_BOOL
RANGE_MAX = {"h1": 0xFFFFFFFF, "h2": 0xFFFFFFFF, "h3": 0xFFFFFFFF, "h4": 0xFFFFFFFF}
V3_KEYS = DEVICE_RANGE[4:] + DEVICE_KEY + DEVICE_BOOL


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


# имя в .conf / поле в vpn:// (без регистра, без «_» и пробелов) → имя параметра устройства
OBF_ALIASES = {_norm(k): k for k in OBF_KEYS}
OBF_ALIASES.update({"junkpacketcount": "jc", "junkpacketminsize": "jmin", "junkpacketmaxsize": "jmax",
                    "initpacketjunksize": "s1", "responsepacketjunksize": "s2", "cookiereplypacketjunksize": "s3",
                    "transportpacketjunksize": "s4", "initpacketmagicheader": "h1", "responsepacketmagicheader": "h2",
                    "underloadpacketmagicheader": "h3", "transportpacketmagicheader": "h4"})
# ключи, которые не относятся к протоколу туннеля и безопасно не учитываются в приложении
IFACE_IGNORED = {"privatekey", "address", "dns", "mtu", "listenport", "table", "preup", "postup", "predown", "postdown",
                 "saveconfig", "fwmark", "name"}
PEER_KEYS = {"publickey", "presharedkey", "endpoint", "allowedips", "persistentkeepalive"}
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|(?<![\w:])(?:[0-9a-fA-F]{1,4}:){3,7}[0-9a-fA-F]{1,4}(?![\w:])"
                    r"|(?<![\w:])(?:[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4})*)?::(?:[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4})*)?(?![\w:])")
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


class AwgConfigError(ValueError):
    """Ошибка разбора конфигурации. Текст безопасен для показа: без ключей и адресов."""


def hide_addresses(text: str) -> str:
    return _IP_RE.sub("<адрес>", text or "")


# ---------- разбор ----------
def parse_any(text: str) -> dict:
    if not isinstance(text, str) or not text.strip():
        raise AwgConfigError("Пустая конфигурация")
    if len(text) > 200_000:
        raise AwgConfigError("Слишком большая конфигурация")
    t = text.strip().lstrip("\ufeff")
    if t.lower().startswith("vpn://"):
        return parse_link(t)
    if "[interface]" in t.lower():
        return parse_conf(t)
    raise AwgConfigError("Нужна ссылка vpn:// из AmneziaVPN или файл конфигурации AmneziaWG (.conf)")


def _b64(data: str) -> bytes:
    data = re.sub(r"\s+", "", data)
    pad = "=" * (-len(data) % 4)
    for dec in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            return dec(data + pad)
        except (binascii.Error, ValueError):
            continue
    raise AwgConfigError("Ссылка vpn:// повреждена (не base64)")


def _qt_uncompress(raw: bytes) -> bytes:
    """qCompress: 4 байта длины (big-endian) + поток zlib. Старые ссылки бывают без сжатия."""
    if len(raw) > 4:
        try:
            return zlib.decompress(raw[4:])
        except zlib.error:
            pass
        try:
            return zlib.decompress(raw)
        except zlib.error:
            pass
    return raw


def parse_link(link: str) -> dict:
    raw = _qt_uncompress(_b64(link.strip()[6:]))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise AwgConfigError("Ссылка vpn:// не содержит конфигурации (не JSON)") from None
    if not isinstance(data, dict):
        raise AwgConfigError("Ссылка vpn:// не содержит конфигурации")
    containers = data.get("containers") if isinstance(data.get("containers"), list) else []
    default = data.get("defaultContainer") or ""
    order = sorted(containers, key=lambda c: 0 if isinstance(c, dict) and c.get("container") == default else 1)
    for c in order:
        if not isinstance(c, dict):
            continue
        for key in ("awg", "amnezia-awg", "amnezia-awg2", "wireguard"):
            block = c.get(key)
            if isinstance(block, dict):
                cfg = _from_block(block, data)
                cfg["name"] = _clean_name(data.get("description") or data.get("name") or "AmneziaWG")
                return cfg
    if data.get("api_key") or data.get("config_version") or data.get("auth_data"):
        raise AwgConfigError("Это ссылка на подписку Amnezia (через их сервер), в ней нет ключей туннеля. "
                             "В AmneziaVPN откройте сервер → «Поделиться» → формат AmneziaWG и вставьте ту ссылку или файл .conf")
    raise AwgConfigError("В ссылке нет конфигурации AmneziaWG (поддерживаются протоколы AmneziaWG и WireGuard)")


def _from_block(block: dict, top: dict) -> dict:
    last = block.get("last_config")
    if isinstance(last, str):
        try:
            last = json.loads(last)
        except ValueError:
            last = {}
    if not isinstance(last, dict):
        last = {}
    text = last.get("config")
    dns1, dns2 = str(top.get("dns1") or "1.1.1.1"), str(top.get("dns2") or "1.0.0.1")
    if isinstance(text, str) and "[interface]" in text.lower():
        text = text.replace("$PRIMARY_DNS", dns1).replace("$SECONDARY_DNS", dns2)
        return parse_conf(text)
    # Конфигурация по полям (старые выгрузки)
    priv = last.get("client_priv_key")
    pub = last.get("server_pub_key")
    host = last.get("hostName") or top.get("hostName")
    port = last.get("port") or block.get("port")
    if not (priv and pub and host and port):
        raise AwgConfigError("В ссылке нет ключей клиента — экспортируйте полную конфигурацию из AmneziaVPN")
    lines = ["[Interface]", f"PrivateKey = {priv}", f"Address = {last.get('client_ip') or '10.8.1.2'}/32",
             f"DNS = {dns1}, {dns2}"]
    if last.get("mtu"):
        lines.append(f"MTU = {last.get('mtu')}")
    seen: dict[str, str] = {}
    for src in (block, last):  # поля последней конфигурации важнее полей контейнера
        for fk, fv in src.items():
            name = OBF_ALIASES.get(_norm(fk))
            if name and isinstance(fv, (str, int)) and not isinstance(fv, bool) and str(fv).strip() != "":
                seen[name] = str(fv)
            elif name and isinstance(fv, bool):
                seen[name] = "true" if fv else "false"
    for name in OBF_KEYS:
        if name in seen:
            lines.append(f"{name} = {seen[name]}")
    lines += ["[Peer]", f"PublicKey = {pub}"]
    if last.get("psk_key"):
        lines.append(f"PresharedKey = {last.get('psk_key')}")
    ka = next((last[k] for k in ("persistent_keep_alive", "persistent_keepalive", "PersistentKeepalive") if last.get(k) not in (None, "")), 25)
    lines += [f"Endpoint = {host}:{port}", "AllowedIPs = 0.0.0.0/0, ::/0", f"PersistentKeepalive = {ka}"]
    return parse_conf("\n".join(lines))


def _clean_name(v) -> str:
    v = "".join(ch for ch in str(v) if ch.isprintable())[:60].strip()
    return v or "AmneziaWG"


def parse_conf(text: str) -> dict:
    iface: dict[str, str] = {}
    shown: dict[str, str] = {}  # нормализованное имя → как написано в конфигурации (для сообщения об ошибке)
    peers: list[dict[str, str]] = []
    section = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip() if not raw.strip().lower().startswith(("i1", "i2", "i3", "i4", "i5")) else raw.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section == "peer":
                peers.append({})
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        shown.setdefault(_norm(k), k.strip())
        k, v = _norm(k), v.strip()
        if section == "interface":
            iface[k] = (iface[k] + "," + v) if k in ("address", "dns") and k in iface else v
        elif section == "peer" and peers:
            if k not in PEER_KEYS:
                raise AwgConfigError(f"Параметр пира «{_safe_name(shown.get(k, k))}» не поддерживается этой версией встроенного VPN")
            peers[-1][k] = (peers[-1][k] + "," + v) if k == "allowedips" and k in peers[-1] else v
    if not iface:
        raise AwgConfigError("В конфигурации нет секции [Interface]")
    if not peers:
        raise AwgConfigError("В конфигурации нет секции [Peer]")
    cfg = {
        "name": "AmneziaWG",
        "private_key": _key(iface.get("privatekey"), "PrivateKey"),
        "addresses": _addresses(iface.get("address", "")),
        "dns": _dns(iface.get("dns", "")),
        "mtu": _int(iface.get("mtu") or "1280", "MTU", 576, 9000),
        "obf": {},
        "peers": [],
    }
    for raw_key, val in iface.items():
        if raw_key in IFACE_IGNORED:
            continue
        name = OBF_ALIASES.get(raw_key)
        if name is None:
            raise AwgConfigError(f"Параметр «{_safe_name(shown.get(raw_key, raw_key))}» не поддерживается этой версией встроенного VPN")
        if val != "":
            cfg["obf"][name] = _obf(name, val)
    cfg["obf"] = {k: cfg["obf"][k] for k in OBF_KEYS if k in cfg["obf"]}
    if "jmin" in cfg["obf"] and "jmax" in cfg["obf"] and int(cfg["obf"]["jmin"]) > int(cfg["obf"]["jmax"]):
        raise AwgConfigError("Jmin больше Jmax")
    for p in peers:
        peer = {
            "public_key": _key(p.get("publickey"), "PublicKey"),
            "preshared_key": _key(p.get("presharedkey"), "PresharedKey") if p.get("presharedkey") else "",
            "endpoint": _endpoint(p.get("endpoint", "")),
            "allowed_ips": _prefixes(p.get("allowedips", "0.0.0.0/0")),
            "keepalive": _keepalive(p.get("persistentkeepalive") or "25"),
        }
        cfg["peers"].append(peer)
    if not any(p["endpoint"] for p in cfg["peers"]):
        raise AwgConfigError("В конфигурации нет Endpoint (адреса сервера)")
    return cfg


def _safe_name(k: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "", str(k))[:40] or "?"


def _range(v: str, what: str, hi: int) -> tuple[int, int]:
    m = re.match(r"^(\d{1,10})(?:\s*-\s*(\d{1,10}))?$", v.strip())
    if not m:
        raise AwgConfigError(f"{what}: нужно число или диапазон «от-до»")
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    if b < a or b > hi:
        raise AwgConfigError(f"{what}: неверный диапазон")
    return a, b


def _keepalive(v):
    """PersistentKeepalive: число или диапазон «от-до» (AmneziaWG 3.x). Число остаётся числом."""
    a, b = _range(str(v), "PersistentKeepalive", 3600)
    return a if str(v).strip().isdigit() else f"{a}-{b}"


def _bool(v: str, what: str) -> str:
    t = v.strip().lower()
    if t in ("on", "true", "yes", "1"):
        return "true"
    if t in ("off", "false", "no", "0"):
        return "false"
    raise AwgConfigError(f"{what}: нужно on/off")


def _key(v, what: str) -> str:
    try:
        raw = base64.b64decode((v or "").strip(), validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != 32:
        raise AwgConfigError(f"{what}: это не ключ WireGuard")
    return base64.b64encode(raw).decode()


def _int(v, what, lo, hi) -> int:
    try:
        n = int(str(v).strip())
    except ValueError:
        raise AwgConfigError(f"{what}: нужно число") from None
    if not lo <= n <= hi:
        raise AwgConfigError(f"{what}: число вне допустимых пределов")
    return n


def _addresses(v: str) -> list[str]:
    out = []
    for part in v.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(str(ipaddress.ip_interface(part)))
        except ValueError:
            raise AwgConfigError("Address: неверный адрес") from None
    if not out:
        raise AwgConfigError("В конфигурации нет Address")
    return out


def _dns(v: str) -> list[str]:
    out = []
    for part in v.split(","):
        part = part.strip()
        try:
            out.append(str(ipaddress.ip_address(part)))
        except ValueError:
            continue  # домены поиска и прочее — пропускаем
    return out or ["1.1.1.1", "1.0.0.1"]


def _prefixes(v: str) -> list[str]:
    out = []
    for part in v.split(","):
        part = part.strip()
        if part:
            try:
                out.append(str(ipaddress.ip_network(part, strict=False)))
            except ValueError:
                raise AwgConfigError("AllowedIPs: неверная сеть") from None
    return out or ["0.0.0.0/0"]


def _endpoint(v: str) -> str:
    v = v.strip()
    if not v:
        return ""
    m = re.match(r"^\[([0-9a-fA-F:.]+)\]:(\d{1,5})$", v) or re.match(r"^([^:\[\]]+):(\d{1,5})$", v)
    if not m:
        raise AwgConfigError("Endpoint: нужен вид адрес:порт")
    host, port = m.group(1), int(m.group(2))
    if not 1 <= port <= 65535:
        raise AwgConfigError("Endpoint: неверный порт")
    try:
        ip = ipaddress.ip_address(host)
        return f"[{ip}]:{port}" if ip.version == 6 else f"{ip}:{port}"
    except ValueError:
        if not _HOST_RE.match(host):
            raise AwgConfigError("Endpoint: неверное имя сервера") from None
        return f"{host.lower()}:{port}"


def _obf(k: str, v: str) -> str:
    v = v.strip()
    what = "".join(w.capitalize() for w in k.split("_")) if "_" in k else k.upper()
    if k in DEVICE_INT:
        return str(_int(v, what, 0, 65535))
    if k in DEVICE_RANGE:
        a, b = _range(v, what, RANGE_MAX.get(k, 0xFFFFFFFF))
        return f"{a}-{b}" if "-" in v else str(a)
    if k in DEVICE_BOOL:
        return _bool(v, what)
    if k in DEVICE_KEY:
        t = re.sub(r"\s+", "", v)
        try:
            raw = bytes.fromhex(t) if re.fullmatch(r"[0-9a-fA-F]{64}", t) else base64.b64decode(t, validate=True)
        except (binascii.Error, ValueError):
            raw = b""
        if len(raw) != 32:
            raise AwgConfigError(f"{what}: нужен ключ из 32 байт (base64)")
        return base64.b64encode(raw).decode()
    if len(v) > 8192 or not v.startswith("<") or any(not ch.isprintable() for ch in v):
        raise AwgConfigError(f"{what}: неверная цепочка (ожидается вида <b 0x...><r 16>)")
    return v


def summary(cfg: dict) -> dict:
    """Что показать в интерфейсе: без ключей и без полного адреса сервера."""
    obf = cfg.get("obf") or {}
    if any(k in obf for k in V3_KEYS):
        kind = "AmneziaWG 3.1"
    elif any(k in obf for k in ("s3", "s4")) or any("-" in obf.get(k, "") for k in DEVICE_RANGE[:4]):
        kind = "AmneziaWG 2.0"
    elif any(k in obf for k in DEVICE_CHAIN):
        kind = "AmneziaWG 1.5"
    elif obf:
        kind = "AmneziaWG 1.0"
    else:
        kind = "WireGuard (без обфускации)"
    ep = next((p["endpoint"] for p in cfg.get("peers", []) if p.get("endpoint")), "")
    port = ep.rsplit(":", 1)[-1] if ep else "?"
    return {"name": cfg.get("name") or "AmneziaWG", "kind": kind, "server": f"•••:{port}",
            "obf": [k.upper() for k in OBF_KEYS if k in obf], "dns": len(cfg.get("dns") or []),
            "full_tunnel": any(a in ("0.0.0.0/0",) for p in cfg.get("peers", []) for a in p.get("allowed_ips", []))}


# ---------- вспомогательный процесс ----------
class Helper:
    def __init__(self, exe: str):
        self.exe = exe
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.user = "own-tube"
        self.auth = ""
        self.bind = ""
        self._q: dict[str, queue.Queue] = {"stats": queue.Queue(), "probe": queue.Queue()}
        self._wlock = threading.Lock()
        self.errors: list[str] = []

    def start(self, cfg: dict, timeout: float = 20) -> None:
        self.auth = secrets.token_urlsafe(24)
        payload = {k: cfg[k] for k in ("private_key", "addresses", "dns", "mtu", "obf", "peers")}
        payload.update(user=self.user, **{"pass": self.auth})
        flags = 0x08000000 if sys.platform.startswith("win") else 0  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen([self.exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     shell=False, creationflags=flags, bufsize=0)
        ready: queue.Queue = queue.Queue()
        threading.Thread(target=self._read_out, args=(ready,), daemon=True, name="awg-out").start()
        threading.Thread(target=self._read_err, daemon=True, name="awg-err").start()
        try:
            self.proc.stdin.write((json.dumps(payload) + "\n").encode())
            self.proc.stdin.flush()
        except OSError:
            pass
        payload.clear()
        try:
            msg = ready.get(timeout=timeout)
        except queue.Empty:
            self.stop()
            raise RouteError("VPN не запустился за отведённое время") from None
        if not msg.get("ready"):
            self.stop()
            err = hide_addresses(str(msg.get("error") or "; ".join(self.errors[-3:]) or "неизвестная ошибка"))
            raise RouteError("VPN не запустился: " + err[:200])
        self.port = int(msg["port"])
        self.bind = str(msg.get("bind") or "")

    def _read_out(self, ready: queue.Queue) -> None:
        first = True
        for line in iter(self.proc.stdout.readline, b""):
            try:
                msg = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if first and ("ready" in msg):
                first = False
                ready.put(msg)
                continue
            for k, q in self._q.items():
                if k in msg:
                    q.put(msg[k])
        if first:
            ready.put({"ready": False})

    def _read_err(self) -> None:
        n = 0
        for line in iter(self.proc.stderr.readline, b""):
            text = hide_addresses(line.decode("utf-8", "replace").strip())[:200]
            if not text:
                continue
            self.errors = (self.errors + [text])[-10:]
            if n < 50:
                log.warning("VPN: %s", text)
                n += 1

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ask(self, cmd: str, key: str, timeout: float = 12):
        q = self._q[key]
        while not q.empty():
            q.get_nowait()
        with self._wlock:
            if not self.alive():
                raise RouteError("VPN остановлен")
            self.proc.stdin.write((cmd + "\n").encode())
            self.proc.stdin.flush()
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            raise RouteError("VPN не ответил") from None

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            p.stdin.close()  # закрытый stdin — сигнал помощнику завершиться
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except OSError:
                pass


class AwgProvider:
    """Маршрут «встроенный VPN»: туннель поднимается при первом запросе и живёт, пока открыта программа."""

    def __init__(self, load_config, exe_finder=lambda: find_tool(TOOL)):
        self._load = load_config
        self._find = exe_finder
        self._helper: Helper | None = None
        self._lock = threading.Lock()
        self._started = 0.0
        self.last_error = ""

    def _ensure(self) -> Helper:
        with self._lock:
            if self._helper is not None and self._helper.alive():
                return self._helper
            if self._helper is not None:
                log.warning("VPN: вспомогательный процесс завершился, перезапускаю")
                self._helper.stop()
                self._helper = None
            cfg = self._load()
            if not cfg:
                raise RouteError("Встроенный VPN включён, но конфигурация не импортирована (Настройки → Сеть)")
            exe = self._find()
            if not exe:
                raise RouteError("Нет tools\\awgproxy.exe рядом с программой — переустановите own-tube")
            h = Helper(exe)
            t = time.monotonic()
            try:
                h.start(cfg)
            except RouteError as exc:
                self.last_error = str(exc)
                raise
            self.last_error = ""
            self._helper, self._started = h, time.time()
            log.info("VPN AmneziaWG запущен за %.2f с (%s)", time.monotonic() - t, summary(cfg)["kind"])
            return h

    def current(self) -> Route:
        h = self._ensure()
        return Route(proxy=f"socks5://127.0.0.1:{h.port}", label="VPN AmneziaWG", user=h.user, password=h.auth)

    def running(self) -> bool:
        return self._helper is not None and self._helper.alive()

    def status(self, probe: bool = False) -> dict:
        if not self.running():
            return {"running": False, "error": self.last_error}
        h = self._helper
        out = {"running": True, "uptime_sec": int(time.time() - self._started), "bind": h.bind}
        try:
            out.update(h.ask("stats", "stats"))
            if probe:
                p = h.ask("probe www.youtube.com:443", "probe", timeout=15)
                out["latency_ms"] = p.get("ms")
                if p.get("error"):
                    out["probe_error"] = hide_addresses(str(p["error"]))[:160]
        except RouteError as exc:
            out["error"] = str(exc)
        return out

    def check(self) -> dict:
        try:
            self._ensure()
        except RouteError as exc:
            return {"ok": False, "detail": str(exc)}
        st = self.status(probe=True)
        if st.get("latency_ms") is None:
            return {"ok": False, "detail": "Туннель поднят, но до YouTube не достучаться: " + (st.get("probe_error") or st.get("error") or "нет ответа")}
        hs = st.get("handshake_age_sec", -1)
        return {"ok": True, "detail": f"Туннель работает: рукопожатие {hs} с назад, соединение с YouTube {st['latency_ms']} мс"}

    def restart(self) -> None:
        self.stop()

    def stop(self) -> None:
        with self._lock:
            if self._helper is not None:
                self._helper.stop()
                self._helper = None


class SwitchRoutes:
    """Выбор маршрута по настройке «Сеть»: прокси из настроек или встроенный VPN."""

    def __init__(self, get_mode, manual, awg: AwgProvider):
        self._mode = get_mode
        self.manual = manual
        self.awg = awg

    @property
    def vpn(self) -> bool:
        return self._mode() == "awg"

    def current(self) -> Route:
        return self.awg.current() if self.vpn else self.manual.current()

    def check(self) -> dict:
        return self.awg.check() if self.vpn else self.manual.check()
