# -*- coding: utf-8 -*-
"""Пропускная способность туннеля через SOCKS5-помощник: 1 и N одновременных потоков (стенд scripts/awg_test_stand.py).
python scripts/bench_awg_flows.py <клиентская .conf> <helper> [адрес в туннеле] [секунд]"""
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from owntube import awg  # noqa: E402


def sink(host, port=0):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(64)
    blob = b"\0" * (1 << 20)

    def serve(c):
        try:
            c.recv(16)
            while True:
                c.sendall(blob)
        except OSError:
            pass
        finally:
            c.close()

    def loop():
        while True:
            c, _ = srv.accept()
            threading.Thread(target=serve, args=(c,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return srv.getsockname()[1]


def socks_connect(route, host, port):
    from urllib.parse import urlsplit
    u = urlsplit(route.proxy)
    s = socket.create_connection((u.hostname, u.port), timeout=10)
    s.sendall(b"\x05\x01\x02")
    s.recv(2)
    user, pw = route.user.encode(), route.password.encode()
    s.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(pw)]) + pw)
    assert s.recv(2) == b"\x01\x00"
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host.encode() + port.to_bytes(2, "big"))
    r = s.recv(10)
    assert r[1] == 0, r
    return s


def run(route, host, port, flows, secs):
    total = [0] * flows
    stop = time.time() + secs

    def worker(i):
        s = socks_connect(route, host, port)
        s.sendall(b"go")
        while time.time() < stop:
            d = s.recv(1 << 18)
            if not d:
                break
            total[i] += len(d)
        s.close()
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(flows)]
    t0 = time.time()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return sum(total) * 8 / (time.time() - t0) / 1e6


def main():
    conf, helper = sys.argv[1], sys.argv[2]
    host = sys.argv[3] if len(sys.argv) > 3 else "10.77.9.1"
    secs = float(sys.argv[4]) if len(sys.argv) > 4 else 6
    prov = awg.AwgProvider(lambda: awg.parse_conf(Path(conf).read_text()), lambda: helper)
    route = prov.current()
    port = sink(host)
    try:
        run(route, host, port, 1, 1.5)  # рукопожатие и прогрев
        for n in (1, 2, 6):
            print(f"{n} поток(ов): {run(route, host, port, n, secs):.0f} Мбит/с")
    finally:
        prov.stop()


if __name__ == "__main__":
    main()
