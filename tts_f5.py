"""Giọng đọc AI tự nhiên (F5-TTS tiếng Việt). Chạy trong môi trường riêng .venv-tts (cài bằng install-voice.sh):
    .venv-tts/bin/python tts_f5.py job.json
job.json: {"ref_audio": wav mẫu giọng, "ref_text": lời của wav mẫu,
           "items": [{"text": câu cần đọc, "out": file wav ra, "duration": số giây muốn đọc (có thể bỏ trống)}]}
Mỗi câu xong in một dòng "done <i>" để pipeline báo tiến độ; câu lỗi in "fail <i> <lý do>" rồi làm tiếp.
"""
import json
import re
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

REPO = "hynt/F5-TTS-Vietnamese-ViVoice"
SLIM = Path(__file__).parent / "voices" / "f5_vi_ema.safetensors"  # tạo bằng slim_f5.py
SPEECH_RATE = 3.3  # âm tiết mỗi giây khi đọc bình thường


def normalize(text):
    """Mô hình học từ chữ tiếng Việt viết thường, số viết bằng chữ."""
    from num2words import num2words

    text = text.replace("%", " phần trăm").replace("^", " mũ ").replace("&", " và ").replace("*", " sao")
    text = re.sub(r"(\d+)[.,](\d+)", lambda m: f"{m[1]} phẩy {m[2]}", text)
    text = re.sub(r"\d+", lambda m: num2words(int(m[0]), lang="vi"), text)
    text = re.sub(r"[\"“”()\[\]{}<>/\\|~`_#@=+]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def main():
    job = json.load(open(sys.argv[1], encoding="utf-8"))
    import soundfile as sf
    from f5_tts.api import F5TTS
    from huggingface_hub import hf_hub_download

    tts = F5TTS(model="F5TTS_Base", ckpt_file=str(SLIM),
                vocab_file=hf_hub_download(REPO, "config.json"), device="cuda")
    from f5_tts.infer.utils_infer import preprocess_ref_audio_text

    # F5 tự cắt khoảng lặng của giọng mẫu -> đo độ dài sau khi cắt để tính đúng thời lượng câu đọc ra
    ref_audio, ref_text = preprocess_ref_audio_text(job["ref_audio"], normalize(job["ref_text"]), show_info=lambda *a: None)
    ref_len = sf.info(ref_audio).duration
    print("ready", flush=True)
    for i, it in enumerate(job["items"]):
        t0 = time.time()
        try:
            text = normalize(it["text"])
            if not re.search(r"\w", text):
                print(f"done {i} 0", flush=True)
                continue
            fix = None
            if it.get("duration"):
                # độ dài đọc = vừa khoảng thời gian của câu, nhưng không nhanh hơn 1,4 lần / chậm hơn 0,9 lần bình thường
                natural = len(text.split()) / (SPEECH_RATE * it.get("speed", 1.0)) + 0.3  # speed: thanh trượt tốc độ
                fix = ref_len + min(max(it["duration"], natural / 1.4), natural / 0.9)
            wav, sr, _ = tts.infer(ref_audio, ref_text, text, show_info=lambda *a: None,
                                   nfe_step=job.get("nfe", 32), fix_duration=fix, remove_silence=False)
            sf.write(it["out"], wav, sr)
            print(f"done {i} {time.time() - t0:.2f}", flush=True)
        except Exception as e:  # một câu lỗi không làm hỏng cả video: pipeline dùng giọng Edge cho câu đó
            print(f"fail {i} {str(e)[:200]}", flush=True)


if __name__ == "__main__":
    main()
