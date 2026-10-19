"""Quy trình: tải video YouTube -> nhận dạng giọng nói -> dịch sang tiếng Việt / tiếng Trung -> lồng tiếng -> ghép video."""
import asyncio
import json
import shutil
import subprocess
import wave
from pathlib import Path

import edge_tts
import imageio_ffmpeg
import numpy as np
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


def ffmpeg(*args):
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg error: {r.stderr.strip()[-500:]}")


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


def transcribe(audio_path, model_name, language, log, progress):
    model = load_whisper(model_name, log)
    segments, info = model.transcribe(
        read_wav(audio_path),  # truyền mảng 16kHz trực tiếp, tránh lỗi tương thích của thư viện av
        language=None if language == "auto" else language,
        vad_filter=True,
        beam_size=5,
        initial_prompt="以下是普通话的句子。" if language == "zh" else None,
    )
    if language == "auto":
        log(f"Detected language: {lang_name(info.language)} ({info.language_probability:.0%} confidence)")
    if info.language not in LANGS:
        log(f"Warning: '{info.language}' is not supported by the offline translator, translation may be poor")
    result = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            result.append({"start": seg.start, "end": seg.end, "text": text})
        progress(min(seg.end / max(info.duration, 1), 1.0))
    return merge_short(result, sep="" if info.language in NO_SPACES else " "), info.duration, info.language


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
        out += [sp.decode(r.hypotheses[0][1:]) for r in res]
        progress(len(out) / len(segments))
    for s, vi in zip(segments, out):
        s["vi"] = vi
    return segments


def translate(segments, engine, lang, target, log, progress):
    """Bản dịch lưu ở khoá "vi" (và file vi.srt) dù đầu ra là tiếng Trung, để video cũ vẫn mở được."""
    if lang == target:  # video vốn đã là ngôn ngữ đầu ra: chỉ đọc lại, không dịch
        log(f"Video is already in {TARGETS[target]['name']}, skipping translation")
        for s in segments:
            s["vi"] = s["text"]
        return segments
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


# ---------- 4. Lồng tiếng ----------
async def _tts_one(text, voice, mp3_path, sem):
    # câu chỉ có dấu câu/ký hiệu ("...", "♪") thì Edge TTS không đọc được -> bỏ qua
    if not any(c.isalnum() for c in text):
        return
    async with sem:
        for attempt in range(3):
            try:
                await edge_tts.Communicate(text, voice).save(str(mp3_path))
                return
            except edge_tts.exceptions.NoAudioReceived:
                mp3_path.unlink(missing_ok=True)  # câu này không đọc được, bỏ qua thay vì hỏng cả video
                return
            except Exception:
                if attempt == 2:
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

    track = np.zeros(int((total_duration + 30) * SR), dtype=np.float32)
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
        a = int(start * SR)
        if a + len(audio) > len(track):
            track = np.concatenate([track, np.zeros(a + len(audio) - len(track), dtype=np.float32)])
        track[a:a + len(audio)] += audio
        cursor = start + len(audio) / SR

    end = max(int(total_duration * SR), int(cursor * SR))
    dub = workdir / "dub.wav"
    write_wav(dub, track[:end])
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
    segments = translate(segments, job.get("translator", "offline"), lang, target, log, lambda p: update(progress=p))
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
