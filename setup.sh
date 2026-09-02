#!/usr/bin/env bash
# Разворачивание после git clone: проверяет инструменты, ставит недостающее,
# создаёт локальные конфиги из примеров и файлы для загрузки.
#
#   ./setup.sh              обычная установка
#   ./setup.sh --no-install только проверить и создать конфиги, ничего не ставить
set -u

cd "$(dirname "$0")"
NO_INSTALL=0
[ "${1:-}" = "--no-install" ] && NO_INSTALL=1

ok(){ printf '  \033[32m✓\033[0m %s\n' "$*"; }
no(){ printf '  \033[31m✗\033[0m %s\n' "$*"; }
hm(){ printf '  \033[33m•\033[0m %s\n' "$*"; }
sec(){ printf '\n\033[1m%s\033[0m\n' "$*"; }

FAIL=0

sec "1. Python"
if command -v python3 >/dev/null; then
  ok "python3 $(python3 -V 2>&1 | cut -d' ' -f2) — сторонних пакетов не нужно"
else
  no "нет python3 — на нём написаны запуск, отчёты и морда"; FAIL=1
fi

sec "2. k6 — сам генератор нагрузки"
if command -v k6 >/dev/null; then
  ok "k6 $(k6 version 2>/dev/null | head -1)"
elif [ "$NO_INSTALL" = 1 ]; then
  no "k6 не установлен"; FAIL=1
elif command -v brew >/dev/null; then
  hm "ставлю через brew…"; brew install k6 && ok "k6 установлен" || { no "brew не справился"; FAIL=1; }
elif command -v apt-get >/dev/null; then
  no "k6 не установлен. Поставь его так (нужен sudo):"
  echo "      sudo gpg -k && sudo gpg --no-default-keyring --keyring /usr/share/keyrings/k6-archive-keyring.gpg --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D69"
  echo "      echo 'deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main' | sudo tee /etc/apt/sources.list.d/k6.list"
  echo "      sudo apt-get update && sudo apt-get install k6"
  FAIL=1
else
  no "k6 не установлен — возьми бинарник со страницы релизов grafana/k6"; FAIL=1
fi

sec "3. Node и рекордер (запись сценариев браузером)"
if command -v node >/dev/null; then
  ok "node $(node -v)"
  if [ "$NO_INSTALL" = 1 ]; then
    hm "пропускаю npm install (--no-install)"
  elif [ -d recorder/node_modules/playwright ]; then
    ok "playwright уже стоит"
  else
    hm "ставлю playwright в recorder/…"
    (cd recorder && npm install --silent) && ok "playwright установлен" || no "npm install не прошёл"
    hm "качаю браузер для записи…"
    (cd recorder && npx --yes playwright install chromium) >/dev/null 2>&1 \
      && ok "chromium готов" || hm "chromium не встал — запись сценариев работать не будет"
  fi
else
  hm "нет node — всё, кроме записи сценариев браузером, работает и без него"
fi

sec "4. Локальные настройки"
[ -f .env ] && ok ".env уже есть — не трогаю" || { cp .env.example .env && ok ".env создан из .env.example"; }
for f in accounts users roles pool requests; do
  if [ -f "$f.json" ]; then ok "$f.json уже есть"
  else cp "$f.example.json" "$f.json" && ok "$f.json создан из примера"; fi
done
mkdir -p har results recorded api && ok "каталоги har/ results/ recorded/ api/ на месте"

sec "5. Файлы для загрузки"
python3 mocks/make_fixtures.py | sed 's/^/  /'

sec "Дальше"
if [ "$FAIL" = 1 ]; then
  echo "  Сначала доставь то, что отмечено ✗."
else
  echo "  1) адрес стенда, вход и профили:  \$EDITOR .env"
  echo "  2) подними морду:                 ./start.sh   →  http://127.0.0.1:${WEB_PORT:-5057}"
  echo "  3) вкладка «Отчёты» → «Записи браузера»: запиши сессию и собери из неё"
  echo "     сценарий. Готовых сценариев в поставке нет — каждый ходит по своему сайту."
fi
exit "$FAIL"
