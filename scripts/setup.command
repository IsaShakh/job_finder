#!/bin/zsh
set -u
cd "$(dirname "$0")/.." || exit 1
if [[ ! -x .venv/bin/python ]]; then
  print "Сначала установите приложение по инструкции README.md"
  read "reply?Нажмите Enter для закрытия: "
  exit 1
fi
while true; do
  print "\nНастройка агента поиска вакансий"
  print "1 — Настройка ключей"
  print "2 — Привязать Telegram"
  print "3 — Войти в HH (API, без браузера)"
  print "4 — Войти в HH в браузере (запасной)"
  print "5 — Запустить агента"
  print "6 — Проверить настройки"
  print "0 — Закрыть"
  read "choice?Выберите действие: " || exit 0
  case "$choice" in
    1) .venv/bin/python -m job_finder configure ;;
    2) .venv/bin/python -m job_finder pair ;;
    3) .venv/bin/python -m job_finder api-login ;;
    4) .venv/bin/python -m job_finder login ;;
    5) .venv/bin/python -m job_finder run ;;
    6) .venv/bin/python -m job_finder doctor ;;
    0) exit 0 ;;
    *) print "Выберите номер из меню" ;;
  esac
done
