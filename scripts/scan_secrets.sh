#!/bin/bash
# Проверка всей истории git перед push: адреса серверов, имена машин, пути пользователя, ключи, бинарники.
set -u
cd "$(dirname "$0")/.."
pat='82\.148\.|193\.233\.|(^|[^0-9.])2\.56\.[0-9]|83\.141\.|5\.180\.|193\.8\.|\bnako\b|\bdima\b|C:\\\\Users|C:/Users|/home/box|BEGIN [A-Z ]*PRIVATE KEY|gh[opsu]_[A-Za-z0-9]{20}|sk-[A-Za-z0-9]{20}|xai-[A-Za-z0-9]{20}|AIza[0-9A-Za-z_-]{30}|AKIA[0-9A-Z]{16}'
# password assignment in tests is dummy data; elsewhere it is forbidden
pat_pwd="password *= *[\"']"
bad=0
for c in $(git rev-list --all); do
  if git grep -I -n -i -E "$pat" "$c" -- . ':!scripts/scan_secrets.sh' 2>/dev/null; then bad=1; fi
  if git grep -I -n -i -E "$pat_pwd" "$c" -- . ':!scripts/scan_secrets.sh' ':!tests/' 2>/dev/null; then bad=1; fi
done
if git log --all --name-only --format= | sort -u | grep -iE '(^|/)\.venv/|\.pyc$|(^|/)(dist|build)/|\.(exe|dll|gguf|onnx|safetensors|7z|zip)$|settings\.json$|\.db$'; then bad=1; fi
big=$(git rev-list --objects --all | git cat-file --batch-check='%(objecttype) %(objectsize) %(rest)' | awk '$1=="blob" && $2>5000000')
[ -n "$big" ] && { echo "крупные файлы: $big"; bad=1; }
[ $bad = 0 ] && echo "SCAN OK" || { echo "SCAN FAILED"; exit 1; }
