#!/usr/bin/env bash
# Cài giọng đọc AI tự nhiên (F5-TTS tiếng Việt, mô hình hynt/F5-TTS-Vietnamese-ViVoice) cho lựa chọn
# "Voice engine: Natural AI voice". Chạy một lần:  bash install-voice.sh   (không cần sudo, tải khoảng 4 GB)
# Cài vào môi trường riêng .venv-tts để PyTorch không đụng tới thư viện của phần nhận dạng giọng nói.
# Gỡ bỏ: rm -rf .venv-tts
# Mô hình theo giấy phép CC-BY-NC-SA-4.0: chỉ dùng phi thương mại (học tập, cá nhân).
set -euo pipefail
cd "$(dirname "$0")"
UV=$(command -v uv || echo "$HOME/.local/bin/uv")

echo "== Tạo môi trường .venv-tts =="
"$UV" venv --python 3.12 .venv-tts
echo "== Cài PyTorch (CUDA) =="
"$UV" pip install --python .venv-tts/bin/python torch==2.5.1 torchaudio==2.5.1 \
  --index-url https://download.pytorch.org/whl/cu124
echo "== Cài F5-TTS =="
"$UV" pip install --python .venv-tts/bin/python "f5-tts==1.1.20" num2words "torch==2.5.1" "torchaudio==2.5.1" \
  --extra-index-url https://download.pytorch.org/whl/cu124 --index-strategy unsafe-best-match
echo "== Tải mô hình giọng tiếng Việt =="
.venv-tts/bin/python -c "
from huggingface_hub import hf_hub_download as d
for f in ('model_last.pt', 'config.json'):
    print(d('hynt/F5-TTS-Vietnamese-ViVoice', f))
from vocos import Vocos
Vocos.from_pretrained('charactr/vocos-mel-24khz')
"
echo "Xong! Trên web chọn Voice engine: Natural AI voice."
