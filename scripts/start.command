#!/bin/zsh
set -u
cd "$(dirname "$0")/.." || exit 1
if [[ ! -x .venv/bin/python ]]; then
  print "Сначала установите приложение по инструкции README.md"
  read "reply?Нажмите Enter для закрытия: "
  exit 1
fi
print "Запуск агента hh.ru. Чтобы остановить работу, нажмите Ctrl+C."
.venv/bin/python -m hh_agent run
agent_status=$?
if [[ $agent_status -ne 0 ]]; then
  print "Для настройки ключей и входа в HH откройте scripts/setup.command."
fi
read "reply?Работа завершена. Нажмите Enter для закрытия: "
exit $agent_status
