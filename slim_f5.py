"""Tạo bản mô hình F5 gọn (chỉ trọng số EMA, fp16) từ checkpoint huấn luyện 5,4 GB để vừa card 6 GB.
Chạy trong .venv-tts (install-voice.sh tự gọi):  .venv-tts/bin/python slim_f5.py"""
import torch
from huggingface_hub import hf_hub_download
from safetensors.torch import save_file

from tts_f5 import REPO, SLIM

ck = torch.load(hf_hub_download(REPO, "model_last.pt"), map_location="cpu", weights_only=True)
ema = {k: (v.half() if v.is_floating_point() else v).contiguous()
       for k, v in ck["ema_model_state_dict"].items() if k not in ("initted", "step")}
SLIM.parent.mkdir(exist_ok=True)
save_file(ema, str(SLIM))
print(f"Saved {SLIM} ({sum(v.numel() for v in ema.values()) / 1e6:.0f}M parameters)")
