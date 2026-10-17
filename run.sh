#!/usr/bin/env bash
# Khởi động web lồng tiếng: ./run.sh rồi mở http://localhost:5000
cd "$(dirname "$0")"
SITE=$(.venv/bin/python -c "import site; print(site.getsitepackages()[0])")
export PATH="$PWD/.venv/bin:$PATH"  # deno cho yt-dlp
export LD_LIBRARY_PATH="$SITE/nvidia/cublas/lib:$SITE/nvidia/cudnn/lib:$LD_LIBRARY_PATH"
exec .venv/bin/python app.py
