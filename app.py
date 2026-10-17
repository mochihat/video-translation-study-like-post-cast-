import json
import queue
import random
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp
from flask import Flask, abort, jsonify, request, send_from_directory

from pipeline import STEPS, cookie_opts, run_pipeline, transient_reason

BASE = Path(__file__).parent
JOBS_DIR = BASE / "jobs"
PLAYLISTS_DIR = BASE / "playlists"
JOBS_DIR.mkdir(exist_ok=True)
PLAYLISTS_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="/static")
jobs = {}
playlists = {}
lock = threading.Lock()
work_queue = queue.Queue()
current = {"id": None, "paused_until": None}


def new_id():
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def save(job):
    (JOBS_DIR / job["id"] / "job.json").write_text(json.dumps(job, ensure_ascii=False), "utf-8")


def save_playlist(pl):
    (PLAYLISTS_DIR / f"{pl['id']}.json").write_text(json.dumps(pl, ensure_ascii=False), "utf-8")


def load_existing():
    for f in JOBS_DIR.glob("*/job.json"):
        job = json.loads(f.read_text("utf-8"))
        jobs[job["id"]] = job
    for f in PLAYLISTS_DIR.glob("*.json"):
        pl = json.loads(f.read_text("utf-8"))
        playlists[pl["id"]] = pl
    # làm tiếp các video còn dở khi máy chủ bị tắt
    pending = sorted((j for j in jobs.values() if j["status"] in ("queued", "running")), key=lambda j: j["created"])
    for job in pending:
        job.update(status="queued", step=None, progress=0)
        save(job)
        work_queue.put(job["id"])
    if pending:
        print(f"  Tiếp tục {len(pending)} video còn dở")


class KeepAwake:
    """Giữ máy không ngủ (kể cả khi gập nắp) trong lúc còn video cần xử lý."""
    proc = None

    def on(self):
        if self.proc or not shutil.which("systemd-inhibit"):
            return
        for what in ("sleep:idle:handle-lid-switch", "sleep:idle"):
            p = subprocess.Popen(
                ["systemd-inhibit", f"--what={what}", "--who=Lồng tiếng YouTube",
                 "--why=Đang lồng tiếng video", "sleep", "infinity"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)
            if p.poll() is None:
                self.proc = p
                return

    def off(self):
        if self.proc:
            self.proc.terminate()
            self.proc = None


awake = KeepAwake()


def worker():
    while True:
        if work_queue.empty():
            awake.off()
        job_id = work_queue.get()
        job = jobs.get(job_id)
        if not job or job["status"] != "queued":  # đã bị xoá/huỷ
            continue
        awake.on()
        current["id"] = job_id
        workdir = JOBS_DIR / job_id

        def update(step=None, progress=None, log=None, **fields):
            with lock:
                if step:
                    job["step"] = step
                if progress is not None:
                    job["progress"] = round(progress, 3)
                job.update({k: v for k, v in fields.items() if v is not None})
                if log:
                    job["logs"].append(f"[{time.strftime('%H:%M:%S')}] {log}")
                save(job)

        job["status"] = "running"
        job.pop("error", None)
        attempt = 0
        waits = iter([5, 10, 20] + [30] * 12)  # phút chờ khi YouTube chặn (tổng ~7 giờ)
        while True:
            try:
                run_pipeline(job, workdir, update)
                job["status"] = "done"
                break
            except Exception as e:
                traceback.print_exc()
                reason = transient_reason(e)
                if reason:
                    # lỗi tạm thời: dừng cả hàng đợi rồi thử lại, không đánh lỗi video
                    wait = next(waits, None)
                    if wait is not None:
                        update(log=f"{reason}, chờ {wait} phút rồi thử lại")
                        current["paused_until"] = time.time() + wait * 60
                        current["pause_reason"] = reason
                        time.sleep(wait * 60)
                        current["paused_until"] = None
                        continue
                elif attempt == 0:
                    attempt += 1
                    update(log=f"Lỗi: {e} — thử lại sau 15 giây")
                    time.sleep(15)
                    continue
                job["status"] = "error"
                job["error"] = str(e)
                break
        job["finished"] = time.time()
        current["id"] = None
        save(job)
        if not work_queue.empty():
            time.sleep(random.uniform(20, 40))  # nghỉ giữa các video để YouTube không chặn


def video_id_from_url(url):
    u = urlparse(url)
    if u.hostname and u.hostname.endswith("youtu.be"):
        return u.path.strip("/") or None
    return parse_qs(u.query).get("v", [None])[0]


def find_existing(video_id, settings):
    """Video đã (hoặc đang) được lồng tiếng với cùng giọng đọc thì dùng lại."""
    if not video_id:
        return None
    for j in jobs.values():
        if j.get("video_id") == video_id and j["voice"] == settings["voice"] and j["status"] != "error":
            return j
    return None


def new_job(url, settings, title=None, video_id=None, playlist_id=None):
    job = {
        "id": new_id(), "url": url, **settings,
        "status": "queued", "step": None, "progress": 0,
        "title": title or url, "video_id": video_id, "playlist_id": playlist_id,
        "logs": [], "created": time.time(),
    }
    (JOBS_DIR / job["id"]).mkdir()
    jobs[job["id"]] = job
    save(job)
    work_queue.put(job["id"])
    return job


def expand_playlist(url, browser):
    opts = {"extract_flat": "in_playlist", "quiet": True, "no_warnings": True, **cookie_opts(browser)}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = [
        e for e in (info.get("entries") or [])
        if e and e.get("id") and e.get("title") not in ("[Private video]", "[Deleted video]")
    ]
    return info.get("title") or "Playlist", entries


def light(job):
    return {k: v for k, v in job.items() if k != "logs"}


@app.get("/")
def index():
    return send_from_directory(BASE / "static", "index.html")


@app.get("/api/steps")
def steps():
    return jsonify([{"id": s, "name": n} for s, n in STEPS])


@app.post("/api/jobs")
def create_job():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url.startswith("http"):
        return jsonify(error="Link YouTube không hợp lệ"), 400
    settings = {
        "language": data.get("language", "auto"),
        "voice": data.get("voice", "female"),
        "model": data.get("model", "large-v3-turbo"),
        "translator": data.get("translator", "offline"),
        "bg_volume": float(data.get("bg_volume", 0.25)),
        "cookies": data.get("cookies", "none"),
    }

    if data.get("playlist") and "list=" in url:
        try:
            title, entries = expand_playlist(url, settings["cookies"])
        except Exception as e:
            return jsonify(error=f"Không đọc được playlist: {e}"), 400
        if not entries:
            return jsonify(error="Playlist không có video nào xem được"), 400
        pl = {"id": new_id(), "title": title, "url": url, "job_ids": [], "created": time.time()}
        reused = 0
        for e in entries:
            existing = find_existing(e["id"], settings)
            if existing:
                reused += 1
            job = existing or new_job(f"https://www.youtube.com/watch?v={e['id']}", settings,
                                      e.get("title"), e["id"], pl["id"])
            pl["job_ids"].append(job["id"])
        playlists[pl["id"]] = pl
        save_playlist(pl)
        return jsonify(playlist=pl, count=len(entries), reused=reused)

    existing = None if data.get("force") else find_existing(video_id_from_url(url), settings)
    if existing:
        return jsonify(job=light(existing), existing=True)
    return jsonify(job=light(new_job(url, settings, video_id=video_id_from_url(url))))


@app.get("/api/library")
def library():
    pls = []
    for pl in sorted(playlists.values(), key=lambda p: p["created"], reverse=True):
        items = [jobs[i] for i in pl["job_ids"] if i in jobs]
        pls.append({**pl, "total": len(items),
                    "done": sum(j["status"] == "done" for j in items),
                    "failed": sum(j["status"] == "error" for j in items)})
    return jsonify(
        jobs=[light(j) for j in sorted(jobs.values(), key=lambda j: j["created"], reverse=True)],
        playlists=pls,
        current=current["id"],
        paused_until=current["paused_until"],
        pause_reason=current.get("pause_reason"),
        queued=sum(j["status"] == "queued" for j in jobs.values()),
    )


@app.get("/api/jobs/<job_id>")
def get_job(job_id):
    job = jobs.get(job_id) or abort(404)
    out = dict(job)
    seg_file = JOBS_DIR / job_id / "segments.json"
    if job["status"] == "done" and seg_file.exists():
        out["segments"] = json.loads(seg_file.read_text("utf-8"))
    return jsonify(out)


@app.post("/api/jobs/<job_id>/retry")
def retry_job(job_id):
    job = jobs.get(job_id) or abort(404)
    if job["status"] != "error":
        return jsonify(error="Chỉ thử lại được video bị lỗi"), 400
    job.update(status="queued", step=None, progress=0, logs=[])
    job.pop("error", None)
    save(job)
    work_queue.put(job_id)
    return jsonify(light(job))


def remove_job(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] == "running":
        return False
    job["status"] = "deleted"  # để worker bỏ qua nếu còn trong hàng đợi
    shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
    jobs.pop(job_id)
    return True


@app.delete("/api/jobs/<job_id>")
def delete_job(job_id):
    if job_id not in jobs:
        abort(404)
    if not remove_job(job_id):
        return jsonify(error="Không thể xoá khi đang xử lý"), 400
    return jsonify(ok=True)


@app.delete("/api/playlists/<pl_id>")
def delete_playlist(pl_id):
    pl = playlists.pop(pl_id, None) or abort(404)
    (PLAYLISTS_DIR / f"{pl_id}.json").unlink(missing_ok=True)
    # chỉ xoá video không thuộc playlist nào khác
    others = {i for p in playlists.values() for i in p["job_ids"]}
    for jid in pl["job_ids"]:
        if jid not in others:
            remove_job(jid)
    return jsonify(ok=True)


@app.get("/files/<job_id>/<name>")
def files(job_id, name):
    if name not in ("dubbed.mp4", "vi.srt", "original.srt", "cover.jpg") or job_id not in jobs:
        abort(404)
    return send_from_directory(JOBS_DIR / job_id, name, as_attachment=request.args.get("dl") == "1")


if __name__ == "__main__":
    load_existing()
    threading.Thread(target=worker, daemon=True).start()
    print("\n  Mở trình duyệt tại: http://localhost:5000\n", flush=True)
    app.run(host="127.0.0.1", port=5000, threaded=True)
