#!/bin/bash
# Кросс-сборка own-tube для Windows на Linux: Wine + CPython 3.12 amd64 + PyInstaller (onedir).
# Внешние программы (mpv, ffmpeg, deno) скачиваются scripts/fetch_tools.py с проверкой SHA-256.
# Результат: $OUT_DIR/own-tube-<версия>-win-x64.zip и .sha256
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
for d in "$HOME/wine/wine-10.17-amd64-wow64/bin" "$HOME/wine/wine-10.17-amd64/bin"; do
  if [ -z "${WINE_BIN_DIR:-}" ] && [ -x "$d/wine" ]; then WINE_BIN_DIR="$d"; fi
done
[ -n "${WINE_BIN_DIR:-}" ] && export PATH="$WINE_BIN_DIR:$PATH"
export WINEPREFIX="${WINEPREFIX:-$HOME/.wine-owntube}" WINEARCH=win64 WINEDEBUG="${WINEDEBUG:--all}"
export WINEDLLOVERRIDES="${WINEDLLOVERRIDES:-mscoree,mshtml=}"
PY_WIN="C:\\Python312\\python.exe"
VERSION="$(python3 -c 'import re;print(re.search(r"__version__ = \"([0-9.]+)\"", open("owntube/__init__.py").read()).group(1))')"
CACHE="${CACHE:-$HOME/.cache/owntube-build}"
OUT_DIR="${OUT_DIR:-$ROOT/dist}"
PKG="own-tube-${VERSION}-win-x64"
STAGE="$ROOT/build/stage"
mkdir -p "$CACHE" "$OUT_DIR"

need() { command -v "$1" >/dev/null 2>&1 || { echo "нет команды: $1" >&2; exit 1; }; }
need wine; need xvfb-run; need python3

echo "== Windows Python в $WINEPREFIX =="
if ! xvfb-run -a wine "$PY_WIN" -c "import sys" 2>/dev/null; then
  INSTALLER="$CACHE/python-3.12.10-amd64.exe"
  [ -f "$INSTALLER" ] || curl -fL --retry 3 -o "$INSTALLER" https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe
  [ -d "$WINEPREFIX/drive_c" ] || xvfb-run -a wineboot --init
  xvfb-run -a wine "$INSTALLER" /quiet InstallAllUsers=1 PrependPath=0 Include_test=0 Include_launcher=0 Include_pip=1 "TargetDir=C:\\Python312"
fi
xvfb-run -a wine "$PY_WIN" -m pip install -q --disable-pip-version-check -r requirements.txt "pyinstaller==6.11.1"

echo "== внешние программы (проверка SHA-256) =="
python3 scripts/fetch_tools.py "$CACHE" "$ROOT/build/tools"

echo "== PyInstaller =="
rm -rf "$ROOT/build/pyi" "$ROOT/dist/own-tube" "$STAGE"
cp own-tube.spec.in "$ROOT/own-tube.spec"
xvfb-run -a wine "$PY_WIN" -m PyInstaller --noconfirm --clean --workpath build/pyi --distpath dist own-tube.spec
test -f dist/own-tube/own-tube.exe

echo "== папка программы =="
mkdir -p "$STAGE/$PKG"
cp -r dist/own-tube/. "$STAGE/$PKG/"
mkdir -p "$STAGE/$PKG/tools"
cp build/tools/mpv.exe build/tools/ffmpeg.exe build/tools/deno.exe build/tools/SOURCES.txt build/tools/ffmpeg-LICENSE.txt "$STAGE/$PKG/tools/"
cp LICENSE "$STAGE/$PKG/LICENSE.txt"
cp docs/START.txt "$STAGE/$PKG/START.txt"
if find "$STAGE" \( -iname '*.db' -o -iname 'settings.json' -o -iname '*.key' -o \( -iname '*.pem' ! -path '*/certifi/*' \) -o -iname '.env*' \) | grep -q .; then
  echo "в сборку попали лишние файлы" >&2; exit 1
fi

echo "== самопроверка exe под Wine =="
xvfb-run -a wine "$STAGE/$PKG/own-tube.exe" --selftest "Z:$(echo "$ROOT/build/selftest.json" | tr / '\\')" || true
python3 - "$ROOT/build/selftest.json" << 'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
for k, v in d.items():
    print(f"{k}: {v if not isinstance(v, dict) else (v.get('result') if v.get('ok') else 'ОШИБКА ' + v.get('error', ''))}")
sys.exit(0 if d.get("ok") else 1)
PY

( cd "$STAGE/$PKG" && find . -type f ! -name SHA256SUMS.txt -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS.txt )
ZIP="$OUT_DIR/$PKG.zip"
rm -f "$ZIP"
( cd "$STAGE" && python3 -c "
import sys, zipfile, pathlib
root = pathlib.Path('$PKG')
with zipfile.ZipFile(sys.argv[1], 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for f in sorted(root.rglob('*')):
        if f.is_file(): z.write(f, f.as_posix())
" "$ZIP" )
( cd "$OUT_DIR" && sha256sum "$PKG.zip" > "$PKG.zip.sha256" && cat "$PKG.zip.sha256" )
echo "готово: $ZIP"
