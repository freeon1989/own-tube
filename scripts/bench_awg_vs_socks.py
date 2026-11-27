# -*- coding: utf-8 -*-
"""Замер: встроенный VPN AmneziaWG против SOCKS5 на одном и том же стенде (только для разработки).

Стенд: scripts/awg_test_stand.py up (сервер amneziawg-go + NAT), microsocks с логином, и ретрансляторы
helper/latrelay с задержкой D мс в каждую сторону — имитация далёкого сервера (RTT = 2·D).
Обе дороги идут через одну и ту же «дальнюю» задержку; замеряется путь самой программы (httpc, lists).

  python scripts/bench_awg_vs_socks.py --delay-ms 20 --conf /tmp/awg-test-client.conf
"""
from __future__ import annotations

import argparse
import json
import secrets
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from owntube import awg, httpc, lists  # noqa: E402
from owntube.network import Route  # noqa: E402

BULK = "https://speed.cloudflare.com/__down?bytes=50000000"


def ids(n=24):
    items, _, _ = lists.first("search", "music video", Route())
    return [i["id"] for i in items][:n]


def thumbs(route, vids):
    httpc.close_pool()
    t = time.monotonic()
    with ThreadPoolExecutor(24) as ex:
        res = list(ex.map(lambda v: httpc.fetch("GET", f"https://i.ytimg.com/vi/{v}/mqdefault.jpg", route)[0], vids))
    return round(time.monotonic() - t, 2), sum(1 for s in res if s == 200)


def new_conn_ms(route, n=8):
    out = []
    for _ in range(n):
        httpc.close_pool()
        t = time.monotonic()
        st, _, _ = httpc.fetch("GET", "https://www.youtube.com/robots.txt", route)
        assert st == 200
        out.append((time.monotonic() - t) * 1000)
    return round(statistics.median(out))


def warm_req_ms(route, n=8):
    httpc.close_pool()
    httpc.fetch("GET", "https://www.youtube.com/robots.txt", route)
    out = []
    for _ in range(n):
        t = time.monotonic()
        httpc.fetch("GET", "https://www.youtube.com/robots.txt", route)
        out.append((time.monotonic() - t) * 1000)
    return round(statistics.median(out))


def search_sec(route):
    httpc.close_pool()
    t = time.monotonic()
    items, _, _ = lists.first("search", "lofi hip hop " + secrets.token_hex(2), route)
    return round(time.monotonic() - t, 2), len(items)


def bulk_mbit(route):
    t = time.monotonic()
    r = httpc.open_url("GET", BULK, route, timeout=60)
    n = 0
    while True:
        b = r.read(1 << 20)
        if not b:
            break
        n += len(b)
    r.close()
    return round(n * 8 / (time.monotonic() - t) / 1e6, 1)


def measure(name, route, vids):
    res = {"route": name}
    res["new_connection_ms"] = new_conn_ms(route)
    res["reused_connection_ms"] = warm_req_ms(route)
    res["thumbs_24_sec"], res["thumbs_ok"] = thumbs(route, vids)
    res["search_sec"], res["search_items"] = search_sec(route)
    res["bulk_mbit_s"] = bulk_mbit(route)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay-ms", type=int, default=20)
    ap.add_argument("--conf", default="/tmp/awg-test-client.conf")
    ap.add_argument("--relay", default="/tmp/latrelay")
    ap.add_argument("--helper", default="/tmp/awgproxy")
    a = ap.parse_args()
    procs = []
    user, pw = "bench", secrets.token_urlsafe(12)
    procs.append(subprocess.Popen(["microsocks", "-i", "127.0.0.1", "-p", "1080", "-u", user, "-P", pw],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    procs.append(subprocess.Popen([a.relay, "-proto", "tcp", "-listen", "127.0.0.1:1999", "-target", "127.0.0.1:1080", "-delay-ms", str(a.delay_ms)]))
    procs.append(subprocess.Popen([a.relay, "-proto", "udp", "-listen", "127.0.0.1:51999", "-target", "127.0.0.1:51888", "-delay-ms", str(a.delay_ms)]))
    time.sleep(0.5)
    try:
        cfg = awg.parse_conf(Path(a.conf).read_text().replace("127.0.0.1:51888", "127.0.0.1:51999"))
        prov = awg.AwgProvider(lambda: cfg, lambda: a.helper)
        t = time.monotonic()
        awg_route = prov.current()
        prov.status(probe=True)  # рукопожатие
        up_ms = round((time.monotonic() - t) * 1000)
        socks_route = Route(proxy="socks5://127.0.0.1:1999", label="SOCKS", user=user, password=pw)
        vids = ids()
        out = {"delay_each_way_ms": a.delay_ms, "awg_up_and_handshake_ms": up_ms,
               "results": [measure("SOCKS5 (microsocks)", socks_route, vids), measure("AmneziaWG (встроенный)", awg_route, vids)]}
        print(json.dumps(out, ensure_ascii=False, indent=1))
        prov.stop()
    finally:
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
