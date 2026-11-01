#!/usr/bin/env bash
# Cài bộ dịch AI chạy trên máy (Ollama + Qwen2.5 7B) cho lựa chọn "AI on this laptop (Qwen)".
# Chạy một lần:  bash install-llm.sh      (không cần sudo, cài vào ~/.local/ollama, tải khoảng 6 GB)
# Gỡ bỏ: systemctl --user disable --now ollama; rm -rf ~/.local/ollama ~/.ollama ~/.config/systemd/user/ollama.service
set -euo pipefail
VER=v0.40.5
DIR="$HOME/.local/ollama"
MODEL=qwen2.5:7b-instruct

if [ ! -x "$DIR/bin/ollama" ]; then
  echo "== Tải Ollama $VER =="
  mkdir -p "$DIR"
  curl -fL --progress-bar "https://github.com/ollama/ollama/releases/download/$VER/ollama-linux-amd64.tar.zst" \
    | tar --zstd -x -C "$DIR"
fi

echo "== Tạo dịch vụ chạy nền ollama (tự bật cùng máy) =="
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/ollama.service" <<EOF
[Unit]
Description=Ollama (AI translator for video dubbing)

[Service]
ExecStart=$DIR/bin/ollama serve
Environment=OLLAMA_HOST=127.0.0.1:11434
Restart=on-failure

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now ollama
until "$DIR/bin/ollama" list >/dev/null 2>&1; do sleep 1; done

echo "== Tải mô hình $MODEL (khoảng 4,7 GB) =="
"$DIR/bin/ollama" pull "$MODEL"
echo "Xong! Trên web chọn Translator: AI on this laptop (Qwen)."
