#!/usr/bin/env bash
# Запуск дашборда prof. Открой потом http://127.0.0.1:7777
cd "$(dirname "$0")"
exec .venv/bin/python app.py
