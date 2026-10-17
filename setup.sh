#!/usr/bin/env bash
# Cài đặt môi trường (chỉ cần chạy 1 lần). Không cần sudo.
set -e
cd "$(dirname "$0")"
if ! command -v uv >/dev/null && [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV=$(command -v uv || echo "$HOME/.local/bin/uv")
"$UV" venv --python 3.12 .venv
"$UV" pip install --python .venv/bin/python -r requirements.txt
echo "Xong! Chạy ./run.sh rồi mở http://localhost:5000"
