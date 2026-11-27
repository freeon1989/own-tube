#!/bin/bash
# Воспроизводимая сборка tools\awgproxy.exe (встроенный VPN AmneziaWG) для windows/amd64.
# Исходники: helper/awgproxy (+ копия gVisor netstack из amneziawg-go с настройкой буферов TCP).
# Зависимости зафиксированы в go.mod/go.sum; версия Go и amneziawg-go проверяются ниже.
# Результат: $1/awgproxy.exe и строка с SHA-256 в $1/SOURCES-awgproxy.txt
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/build/tools}"
GO_VERSION="go1.27.3"
AWG_MODULE="github.com/amnezia-vpn/amneziawg-go/v3"
AWG_VERSION="v3.1.20261116"   # коммит 08fe3d33bbb116be09a6183d02609311b8b0c9e8
mkdir -p "$OUT"
have="$(GOTOOLCHAIN=local go env GOVERSION)"
if [ "$have" != "$GO_VERSION" ] && [ -z "${ALLOW_GO_MISMATCH:-}" ]; then
  echo "нужен $GO_VERSION (есть $have); ALLOW_GO_MISMATCH=1 — собрать всё равно (без побайтового совпадения)" >&2; exit 1
fi
cd "$ROOT/helper/awgproxy"
export GOTOOLCHAIN=local GOFLAGS=-mod=readonly CGO_ENABLED=0 GOOS=windows GOARCH=amd64 GOAMD64=v1
got="$(go list -m -f '{{.Version}}' "$AWG_MODULE")"
[ "$got" = "$AWG_VERSION" ] || { echo "amneziawg-go $got, ожидалась $AWG_VERSION" >&2; exit 1; }
go mod verify
go build -trimpath -buildvcs=false -ldflags "-s -w -buildid=" -o "$OUT/awgproxy.exe" .
sum="$(sha256sum "$OUT/awgproxy.exe" | cut -d' ' -f1)"
cat > "$OUT/SOURCES-awgproxy.txt" <<T
awgproxy.exe — встроенный VPN own-tube (AmneziaWG в процессе, SOCKS5 на 127.0.0.1)
  исходники: helper/awgproxy в https://github.com/freeon1989/own-tube
  $AWG_MODULE $AWG_VERSION (commit 08fe3d33bbb116be09a6183d02609311b8b0c9e8), MIT
  gVisor netstack (Apache-2.0), golang.org/x/crypto, golang.org/x/net (BSD-3-Clause)
  сборка: $GO_VERSION, CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -trimpath -buildvcs=false -ldflags "-s -w -buildid="
  sha256: $sum
T
echo "awgproxy.exe sha256: $sum"
