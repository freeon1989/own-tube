# Скрипты и выпуск релиза

| Скрипт | Что делает |
|---|---|
| `build_windows.sh` | Сборка для Windows под Wine (PyInstaller onedir), `--selftest`, zip и `.sha256` |
| `build_awg_helper.sh` | Воспроизводимая сборка `tools\awgproxy.exe` (Go 1.27.3, amneziawg-go v3.1.20261116, `-trimpath`, без buildid) |
| `fetch_tools.py` | mpv, ffmpeg, deno по закреплённым адресам с проверкой SHA-256 |
| `scan_secrets.sh` | Поиск секретов в рабочей копии и истории; перед push должен напечатать `SCAN OK` |
| `awg_test_stand.py` | Тестовый сервер AmneziaWG на этой машине (`sudo python3 scripts/awg_test_stand.py up/down`), только тестовые ключи |
| `bench_awg_vs_socks.py` | Замер: встроенный VPN против SOCKS5 через тот же канал с задержкой |
| `shoot*.py` | Скриншоты интерфейса в Chromium без окна |

## Что обязательно в каждом релизе GitHub

Обновление внутри программы (`owntube/updater.py`) и ссылка «Скачать» в README рассчитаны на эти файлы.
Без них программа сообщит «В релизе нет архива own-tube-win-x64.zip или его .sha256» и ничего не поставит.

1. Тег `vX.Y.Z` (semver, как `__version__` в `owntube/__init__.py`), релиз помечен как последний (Latest), не черновик и не pre-release.
2. Файлы:
   - `own-tube-win-x64.zip` — постоянное имя: на него ведёт `releases/latest/download/own-tube-win-x64.zip`, его скачивает обновление;
   - `own-tube-win-x64.zip.sha256` — строка `<64 hex>  own-tube-win-x64.zip` (как выдаёт `sha256sum`);
   - `own-tube-X.Y.Z-win-x64.zip` и `own-tube-X.Y.Z-win-x64.zip.sha256` — тот же архив с номером версии.
3. Внутри zip — одна папка верхнего уровня с `own-tube.exe` (так собирает `build_windows.sh`).
4. Описание релиза — по-русски: его показывает окно «Доступна новая версия».

Все четыре файла создаёт `build_windows.sh` в `$OUT_DIR`. Публикация (например, `gh`):

```
gh release create vX.Y.Z --title "own-tube X.Y.Z" --notes-file notes.md --latest \
  own-tube-win-x64.zip own-tube-win-x64.zip.sha256 own-tube-X.Y.Z-win-x64.zip own-tube-X.Y.Z-win-x64.zip.sha256
curl -sI https://github.com/freeon1989/own-tube/releases/latest/download/own-tube-win-x64.zip | grep -i location
```

## Как ставится обновление

1. Один запрос `GET api.github.com/repos/freeon1989/own-tube/releases/latest` через маршрут программы (прокси или VPN). Если лимит API исчерпан — версия из переадресации `github.com/…/releases/latest`.
2. По кнопке: скачивание `.sha256`, затем zip (только https, только хосты GitHub, в том числе после переадресации), сверка SHA-256, распаковка с проверкой путей в `%LOCALAPPDATA%\own-tube\update\<версия>`.
3. «Обновить и перезапустить»: новая `own-tube.exe --apply-update <план>` запускается из папки подготовки, ждёт выхода программы, переименовывает папку программы в `own-tube.old-<прежняя версия>`, копирует новую, при ошибке возвращает прежнюю, запускает программу. Хранится одна резервная копия; «Вернуть …» в Настройки → О программе.

Ключ `--apply-update` и формат плана — договор между версиями: менять только совместимо.
