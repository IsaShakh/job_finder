#!/bin/zsh
set -u
cd "$(dirname "$0")/.." || exit 1
if [[ ! -x .venv/bin/python ]]; then
  print "Сначала установите приложение по инструкции README.md"
  read "reply?Нажмите Enter для закрытия: "
  exit 1
fi
print "Чтение 4 каналов и сохранение фикстур (этап 0.5)."
.venv/bin/python scripts/tg_probe.py read
read "reply?Готово. Нажмите Enter для закрытия: "
