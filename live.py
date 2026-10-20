"""Dịch livestream: phụ đề trực tiếp, lồng tiếng trực tiếp (trễ ~30–60 giây), và khi live kết thúc
thì đưa bản ghi đầy đủ vào hàng đợi lồng tiếng như video thường.

Luồng: ffmpeg đọc HLS của buổi live và cắt thành từng đoạn ~12 giây. Mỗi đoạn: nhận dạng giọng nói -> dịch
-> đọc lại -> ghép với hình gốc -> nối vào playlist HLS riêng (live.m3u8) để trình duyệt phát.
Mọi đoạn nằm trên một trục thời gian chung (giây tính từ đầu phiên) để phụ đề khớp với hình.
"""
import json
import shutil
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import av
import numpy as np
import yt_dlp

import pipeline as P

BASE = Path(__file__).parent
CHUNK = 12                 # độ dài mỗi đoạn (giây)
TARGET_DURATION = CHUNK + 8  # đoạn được cắt ở keyframe nên có thể dài hơn CHUNK một chút
WINDOW = 150               # giữ ~30 phút gần nhất để tua lại, xoá đoạn cũ hơn cho đỡ tốn ổ
VOD_WAIT = 6 * 3600        # chờ YouTube xử lý xong bản ghi tối đa 6 giờ
MODEL = "large-v3-turbo"

session = None             # chỉ dịch một buổi live mỗi lúc
_lock = threading.Lock()


def busy():
    """Đang dịch live -> hàng đợi video thường tạm dừng để nhường card đồ hoạ."""
    s = session
    return bool(s and s.status in ("connecting", "live"))


def start(url, settings, on_ended):
    global session
    with _lock:
        if busy():
            raise RuntimeError("A livestream is already being translated — stop it first")
        session = Live(url, settings, on_ended)
        return session


def _quiet(msg):
    print(f"  [live] {msg}", flush=True)


def _decode(path, rate):
    """Giải mã tiếng thành mảng mono float ở tần số rate (bằng PyAV, không dùng ffmpeg đi kèm)."""
    out = []
    with av.open(str(path)) as c:
        rs = av.AudioResampler(format="flt", layout="mono", rate=rate)
        for frame in c.decode(audio=0):
            out += [r.to_ndarray().reshape(-1) for r in rs.resample(frame)]
        out += [r.to_ndarray().reshape(-1) for r in rs.resample(None)]
    return np.concatenate(out).astype(np.float32) if out else np.zeros(0, dtype=np.float32)


def _video_only(src, dst):
    """Tách riêng hình (không giải mã) từ file TS có cả hình lẫn tiếng."""
    with av.open(str(src)) as i, av.open(str(dst), "w", format="mpegts") as o:
        vin = i.streams.video[0]
        vout = o.add_stream_from_template(vin)
        for pkt in i.demux(vin):
            if pkt.dts is None:
                continue
            pkt.stream = vout
            o.mux(pkt)


class Live:
    def __init__(self, url, settings, on_ended):
        self.id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.url, self.settings, self.on_ended = url, settings, on_ended
        self.status = "connecting"   # connecting | live | ended | stopped | error
        self.error = None
        self.title = self.video_id = self.lang = None
        self.vod = None              # None | waiting | queued | gave_up
        self.lines = []              # {start, end, text, tr} trên trục thời gian chung
        self.out = []                # [(số thứ tự, độ dài)] các đoạn đã lồng tiếng xong
        self.t = 0.0                 # độ dài đã xử lý = thời điểm bắt đầu của đoạn kế tiếp
        self.epoch0 = time.time()    # mốc giờ thật của t = 0 (ghi vào EXT-X-PROGRAM-DATE-TIME)
        self.lag = None              # đoạn mới nhất xong sau bao lâu kể từ lúc nó được cắt
        self.next_src = 0
        self.stop_flag = threading.Event()
        self.dir = BASE / "tmp" / "live" / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self.run, daemon=True).start()

    # ---------- điều khiển ----------
    def stop(self):
        self.stop_flag.set()
        if self.status in ("connecting", "live"):
            self.status = "stopped"  # báo ngay; đoạn đang xử lý dở sẽ bị bỏ

    def state(self, since=0):
        return {"id": self.id, "url": self.url, "status": self.status, "error": self.error, "title": self.title,
                "video_id": self.video_id, "lang": self.lang, "target": self.settings["target"],
                "dub_after": self.settings.get("dub_after", True), "vod": self.vod, "epoch0": self.epoch0,
                "lag": self.lag, "chunk": CHUNK, "segments": len(self.out), "total": len(self.lines),
                "lines": self.lines[since:]}

    # ---------- vòng chính ----------
    def run(self):
        try:
            ended = self._loop()
            if self.stop_flag.is_set():
                self.status = "stopped"
            elif ended:
                self.status = "ended"
            self._write_playlist(final=True)
            if ended and not self.stop_flag.is_set() and self.settings.get("dub_after", True):
                self._wait_vod_and_queue()
        except Exception as e:
            traceback.print_exc()
            self.status, self.error = "error", str(e)
            self._write_playlist(final=True)

    def _probe(self):
        opts = {"quiet": True, "no_warnings": True,
                # tuỳ lúc YouTube chỉ có hình và tiếng tách riêng -> lấy hai luồng rồi ghép
                "format": "bv*[height<=720][protocol^=m3u8]+ba[protocol^=m3u8]/b[height<=720][protocol^=m3u8]/b[protocol^=m3u8]/b",
                "ffmpeg_location": P.FFMPEG,
                **P.cookie_opts(self.settings.get("cookies"))}
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(self.url, download=False)
        except yt_dlp.utils.DownloadError as e:
            if "will begin" in str(e) or "premieres in" in str(e).lower():
                raise RuntimeError("This livestream has not started yet — start again when it is live") from e
            raise
        self.title, self.video_id = info.get("title"), info.get("id")
        return info

    def _loop(self):
        """Đọc live tới khi hết; mất kết nối / URL hết hạn thì lấy URL mới và đọc tiếp. Trả về True nếu live đã kết thúc."""
        fails = 0
        while not self.stop_flag.is_set():
            try:
                info = self._probe()
                fails = 0
            except RuntimeError:
                raise
            except Exception as e:
                fails += 1
                if fails > 10 or not P.transient_reason(e) and not self.out:
                    raise
                _quiet(f"probe failed ({e}), retrying")
                time.sleep(10)
                continue
            if info.get("live_status") != "is_live":
                if not self.out:
                    raise RuntimeError("This video is not a livestream — paste it in the main box instead")
                return True
            self.status = "live"
            self._ingest(info)
            if not self.stop_flag.is_set():
                time.sleep(3)  # mạng rớt / link hết hạn / live có thể đã hết: nghỉ chút rồi hỏi lại YouTube
        return False

    def _ingest(self, info):
        """Tự tải HLS của buổi live bằng Python (ffmpeg tĩnh đi kèm bị crash khi tự mở link https),
        gom đủ ~CHUNK giây thì xử lý. Trả về khi live kết thúc, mạng lỗi hoặc link hết hạn."""
        fmts = info.get("requested_formats") or [info]
        heads = [f.get("http_headers") or info.get("http_headers") or {} for f in fmts]
        nxt, batch, batch_dur, idle_since = None, [], 0.0, time.time()
        while not self.stop_flag.is_set():
            try:
                lists = [self._playlist(f["url"], h) for f, h in zip(fmts, heads)]
            except Exception as e:
                _quiet(f"playlist failed: {e}")
                return
            # hình và tiếng tách riêng dùng chung số thứ tự đoạn (sq)
            common = sorted(set.intersection(*(set(segs) for segs, _ in lists)))
            if common and (nxt is None or nxt < common[0]):
                nxt = common[-1]  # bắt đầu sát live
            progressed = False
            while nxt is not None and nxt in common and not self.stop_flag.is_set():
                try:
                    parts = [self._get(segs[nxt][1], h) for (segs, _), h in zip(lists, heads)]
                except Exception as e:
                    _quiet(f"segment {nxt} failed: {e}")
                    return
                batch.append(parts)
                batch_dur += lists[0][0][nxt][0]
                nxt += 1
                progressed = True
                if batch_dur >= CHUNK:
                    self._process_batch(batch, batch_dur)
                    batch, batch_dur = [], 0.0
            if any(ended for _, ended in lists) and (not common or nxt > common[-1]):
                break  # có #EXT-X-ENDLIST: live đã hết
            if progressed:
                idle_since = time.time()
            elif time.time() - idle_since > 60:
                break  # 1 phút không có đoạn mới -> hỏi lại YouTube xem live còn không
            time.sleep(2)
        if batch and not self.stop_flag.is_set():
            self._process_batch(batch, batch_dur)

    @staticmethod
    def _http(url, headers):
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()

    def _playlist(self, url, headers):
        """{sq: (độ dài, url)} và đã kết thúc hay chưa."""
        text = self._http(url, headers).decode("utf-8", "replace")
        segs, sq, dur = {}, 0, None
        for line in text.splitlines():
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                sq = int(line.split(":", 1)[1])
            elif line.startswith("#EXTINF:"):
                dur = float(line[8:].split(",")[0])
            elif line and not line.startswith("#") and dur is not None:
                segs[sq] = (dur, urllib.parse.urljoin(url, line))
                sq, dur = sq + 1, None
        return segs, "#EXT-X-ENDLIST" in text

    def _get(self, url, headers):
        for attempt in range(3):
            try:
                return self._http(url, headers)
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(1)

    def _process_batch(self, batch, dur_hint):
        """Nối các mảnh 5 giây thành một đoạn rồi xử lý. Lỗi ở một đoạn thì bỏ đoạn đó, live vẫn chạy tiếp."""
        seq = self.next_src
        self.next_src += 1
        work = self.dir / f"w_{seq:06d}"
        work.mkdir(exist_ok=True)
        try:
            # ffmpeg đi kèm bị crash khi đọc file TS có tiếng AAC -> hình và tiếng luôn để ở hai file riêng,
            # tiếng thì giải mã bằng PyAV
            if len(batch[0]) == 2:
                (work / "v.ts").write_bytes(b"".join(p[0] for p in batch))
                (work / "a.aac").write_bytes(b"".join(p[1] for p in batch))
                audio_src = work / "a.aac"
            else:
                audio_src = work / "av.ts"
                audio_src.write_bytes(b"".join(p[0] for p in batch))
                _video_only(audio_src, work / "v.ts")
            self._process(seq, work, audio_src, dur_hint)
        except Exception as e:
            traceback.print_exc()
            _quiet(f"segment {seq} failed: {e}")
            self.t += dur_hint
        finally:
            shutil.rmtree(work, ignore_errors=True)
            self._trim()

    def _process(self, seq, work, audio_src, dur_hint):
        cut_at = time.time()
        bg = _decode(audio_src, P.SR)
        dur = len(bg) / P.SR or dur_hint
        segs, chunk_lang = self._transcribe(_decode(audio_src, 16000))
        target = self.settings["target"]
        if segs:
            P.translate(segs, self.settings.get("translator", "offline"), self.lang or chunk_lang, target, _quiet, lambda p: None)
        t0 = self.t
        self.lines += [{"start": round(t0 + s["start"], 2), "end": round(t0 + s["end"], 2),
                        "text": s["text"], "tr": s["vi"]} for s in segs if s.get("vi")]
        mix = bg * float(self.settings.get("bg_volume", 0.25)) + self._dub(segs, len(bg), dur, work) * 1.4
        P.write_wav(work / "mix.wav", mix)
        P.ffmpeg("-i", str(work / "v.ts"), "-i", str(work / "mix.wav"), "-map", "0:v:0", "-map", "1:a:0",
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-muxdelay", "0", "-muxpreload", "0",
                 "-output_ts_offset", f"{t0:.3f}", "-f", "mpegts", str(self.dir / f"out_{seq:06d}.ts"))
        self.out.append((seq, dur))
        self.t += dur
        self.lag = round(time.time() - cut_at, 1)
        self._write_playlist()

    def _transcribe(self, audio):
        model = P.load_whisper(MODEL, _quiet)
        segments, info = model.transcribe(audio, language=self.lang, vad_filter=True, beam_size=5,
                                          initial_prompt="以下是普通话的句子。" if self.lang == "zh" else None)
        segs = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments if s.text.strip()]
        # đoán ngôn ngữ ở đoạn đầu có lời nói rõ ràng rồi giữ nguyên, tránh nhảy ngôn ngữ giữa chừng
        if self.lang is None and segs and info.language_probability >= 0.6:
            self.lang = info.language
        return segs, info.language

    def _dub(self, segs, n, dur, work):
        """Giọng đọc dài đúng n mẫu (câu đọc tràn quá cuối đoạn bị cắt); lỗi giọng đọc thì để im lặng."""
        track = np.zeros(n, dtype=np.float32)
        if not segs:
            return track
        try:
            voice = P.TARGETS[self.settings["target"]]["voices"][self.settings.get("voice", "female")]
            dub = P.read_wav(P.synthesize(segs, voice, dur, work, _quiet, lambda p: None))[:n]
            track[:len(dub)] = dub
        except Exception as e:
            _quiet(f"voice-over failed, using silence: {e}")
        return track

    # ---------- playlist HLS ----------
    def _write_playlist(self, final=False):
        items = self.out[-WINDOW:]
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{TARGET_DURATION}",
                 f"#EXT-X-MEDIA-SEQUENCE:{items[0][0] if items else 0}"]
        start = self.t - sum(d for _, d in items)
        for seq, d in items:
            when = datetime.fromtimestamp(self.epoch0 + start, timezone.utc)
            lines += [f"#EXT-X-PROGRAM-DATE-TIME:{when.isoformat(timespec='milliseconds').replace('+00:00', 'Z')}",
                      f"#EXTINF:{d:.3f},", f"out_{seq:06d}.ts"]
            start += d
        if final:
            lines.append("#EXT-X-ENDLIST")
        tmp = self.dir / "live.m3u8.tmp"
        tmp.write_text("\n".join(lines) + "\n")
        tmp.replace(self.dir / "live.m3u8")

    def _trim(self):
        keep = {f"out_{seq:06d}.ts" for seq, _ in self.out[-WINDOW:]}
        for f in self.dir.glob("out_*.ts"):
            if f.name not in keep:
                f.unlink(missing_ok=True)

    # ---------- sau khi live kết thúc ----------
    def _wait_vod_and_queue(self):
        """YouTube cần một lúc để xử lý bản ghi; khi xem lại được thì đưa vào hàng đợi lồng tiếng."""
        self.vod = "waiting"
        deadline = time.time() + VOD_WAIT
        while time.time() < deadline and not self.stop_flag.is_set():
            try:
                with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, **P.cookie_opts(self.settings.get("cookies"))}) as y:
                    info = y.extract_info(self.url, download=False)
                if info.get("live_status") == "was_live" and info.get("duration"):
                    self.on_ended(self)
                    self.vod = "queued"
                    return
            except Exception as e:
                _quiet(f"recording not ready yet: {str(e)[:120]}")
            time.sleep(300)
        self.vod = "gave_up"
