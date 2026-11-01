"""Quy trình: tải video YouTube -> nhận dạng giọng nói -> dịch sang tiếng Việt / tiếng Trung -> lồng tiếng -> ghép video."""
import asyncio
import gc
import json
import os
import re
import shutil
import subprocess
import threading
import wave
from pathlib import Path

import edge_tts
import imageio_ffmpeg
import numpy as np
import requests
import yt_dlp
from deep_translator import GoogleTranslator

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
SR = 24000  # sample rate của track lồng tiếng

# Ngôn ngữ đầu ra: mã NLLB, mã Google Dịch, giọng Edge TTS, mã ngôn ngữ phụ đề trong file mp4
TARGETS = {
    "vi": {"name": "Vietnamese", "nllb": "vie_Latn", "google": "vi", "sub": "vie",
           "voices": {"female": "vi-VN-HoaiMyNeural", "male": "vi-VN-NamMinhNeural"}},
    "zh": {"name": "Chinese (Simplified)", "nllb": "zho_Hans", "google": "zh-CN", "sub": "chi",
           "voices": {"female": "zh-CN-XiaoxiaoNeural", "male": "zh-CN-YunxiNeural"}},
    "zh-TW": {"name": "Chinese (Traditional)", "nllb": "zho_Hant", "google": "zh-TW", "sub": "chi",
              "voices": {"female": "zh-TW-HsiaoChenNeural", "male": "zh-TW-YunJheNeural"}},
}

# Ngôn ngữ Whisper nhận dạng được -> (mã NLLB, tên). Ngôn ngữ ngoài bảng vẫn dịch, nhưng coi như tiếng Anh.
LANGS = {
    "en": ("eng_Latn", "English"), "zh": ("zho_Hans", "Chinese"), "yue": ("yue_Hant", "Cantonese"),
    "ja": ("jpn_Jpan", "Japanese"), "ko": ("kor_Hang", "Korean"), "vi": ("vie_Latn", "Vietnamese"),
    "th": ("tha_Thai", "Thai"), "id": ("ind_Latn", "Indonesian"), "ms": ("zsm_Latn", "Malay"),
    "tl": ("tgl_Latn", "Tagalog"), "km": ("khm_Khmr", "Khmer"), "lo": ("lao_Laoo", "Lao"),
    "my": ("mya_Mymr", "Burmese"), "hi": ("hin_Deva", "Hindi"), "bn": ("ben_Beng", "Bengali"),
    "ur": ("urd_Arab", "Urdu"), "ta": ("tam_Taml", "Tamil"), "te": ("tel_Telu", "Telugu"),
    "mr": ("mar_Deva", "Marathi"), "gu": ("guj_Gujr", "Gujarati"), "kn": ("kan_Knda", "Kannada"),
    "ml": ("mal_Mlym", "Malayalam"), "pa": ("pan_Guru", "Punjabi"), "ne": ("npi_Deva", "Nepali"),
    "si": ("sin_Sinh", "Sinhala"), "fr": ("fra_Latn", "French"), "de": ("deu_Latn", "German"),
    "es": ("spa_Latn", "Spanish"), "pt": ("por_Latn", "Portuguese"), "it": ("ita_Latn", "Italian"),
    "nl": ("nld_Latn", "Dutch"), "ru": ("rus_Cyrl", "Russian"), "uk": ("ukr_Cyrl", "Ukrainian"),
    "pl": ("pol_Latn", "Polish"), "cs": ("ces_Latn", "Czech"), "sk": ("slk_Latn", "Slovak"),
    "ro": ("ron_Latn", "Romanian"), "hu": ("hun_Latn", "Hungarian"), "bg": ("bul_Cyrl", "Bulgarian"),
    "el": ("ell_Grek", "Greek"), "tr": ("tur_Latn", "Turkish"), "ar": ("arb_Arab", "Arabic"),
    "he": ("heb_Hebr", "Hebrew"), "fa": ("pes_Arab", "Persian"), "sv": ("swe_Latn", "Swedish"),
    "da": ("dan_Latn", "Danish"), "no": ("nob_Latn", "Norwegian"), "fi": ("fin_Latn", "Finnish"),
    "is": ("isl_Latn", "Icelandic"), "et": ("est_Latn", "Estonian"), "lv": ("lvs_Latn", "Latvian"),
    "lt": ("lit_Latn", "Lithuanian"), "sl": ("slv_Latn", "Slovenian"), "hr": ("hrv_Latn", "Croatian"),
    "sr": ("srp_Cyrl", "Serbian"), "bs": ("bos_Latn", "Bosnian"), "mk": ("mkd_Cyrl", "Macedonian"),
    "sq": ("als_Latn", "Albanian"), "ca": ("cat_Latn", "Catalan"), "gl": ("glg_Latn", "Galician"),
    "eu": ("eus_Latn", "Basque"), "cy": ("cym_Latn", "Welsh"), "af": ("afr_Latn", "Afrikaans"),
    "sw": ("swh_Latn", "Swahili"), "am": ("amh_Ethi", "Amharic"), "yo": ("yor_Latn", "Yoruba"),
    "ha": ("hau_Latn", "Hausa"), "so": ("som_Latn", "Somali"), "sn": ("sna_Latn", "Shona"),
    "ln": ("lin_Latn", "Lingala"), "mg": ("plt_Latn", "Malagasy"), "ka": ("kat_Geor", "Georgian"),
    "hy": ("hye_Armn", "Armenian"), "az": ("azj_Latn", "Azerbaijani"), "kk": ("kaz_Cyrl", "Kazakh"),
    "uz": ("uzn_Latn", "Uzbek"), "tk": ("tuk_Latn", "Turkmen"), "tg": ("tgk_Cyrl", "Tajik"),
    "mn": ("khk_Cyrl", "Mongolian"), "be": ("bel_Cyrl", "Belarusian"), "tt": ("tat_Cyrl", "Tatar"),
    "ba": ("bak_Cyrl", "Bashkir"), "ps": ("pbt_Arab", "Pashto"), "sd": ("snd_Arab", "Sindhi"),
    "as": ("asm_Beng", "Assamese"), "bo": ("bod_Tibt", "Tibetan"), "jw": ("jav_Latn", "Javanese"),
    "su": ("sun_Latn", "Sundanese"), "mi": ("mri_Latn", "Maori"), "ht": ("hat_Latn", "Haitian Creole"),
    "lb": ("ltz_Latn", "Luxembourgish"), "mt": ("mlt_Latn", "Maltese"), "fo": ("fao_Latn", "Faroese"),
    "nn": ("nno_Latn", "Norwegian Nynorsk"), "oc": ("oci_Latn", "Occitan"), "yi": ("ydd_Hebr", "Yiddish"),
    "sa": ("san_Deva", "Sanskrit"),
}
NO_SPACES = {"zh", "yue", "ja", "th", "lo", "km", "my", "bo"}  # viết liền, gộp câu không chèn dấu cách


def lang_name(code):
    return LANGS.get(code, (None, code))[1]

_whisper_cache = {}


_running = {}  # luồng -> tiến trình ffmpeg đang chạy, để nút Restart dừng được ngay


def ffmpeg(*args):
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _running[threading.get_ident()] = p
    try:
        _, err = p.communicate()
    finally:
        _running.pop(threading.get_ident(), None)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg error: {err.strip()[-500:]}")


def kill_ffmpeg(thread_id):
    p = _running.get(thread_id)
    if p and p.poll() is None:
        p.kill()


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0


def write_wav(path, samples):
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def write_srt(path, segments, key):
    lines = []
    for i, seg in enumerate(segments, 1):
        lines += [str(i), f"{srt_time(seg['start'])} --> {srt_time(seg['end'])}", seg[key], ""]
    Path(path).write_text("\n".join(lines), encoding="utf-8")


# ---------- 1. Tải video ----------
def firefox_profiles():
    for p in (Path.home() / "snap/firefox/common/.mozilla/firefox", Path.home() / ".mozilla/firefox"):
        if p.is_dir():
            return str(p)
    return None


def cookie_opts(browser):
    """Dùng phiên đăng nhập YouTube trong trình duyệt khi YouTube đòi xác minh 'không phải bot'."""
    if browser == "firefox":
        return {"cookiesfrombrowser": ("firefox", firefox_profiles(), None, None)}
    if browser in ("chrome", "chromium"):
        return {"cookiesfrombrowser": (browser, None, None, None)}
    return {}


def transient_reason(err):
    """Lỗi tạm thời (bị YouTube chặn, mất mạng) thì nên chờ rồi thử lại thay vì bỏ video."""
    msg = str(err).lower()
    if any(k in msg for k in ("not a bot", "http error 429", "too many requests", "rate-limit")):
        return "YouTube is temporarily blocking downloads"
    if any(k in msg for k in ("name resolution", "failed to resolve", "network is unreachable",
                              "connection reset", "connection refused", "timed out", "no internet")):
        return "No network connection"
    return None


def download(url, workdir, log, browser=None, progress=None):
    last = [-1.0]

    def hook(d):
        if progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                p = min(d.get("downloaded_bytes", 0) / total, 1.0)
                if p - last[0] >= 0.01:  # chỉ báo khi tăng ≥1% để không ghi job.json liên tục
                    last[0] = p
                    progress(p)

    opts = {
        **cookie_opts(browser),
        "progress_hooks": [hook],
        # ưu tiên H.264 để mọi trình duyệt/điện thoại đều phát được
        "format": "bv*[height<=720][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=720][ext=mp4]+ba[ext=m4a]"
                  "/b[height<=720][ext=mp4]/bv*[height<=720]+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": {"default": str(workdir / "source.%(ext)s"), "thumbnail": str(workdir / "thumb.%(ext)s")},
        "writethumbnail": True,
        "ffmpeg_location": FFMPEG,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    video = next(p for p in workdir.glob("source.*") if p.suffix in (".mp4", ".mkv", ".webm"))
    if video.suffix != ".mp4":
        ffmpeg("-i", str(video), "-c:v", "libx264", "-c:a", "aac", str(workdir / "source.mp4"))
        video.unlink()
        video = workdir / "source.mp4"
    # ảnh bìa lưu trên máy để thư viện xem được cả khi không có mạng
    for thumb in workdir.glob("thumb.*"):
        try:
            ffmpeg("-i", str(thumb), "-vf", "scale=480:-2", "-frames:v", "1", str(workdir / "cover.jpg"))
        except RuntimeError:
            pass
        thumb.unlink()
    log(f"Downloaded: {info.get('title')}")
    return video, info


# ---------- 2. Nhận dạng giọng nói ----------
def load_whisper(model_name, log):
    if model_name in _whisper_cache:
        return _whisper_cache[model_name]
    from faster_whisper import WhisperModel
    try:
        model = WhisperModel(model_name, device="cuda", compute_type="int8_float16")
        log(f"Loaded Whisper '{model_name}' on GPU")
    except Exception as e:
        log(f"GPU unavailable ({e}), using CPU (slower)")
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
    _whisper_cache.clear()  # chỉ giữ 1 model trong VRAM
    _whisper_cache[model_name] = model
    return model


CHUNK_SEC = 20 * 60  # nhận dạng từng đoạn 20 phút -> RAM như nhau dù video dài 10 phút hay 6 tiếng
CUT_SEARCH_SEC = 30  # cắt đoạn ở chỗ im lặng nhất trong 30 giây cuối để không cắt ngang câu


def _wav_chunks(path, sr=16000):
    """Đọc file wav 16kHz theo từng đoạn ~20 phút: trả (thời điểm bắt đầu, mảng float32, tổng thời lượng)."""
    with wave.open(str(path), "rb") as w:
        total = w.getnframes()
        pos = 0
        while pos < total:
            n = min(CHUNK_SEC * sr, total - pos)
            w.setpos(pos)
            data = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
            if pos + n < total:
                # tìm khung 0,5 giây yên lặng nhất trong 30 giây cuối của đoạn và cắt ở giữa khung đó
                win, tail = sr // 2, data[-CUT_SEARCH_SEC * sr:]
                energy = [float(np.mean(tail[k:k + win] ** 2)) for k in range(0, len(tail) - win, win // 2)]
                cut = len(data) - len(tail) + int(np.argmin(energy)) * (win // 2) + win // 2
                data = data[:cut]
            yield pos / sr, data, total / sr
            pos += len(data)


def transcribe(audio_path, model_name, language, log, progress):
    model = load_whisper(model_name, log)
    result, lang = [], None if language == "auto" else language
    duration = 0.0
    for offset, audio, duration in _wav_chunks(audio_path):
        # truyền mảng 16kHz trực tiếp, tránh lỗi tương thích của thư viện av
        segments, info = model.transcribe(
            audio,
            language=lang,  # ngôn ngữ phát hiện ở đoạn đầu được dùng cho cả video
            vad_filter=True,
            beam_size=5,
            initial_prompt="以下是普通话的句子。" if lang == "zh" else None,
        )
        if lang is None:
            lang = info.language
            log(f"Detected language: {lang_name(lang)} ({info.language_probability:.0%} confidence)")
            if lang not in LANGS:
                log(f"Warning: '{lang}' is not supported by the offline translator, translation may be poor")
        if duration > CHUNK_SEC:
            log(f"Recognizing {fmt_hms(offset)}–{fmt_hms(offset + len(audio) / 16000)} of {fmt_hms(duration)}")
        for seg in segments:
            text = seg.text.strip()
            if text:
                result.append({"start": offset + seg.start, "end": offset + seg.end, "text": text})
            progress(min((offset + seg.end) / max(duration, 1), 1.0))
        del audio, segments
    return merge_short(result, sep="" if lang in NO_SPACES else " "), duration, lang


def fmt_hms(t):
    t = int(t)
    return f"{t // 3600}:{t // 60 % 60:02}:{t % 60:02}"


def merge_short(segs, sep=" ", max_gap=0.4, max_len=10.0, min_len=1.2):
    """Gộp các câu quá ngắn liền nhau để giọng đọc tự nhiên hơn."""
    out = []
    for s in segs:
        if out:
            p = out[-1]
            short = (p["end"] - p["start"]) < min_len or (s["end"] - s["start"]) < min_len
            if short and s["start"] - p["end"] < max_gap and s["end"] - p["start"] < max_len:
                p["end"] = s["end"]
                p["text"] = p["text"] + sep + s["text"]
                continue
        out.append(dict(s))
    return out


# ---------- 3. Dịch ----------
NLLB_REPO = "JustFrederik/nllb-200-distilled-1.3B-ct2-int8"
_nllb = {}


def load_nllb(log):
    if not _nllb:
        import ctranslate2
        import sentencepiece as spm
        from huggingface_hub import snapshot_download
        path = snapshot_download(NLLB_REPO)
        try:
            _nllb["model"] = ctranslate2.Translator(path, device="cuda", compute_type="int8_float16")
        except Exception:
            _nllb["model"] = ctranslate2.Translator(path, device="cpu", compute_type="int8")
        _nllb["sp"] = spm.SentencePieceProcessor(model_file=f"{path}/sentencepiece.bpe.model")
        log("Loaded offline translation model (NLLB)")
    return _nllb["model"], _nllb["sp"]


def translate_offline(segments, lang, target, log, progress):
    model, sp = load_nllb(log)
    src = LANGS.get(lang, ("eng_Latn",))[0]
    out = []
    for i in range(0, len(segments), 16):
        batch = [s["text"] for s in segments[i:i + 16]]
        tokens = [[src] + sp.encode(t, out_type=str)[:400] + ["</s>"] for t in batch]
        res = model.translate_batch(tokens, target_prefix=[[TARGETS[target]["nllb"]]] * len(batch),
                                    beam_size=4, max_decoding_length=300)
        # "⁇" = chữ mô hình không có trong từ điển -> bỏ đi thay vì hiện ra phụ đề
        out += [sp.decode(r.hypotheses[0][1:]).replace("⁇", "").strip() for r in res]
        progress(len(out) / len(segments))
    for s, vi in zip(segments, out):
        s["vi"] = vi
    return segments


def translate(segments, engine, lang, target, log, progress, title=""):
    """Bản dịch lưu ở khoá "vi" (và file vi.srt) dù đầu ra là tiếng Trung, để video cũ vẫn mở được."""
    if lang == target:  # video vốn đã là ngôn ngữ đầu ra: chỉ đọc lại, không dịch
        log(f"Video is already in {TARGETS[target]['name']}, skipping translation")
        for s in segments:
            s["vi"] = s["text"]
        return segments
    if engine == "llm":
        try:
            return translate_llm(segments, lang, target, log, progress, title)
        except LLMUnavailable as e:
            log(f"AI translator unavailable ({str(e)[:80]}), using offline translator")
    if engine == "google":
        try:
            return translate_google(segments, target, log, progress)
        except Exception as e:
            log(f"Google Translate unavailable ({str(e)[:80]}), using offline translator")
    return translate_offline(segments, lang, target, log, progress)


def translate_google(segments, target, log, progress):
    tr = GoogleTranslator(source="auto", target=TARGETS[target]["google"])
    tr.translate("hello")  # kiểm tra trước, ném lỗi nếu bị Google chặn
    texts = [s["text"] for s in segments]
    out = []
    chunk = []

    def flush():
        if not chunk:
            return
        joined = "\n".join(chunk)
        try:
            res = tr.translate(joined).split("\n")
        except Exception:
            res = []
        if len(res) != len(chunk):  # Google gộp/tách dòng -> dịch từng câu
            res = []
            for t in chunk:
                try:
                    res.append(tr.translate(t) or t)
                except Exception as e:
                    log(f"Translation error '{t[:30]}': {e}")
                    res.append(t)
        out.extend(r.strip() for r in res)
        chunk.clear()
        progress(len(out) / len(texts))

    size = 0
    for t in texts:
        if size + len(t) > 3500 or len(chunk) >= 40:
            flush()
            size = 0
        chunk.append(t.replace("\n", " "))
        size += len(t) + 1
    flush()
    for s, vi in zip(segments, out):
        s["vi"] = vi
    return segments


# Dịch bằng mô hình ngôn ngữ lớn chạy trên máy (Ollama + Qwen2.5 7B), cài bằng install-llm.sh.
# Khác NLLB (dịch từng câu riêng lẻ, sát chữ): mô hình đọc cả đoạn kèm câu trước đó nên hiểu ngữ cảnh,
# sửa lỗi nhận dạng giọng nói, giữ thuật ngữ chuyên ngành và viết câu tự nhiên như người bản xứ.
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen2.5:7b-instruct")
LLM_BATCH = 10  # số câu mỗi lần gọi
LLM_CONTEXT = 6  # số câu đã dịch ngay trước đó gửi kèm làm ngữ cảnh
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


class LLMUnavailable(Exception):
    pass


def _llm_prompt(src, target, title):
    tgt = TARGETS[target]["name"]
    style = {
        "vi": "- When the speaker says \"we\" while talking to the audience, translate it as \"chúng ta\" (never \"chúng tôi\"); "
              "\"you\" addressing the audience is \"các bạn\" or \"bạn\".\n",
        "zh": "- 使用简体中文。\n",
        "zh-TW": "- 使用繁體中文（台灣用語）。\n",
    }.get(target, "")
    return (
        f"You are a professional subtitle translator and dubbing script writer. "
        f"Translate the numbered lines of a video transcript from {src} into natural, fluent {tgt}, "
        f"the way a native speaker would actually say it.\n"
        f"Video title: {title or 'unknown'}\n"
        "Rules:\n"
        "- Translate the meaning in context, never word by word. Lines come from automatic speech recognition: "
        "silently fix obvious recognition mistakes (e.g. \"a star search\" = A* search, \"dextra's hand\" = dexterous hand), "
        "drop stutters, repeated words and fillers (um, uh, you know, basically).\n"
        "- Lines are timed subtitle cues. The translation for key N must contain ONLY what is said in line N, "
        "even when line N is an incomplete sentence fragment. Never move words to a neighbouring line, never merge, "
        "split or skip lines. Fragments should still read naturally when the lines are read one after another.\n"
        "- Keep the speaker's meaning exactly: do not add, correct or reinterpret content (left stays left, right stays right).\n"
        "- Be concise: each line is read aloud in the same time as the original.\n"
        f"- Use the standard {tgt} terminology of the field. When people in that field normally keep the English term "
        "(e.g. robot, GPU, A*, framework), keep it in English. Keep names, code, numbers and formulas unchanged.\n"
        f"{style}"
        f"- Use correct punctuation and capitalization. Write only in {tgt}; no notes or explanations.\n"
        'Reply with JSON mapping each line number to an object with "src" (line N copied exactly) and "tr" '
        '(its translation): {"1": {"src": "...", "tr": "..."}, "2": {"src": "...", "tr": "..."}, ...}'
    )


def _same(a, b):
    """Mô hình chép lại đúng câu gốc của dòng đó chưa (cho phép sai lệch nhỏ) -> phát hiện dịch lệch dòng."""
    import difflib
    norm = lambda x: re.sub(r"\W+", " ", x.lower()).strip()
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio() >= 0.8


def _llm_call(system, user, n, temperature, batch):
    keys = [str(k + 1) for k in range(n)]
    schema = {"type": "object", "required": keys, "additionalProperties": False,
              "properties": {k: {"type": "object", "required": ["src", "tr"], "additionalProperties": False,
                                 "properties": {"src": {"type": "string"}, "tr": {"type": "string"}}} for k in keys}}
    try:
        r = requests.post(f"{OLLAMA}/api/chat", timeout=600, json={
            "model": LLM_MODEL, "stream": False, "format": schema, "keep_alive": "5m",
            "options": {"temperature": temperature, "num_ctx": 4096, "num_predict": 3000},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        })
    except requests.RequestException as e:
        raise LLMUnavailable(f"cannot reach Ollama at {OLLAMA}") from e
    if r.status_code == 404:
        raise LLMUnavailable(f"model {LLM_MODEL} is not installed")
    r.raise_for_status()
    try:
        out = json.loads(r.json()["message"]["content"])
        out = [out[k] for k in keys]
        # dòng nào chép lại sai câu gốc là dòng bị lệch -> để trống, lát nữa dịch lại riêng
        return [str(o["tr"]).strip() if _same(o["src"], src) else "" for o, src in zip(out, batch)]
    except (ValueError, KeyError, TypeError):
        return None


def _llm_bad(line, target):
    # Qwen đôi khi chêm chữ Hán vào câu tiếng Việt
    return not line or (target == "vi" and CJK.search(line))


def translate_llm(segments, lang, target, log, progress, title=""):
    # nhường bộ nhớ GPU (6 GB) cho mô hình dịch: bỏ Whisper/NLLB khỏi VRAM, lúc sau tự nạp lại khi cần
    _whisper_cache.clear()
    _nllb.clear()
    gc.collect()
    system = _llm_prompt(lang_name(lang), target, title)
    log(f"Translating with AI ({LLM_MODEL}) in context")
    texts = [s["text"] for s in segments]
    out, fallback = [], []
    try:
        for i in range(0, len(texts), LLM_BATCH):
            batch = texts[i:i + LLM_BATCH]
            ctx = "".join(f"{texts[k]}\n=> {out[k]}\n" for k in range(max(0, i - LLM_CONTEXT), i))
            user = (f"Previous lines (context only, already translated, do not output):\n{ctx}\n" if ctx else "") + \
                "Lines to translate:\n" + "\n".join(f"[{k + 1}] {t}" for k, t in enumerate(batch))
            res = None
            for temperature in (0.2, 0.5):  # lần 2 đổi nhiệt độ để mô hình trả lời khác đi
                res = _llm_call(system, user, len(batch), temperature, batch)
                if res and not any(_llm_bad(x, target) for x in res):
                    break
            if res is None:
                res = [""] * len(batch)
            for k, x in enumerate(res):
                if _llm_bad(x, target):
                    fallback.append(i + k)
            out += res
            progress(len(out) / len(texts))
    finally:
        try:  # trả lại VRAM cho Whisper của video kế tiếp
            requests.post(f"{OLLAMA}/api/generate", json={"model": LLM_MODEL, "keep_alive": 0}, timeout=30)
        except requests.RequestException:
            pass
    if fallback:  # câu mô hình dịch hỏng thì dịch lại bằng NLLB
        log(f"{len(fallback)} lines were translated with the offline translator instead")
        redo = translate_offline([{"text": texts[k]} for k in fallback], lang, target, log, lambda p: None)
        for k, r in zip(fallback, redo):
            out[k] = r["vi"]
    for s, vi in zip(segments, out):
        s["vi"] = vi
    return segments


# ---------- 4. Lồng tiếng ----------
async def _tts_one(text, voice, mp3_path, sem):
    # câu chỉ có dấu câu/ký hiệu ("...", "♪") thì Edge TTS không đọc được -> bỏ qua
    if not any(c.isalnum() for c in text):
        return
    async with sem:
        for attempt in range(3):
            try:
                # kết nối có lúc treo mãi không trả lời (không lỗi, không ngắt) -> giới hạn 60 giây mỗi câu
                await asyncio.wait_for(edge_tts.Communicate(text, voice).save(str(mp3_path)), 60)
                return
            except edge_tts.exceptions.NoAudioReceived:
                mp3_path.unlink(missing_ok=True)  # câu này không đọc được, bỏ qua thay vì hỏng cả video
                return
            except Exception as e:
                mp3_path.unlink(missing_ok=True)
                if attempt == 2:
                    if isinstance(e, asyncio.TimeoutError):  # "timed out" -> được coi là lỗi mạng tạm thời, chờ rồi thử lại
                        raise RuntimeError("Voice service (Edge TTS) timed out") from e
                    raise
                await asyncio.sleep(2)


async def _tts_all(segments, voice, tts_dir, progress):
    sem = asyncio.Semaphore(6)
    done = 0

    async def run(i, seg):
        nonlocal done
        await _tts_one(seg["vi"], voice, tts_dir / f"{i:05}.mp3", sem)
        done += 1
        progress(done / len(segments))

    await asyncio.gather(*(run(i, s) for i, s in enumerate(segments) if s["vi"]))


def synthesize(segments, voice, total_duration, workdir, log, progress):
    tts_dir = workdir / "tts"
    tts_dir.mkdir(exist_ok=True)
    asyncio.run(_tts_all(segments, voice, tts_dir, progress))

    # ghi thẳng ra file theo thứ tự thời gian thay vì giữ cả track trong RAM
    # (video 6 tiếng = 500 triệu mẫu, giữ trong RAM tốn tới ~8 GB khi lưu)
    dub = workdir / "dub.wav"
    with wave.open(str(dub), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SR)
        written = 0  # số mẫu đã ghi

        def pad_to(n):
            nonlocal written
            while written < n:
                k = min(n - written, SR * 60)
                out.writeframes(np.zeros(k, dtype=np.int16).tobytes())
                written += k

        cursor = 0.0  # thời điểm câu trước kết thúc
        sped_up = 0
        for i, seg in enumerate(segments):
            mp3 = tts_dir / f"{i:05}.mp3"
            if not mp3.exists():
                continue
            wav_path = tts_dir / f"{i:05}.wav"
            ffmpeg("-i", str(mp3), "-ac", "1", "-ar", str(SR), str(wav_path))
            audio = read_wav(wav_path)
            dur = len(audio) / SR

            # Khoảng thời gian được phép nói: tới khi câu kế tiếp bắt đầu
            next_start = segments[i + 1]["start"] if i + 1 < len(segments) else total_duration
            slot = max(next_start - seg["start"], 0.5)
            if dur > slot * 1.05:
                tempo = min(dur / slot, 1.5)  # tăng tốc tối đa 1.5x để còn nghe rõ
                fast = tts_dir / f"{i:05}_fast.wav"
                ffmpeg("-i", str(wav_path), "-filter:a", f"atempo={tempo:.3f}", "-ar", str(SR), str(fast))
                audio = read_wav(fast)
                sped_up += 1

            start = max(seg["start"], cursor)
            pad_to(int(start * SR))  # các câu không chồng lên nhau nên chỉ cần nối tiếp
            out.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
            written += len(audio)
            cursor = written / SR
            for f in (wav_path, tts_dir / f"{i:05}_fast.wav"):
                f.unlink(missing_ok=True)

        pad_to(int(total_duration * SR))
    if sped_up:
        log(f"{sped_up}/{len(segments)} lines were read faster to fit the timing")
    return dub


# ---------- 5. Ghép video ----------
def mux(video, dub, srt, bg_volume, out_path, sub_lang="vie"):
    filt = (
        f"[0:a]volume={bg_volume}[bg];"
        "[1:a]volume=1.4[vo];"
        "[bg][vo]amix=inputs=2:duration=longest:normalize=0[a]"
    )
    ffmpeg(
        "-i", str(video), "-i", str(dub), "-i", str(srt),
        "-filter_complex", filt,
        "-map", "0:v", "-map", "[a]", "-map", "2:s",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-c:s", "mov_text", "-metadata:s:s:0", f"language={sub_lang}",
        "-shortest", str(out_path),
    )


# ---------- Toàn bộ quy trình ----------
STEPS = [
    ("download", "Download video"),
    ("transcribe", "Speech recognition"),
    ("translate", "Translate"),
    ("tts", "Generate voice-over"),
    ("mux", "Combine video"),
]


def run_pipeline(job, workdir: Path, update):
    """job: dict cấu hình. update(step=..., progress=..., log=...) báo tiến độ."""
    log = lambda m: update(log=m)

    # dọn file dở dang của lần chạy trước (khi thử lại hoặc máy tắt giữa chừng)
    for p in workdir.iterdir():
        if p.name != "job.json":
            shutil.rmtree(p) if p.is_dir() else p.unlink()

    update(step="download", progress=0)
    video, info = download(job["url"], workdir, log, job.get("cookies"), lambda p: update(progress=p))
    update(title=info.get("title"), video_id=info.get("id"), duration=info.get("duration"))

    update(step="transcribe", progress=0)
    audio16 = workdir / "audio16k.wav"
    ffmpeg("-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio16))
    segments, duration, lang = transcribe(
        audio16, job["model"], job["language"], log, lambda p: update(progress=p)
    )
    if not segments:
        raise RuntimeError("No speech was recognized in this video")
    update(source_lang=lang)
    log(f"Recognized {len(segments)} lines")
    target = job.get("target", "vi")

    update(step="translate", progress=0)
    segments = translate(segments, job.get("translator", "offline"), lang, target, log, lambda p: update(progress=p),
                         info.get("title", ""))
    (workdir / "segments.json").write_text(json.dumps(segments, ensure_ascii=False, indent=1), "utf-8")
    write_srt(workdir / "vi.srt", segments, "vi")
    write_srt(workdir / "original.srt", segments, "text")

    update(step="tts", progress=0)
    dub = synthesize(segments, TARGETS[target]["voices"][job["voice"]], duration, workdir, log, lambda p: update(progress=p))

    update(step="mux", progress=0)
    mux(video, dub, workdir / "vi.srt", job["bg_volume"], workdir / "dubbed.mp4", TARGETS[target]["sub"])

    # chỉ giữ video thành phẩm, phụ đề và ảnh bìa để tiết kiệm ổ cứng
    for name in ("source.mp4", "audio16k.wav", "dub.wav"):
        (workdir / name).unlink(missing_ok=True)
    shutil.rmtree(workdir / "tts", ignore_errors=True)
    update(progress=1)
    log("Done!")
