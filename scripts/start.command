#!/bin/zsh
set -u
cd "$(dirname "$0")/.." || exit 1
if [[ ! -x .venv/bin/python ]]; then
  print "Сначала установите приложение по инструкции README.md"
  read "reply?Нажмите Enter для закрытия: "
  exit 1
fi
while true; do
  print "\nАгент hh.ru"
  print "1 — Настройка ключей"
  print "2 — Привязать Telegram"
  print "3 — Войти в HH"
  print "4 — Запустить агента"
  print "5 — Проверить настройки"
  print "0 — Закрыть"
  read "choice?Выберите действие: "
  case "$choice" in
    1) .venv/bin/python -m hh_agent configure ;;
    2) .venv/bin/python -m hh_agent pair ;;
    3) .venv/bin/python -m hh_agent login ;;
    4) .venv/bin/python -m hh_agent run ;;
    5) .venv/bin/python -m hh_agent doctor ;;
    0) exit 0 ;;
    *) print "Выберите номер из меню" ;;
  esac
done
