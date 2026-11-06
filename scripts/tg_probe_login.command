#!/bin/zsh
set -u
cd "$(dirname "$0")/.." || exit 1
if [[ ! -x .venv/bin/python ]]; then
  print "Сначала установите приложение по инструкции README.md"
  read "reply?Нажмите Enter для закрытия: "
  exit 1
fi
print "Вход в Telegram для разведки каналов (этап 0.5)."
print "Скрипт спросит номер телефона (+7...); код придёт в Telegram; при 2FA — пароль."
print "Ключи берутся из .env (TG_API_ID/TG_API_HASH)."
.venv/bin/python scripts/tg_probe.py login
read "reply?Готово. Нажмите Enter для закрытия: "
