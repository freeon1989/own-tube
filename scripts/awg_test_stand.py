# -*- coding: utf-8 -*-
"""Тестовый стенд AmneziaWG на Linux (только для разработки; ключи генерируются заново и нигде не хранятся).

Сервер — обычный amneziawg-go с системным tun (нужен sudo) и NAT наружу, клиент — tools/awgproxy own-tube.
Так проверяется весь путь до настоящего YouTube через туннель с параметрами обфускации AWG 2.0.

  sudo python3 scripts/awg_test_stand.py up   --server-bin /tmp/amneziawg-go-linux --keys-bin /tmp/awgproxy --out /tmp/awg-test-client.conf
  sudo python3 scripts/awg_test_stand.py down
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import time

IFACE = "awgt0"
NET = "10.77.9"
PORT = 51888
OBF = {"jc": "4", "jmin": "40", "jmax": "70", "s1": "15", "s2": "18", "s3": "20", "s4": "8",
       "h1": "1000000-1000100", "h2": "2000000-2000100", "h3": "3000000-3000100", "h4": "4000000-4000100",
       "i1": "<b 0xc70000000108><r 16><t>"}


def sh(*cmd, check=True):
    try:
        return subprocess.run(list(cmd), check=check, capture_output=True, text=True)
    except FileNotFoundError:  # нет iptables-legacy: для замеров до самой этой машины NAT наружу не нужен
        if check:
            raise
        return subprocess.CompletedProcess(list(cmd), 127, "", "нет такой команды")


def keypair(keys_bin):
    return json.loads(subprocess.run([keys_bin, "-test-keypair"], capture_output=True, text=True, check=True).stdout)


def hexkey(b64):
    return base64.b64decode(b64).hex()


def uapi(text):
    path = f"/var/run/amneziawg/{IFACE}.sock"
    for _ in range(50):
        if os.path.exists(path):
            break
        time.sleep(0.1)
    s = socket.socket(socket.AF_UNIX)
    s.connect(path)
    s.sendall(("set=1\n" + text + "\n").encode())
    resp = s.recv(4096).decode()
    s.close()
    if "errno=0" not in resp:
        raise SystemExit("UAPI: " + resp.strip())


def up(a):
    down(None)
    srv, cli = keypair(a.keys_bin), keypair(a.keys_bin)
    subprocess.Popen([a.server_bin, "-f", IFACE], stdout=subprocess.DEVNULL, stderr=open("/tmp/awg-test-server.log", "w"),
                     env={**os.environ, "LOG_LEVEL": "error"}, start_new_session=True)
    obf = "".join(f"{k}={v}\n" for k, v in OBF.items())
    uapi(f"private_key={hexkey(srv['private'])}\nlisten_port={PORT}\n{obf}public_key={hexkey(cli['public'])}\nallowed_ip={NET}.2/32\n")
    sh("ip", "addr", "add", f"{NET}.1/24", "dev", IFACE)
    sh("ip", "link", "set", IFACE, "mtu", "1280", "up")
    with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
        f.write("1")
    for flag in ("-s", "-d"):  # Docker ставит FORWARD DROP в iptables-legacy: пропускаем только сеть стенда
        sh("iptables-legacy", "-I", "FORWARD", "1", flag, f"{NET}.0/24", "-j", "ACCEPT", check=False)
    sh("nft", "add", "table", "ip", "owntube_awg_test")
    sh("nft", "add", "chain", "ip", "owntube_awg_test", "post", "{ type nat hook postrouting priority 100 ; }")
    sh("nft", "add", "rule", "ip", "owntube_awg_test", "post", "ip", "saddr", f"{NET}.0/24", "oifname", "!=", IFACE, "masquerade")
    lines = ["[Interface]", f"PrivateKey = {cli['private']}", f"Address = {NET}.2/32", "DNS = 1.1.1.1, 8.8.8.8", "MTU = 1280"]
    lines += [f"{k.upper()} = {v}" for k, v in OBF.items()]
    lines += ["", "[Peer]", f"PublicKey = {srv['public']}", f"Endpoint = 127.0.0.1:{PORT}", "AllowedIPs = 0.0.0.0/0",
              "PersistentKeepalive = 25", ""]
    if os.path.exists(a.out):
        os.unlink(a.out)
    with open(a.out, "w") as f:
        f.write("\n".join(lines))
    if os.environ.get("SUDO_UID"):
        os.chown(a.out, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
    os.chmod(a.out, 0o600)
    print("стенд поднят:", IFACE, f"udp/{PORT}", "клиентская конфигурация:", a.out)


def down(_):
    sh("nft", "delete", "table", "ip", "owntube_awg_test", check=False)
    sh("nft", "delete", "table", "ip", "owntube_fwd", check=False)
    for flag in ("-s", "-d"):
        while sh("iptables-legacy", "-D", "FORWARD", flag, f"{NET}.0/24", "-j", "ACCEPT", check=False).returncode == 0:
            pass
    sh("pkill", "-f", f"amneziawg-go-linux -f {IFACE}", check=False)
    sh("ip", "link", "del", IFACE, check=False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["up", "down"])
    ap.add_argument("--server-bin", default="/tmp/amneziawg-go-linux")
    ap.add_argument("--keys-bin", default="/tmp/awgproxy")
    ap.add_argument("--out", default="/tmp/awg-test-client.conf")
    a = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("нужен sudo")
    (up if a.cmd == "up" else down)(a)
