#!/data/data/com.termux/files/usr/bin/bash
# Запуск покер-сервера прямо в Termux на телефоне.
# Первый запуск поставит Python, если его ещё нет.

echo "Проверяю Python..."
pkg install -y python >/dev/null 2>&1

echo "Запускаю сервер..."
python server.py
