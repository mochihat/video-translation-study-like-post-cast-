"""Quy trình: tải video YouTube -> nhận dạng giọng nói -> dịch sang tiếng Việt -> lồng tiếng -> ghép video."""
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

VOICES = {
    "female": "vi-VN-HoaiMyNeural",
    "male": "vi-VN-NamMinhNeural",
}

_whisper_cache = {}


def ffmpeg(*args):
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg lỗi: {r.stderr.strip()[-500:]}")


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
        return "YouTube đang tạm chặn tải video"
    if any(k in msg for k in ("name resolution", "failed to resolve", "network is unreachable",
                              "connection reset", "connection refused", "timed out", "no internet")):
        return "Mất kết nối mạng"
    return None


def download(url, workdir, log, browser=None):
    opts = {
        **cookie_opts(browser),
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
    log(f"Đã tải: {info.get('title')}")
    return video, info


# ---------- 2. Nhận dạng giọng nói ----------
def load_whisper(model_name, log):
    if model_name in _whisper_cache:
        return _whisper_cache[model_name]
    from faster_whisper import WhisperModel
    try:
        model = WhisperModel(model_name, device="cuda", compute_type="int8_float16")
        log(f"Đã nạp Whisper '{model_name}' trên GPU")
    except Exception as e:
        log(f"Không dùng được GPU ({e}), chuyển sang CPU (sẽ chậm hơn)")
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
    log(f"Ngôn ngữ phát hiện: {info.language} ({info.language_probability:.0%})")
    if info.language not in NLLB_LANGS:
        log("Cảnh báo: ngôn ngữ này ngoài Trung/Anh, kết quả dịch có thể kém")
    result = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            result.append({"start": seg.start, "end": seg.end, "text": text})
        progress(min(seg.end / max(info.duration, 1), 1.0))
    return merge_short(result), info.duration, info.language


def merge_short(segs, max_gap=0.4, max_len=10.0, min_len=1.2):
    """Gộp các câu quá ngắn liền nhau để giọng đọc tự nhiên hơn."""
    out = []
    for s in segs:
        if out:
            p = out[-1]
            short = (p["end"] - p["start"]) < min_len or (s["end"] - s["start"]) < min_len
            if short and s["start"] - p["end"] < max_gap and s["end"] - p["start"] < max_len:
                p["end"] = s["end"]
                p["text"] = p["text"] + " " + s["text"]
                continue
        out.append(dict(s))
    return out


# ---------- 3. Dịch ----------
NLLB_REPO = "JustFrederik/nllb-200-distilled-1.3B-ct2-int8"
NLLB_LANGS = {"zh": "zho_Hans", "en": "eng_Latn", "ja": "jpn_Jpan", "ko": "kor_Hang"}
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
        log("Đã nạp mô hình dịch offline NLLB")
    return _nllb["model"], _nllb["sp"]


def translate_offline(segments, lang, log, progress):
    model, sp = load_nllb(log)
    src = NLLB_LANGS.get(lang, "eng_Latn")
    out = []
    for i in range(0, len(segments), 16):
        batch = [s["text"] for s in segments[i:i + 16]]
        tokens = [[src] + sp.encode(t, out_type=str)[:400] + ["</s>"] for t in batch]
        res = model.translate_batch(tokens, target_prefix=[["vie_Latn"]] * len(batch),
                                    beam_size=4, max_decoding_length=300)
        out += [sp.decode(r.hypotheses[0][1:]) for r in res]
        progress(len(out) / len(segments))
    for s, vi in zip(segments, out):
        s["vi"] = vi
    return segments


def translate(segments, engine, lang, log, progress):
    if engine == "google":
        try:
            return translate_google(segments, log, progress)
        except Exception as e:
            log(f"Google Dịch không dùng được ({str(e)[:80]}), chuyển sang dịch offline")
    return translate_offline(segments, lang, log, progress)


def translate_google(segments, log, progress):
    tr = GoogleTranslator(source="auto", target="vi")
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
                    log(f"Lỗi dịch '{t[:30]}': {e}")
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
    async with sem:
        for attempt in range(3):
            try:
                await edge_tts.Communicate(text, voice).save(str(mp3_path))
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
        log(f"{sped_up}/{len(segments)} câu được đọc nhanh hơn để khớp thời gian")
    return dub


# ---------- 5. Ghép video ----------
def mux(video, dub, srt, bg_volume, out_path):
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
        "-c:s", "mov_text", "-metadata:s:s:0", "language=vie",
        "-shortest", str(out_path),
    )


# ---------- Toàn bộ quy trình ----------
STEPS = [
    ("download", "Tải video"),
    ("transcribe", "Nhận dạng giọng nói"),
    ("translate", "Dịch sang tiếng Việt"),
    ("tts", "Tạo giọng đọc tiếng Việt"),
    ("mux", "Ghép video"),
]


def run_pipeline(job, workdir: Path, update):
    """job: dict cấu hình. update(step=..., progress=..., log=...) báo tiến độ."""
    log = lambda m: update(log=m)

    # dọn file dở dang của lần chạy trước (khi thử lại hoặc máy tắt giữa chừng)
    for p in workdir.iterdir():
        if p.name != "job.json":
            shutil.rmtree(p) if p.is_dir() else p.unlink()

    update(step="download", progress=0)
    video, info = download(job["url"], workdir, log, job.get("cookies"))
    update(title=info.get("title"), video_id=info.get("id"), duration=info.get("duration"))

    update(step="transcribe", progress=0)
    audio16 = workdir / "audio16k.wav"
    ffmpeg("-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio16))
    segments, duration, lang = transcribe(
        audio16, job["model"], job["language"], log, lambda p: update(progress=p)
    )
    if not segments:
        raise RuntimeError("Không nhận dạng được lời nói nào trong video")
    log(f"Nhận dạng được {len(segments)} câu")

    update(step="translate", progress=0)
    segments = translate(segments, job.get("translator", "offline"), lang, log, lambda p: update(progress=p))
    (workdir / "segments.json").write_text(json.dumps(segments, ensure_ascii=False, indent=1), "utf-8")
    write_srt(workdir / "vi.srt", segments, "vi")
    write_srt(workdir / "original.srt", segments, "text")

    update(step="tts", progress=0)
    dub = synthesize(segments, VOICES[job["voice"]], duration, workdir, log, lambda p: update(progress=p))

    update(step="mux", progress=0)
    mux(video, dub, workdir / "vi.srt", job["bg_volume"], workdir / "dubbed.mp4")

    # chỉ giữ video thành phẩm, phụ đề và ảnh bìa để tiết kiệm ổ cứng
    for name in ("source.mp4", "audio16k.wav", "dub.wav"):
        (workdir / name).unlink(missing_ok=True)
    shutil.rmtree(workdir / "tts", ignore_errors=True)
    update(progress=1)
    log("Hoàn tất!")
