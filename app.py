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
        if "entries" not in pl:
            migrate_playlist(pl)
    # video dán lẻ nhưng thuộc một playlist (vị trí trống hoặc bản lỗi) -> đưa về đúng chỗ
    in_playlist = {e["job_id"] for pl in playlists.values() for e in pl["entries"]}
    for job in sorted(jobs.values(), key=lambda j: j["created"]):
        if job["id"] not in in_playlist:
            attach_to_playlists(job)
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


def list_id_from_url(url):
    return parse_qs(urlparse(url).query).get("list", [None])[0]


SETTING_DEFAULTS = {"language": "auto", "voice": "female", "model": "large-v3-turbo",
                    "translator": "offline", "bg_volume": 0.25, "cookies": "none"}


def settings_of(source):
    s = {k: source.get(k, v) for k, v in SETTING_DEFAULTS.items()}
    s["bg_volume"] = float(s["bg_volume"])
    return s


def find_existing(video_id, settings):
    """Video đã (hoặc đang) được lồng tiếng với cùng giọng đọc thì dùng lại."""
    if not video_id:
        return None
    for j in jobs.values():
        if j.get("video_id") == video_id and j["voice"] == settings["voice"] and j["status"] != "error":
            return j
    return None


def find_failed(video_id):
    if not video_id:
        return None
    return next((j for j in jobs.values() if j.get("video_id") == video_id and j["status"] == "error"), None)


def requeue(job, settings=None):
    if settings:
        job.update(settings)
    job.update(status="queued", step=None, progress=0, logs=[])
    job.pop("error", None)
    save(job)
    work_queue.put(job["id"])


def migrate_playlist(pl):
    """Bản cũ chỉ lưu job_ids nên video bị xoá mất luôn chỗ; chuyển sang danh sách vị trí."""
    pl["entries"] = []
    for jid in pl.pop("job_ids", []):
        j = jobs.get(jid)
        pl["entries"].append({"video_id": j and j.get("video_id"), "title": j and j["title"],
                              "job_id": jid if j else None})
    first = next((jobs[e["job_id"]] for e in pl["entries"] if e["job_id"]), {})
    pl.setdefault("settings", settings_of(first))
    save_playlist(pl)


def attach_to_playlists(job):
    """Đưa video vào đúng vị trí của nó trong playlist nếu vị trí đó đang trống (đã xoá) hoặc bị lỗi."""
    vid = job.get("video_id")
    if not vid:
        return
    for pl in list(playlists.values()):
        for e in pl["entries"]:
            if e["video_id"] != vid or e["job_id"] == job["id"]:
                continue
            old = jobs.get(e["job_id"])
            if old and old["status"] != "error":
                continue
            if old:
                remove_job(old["id"])  # bản lỗi được thay bằng bản mới
            e["job_id"] = job["id"]
            job["playlist_id"] = job.get("playlist_id") or pl["id"]
            save_playlist(pl)
            save(job)


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
    settings = settings_of(data)

    if data.get("playlist") and "list=" in url:
        return sync_playlist(url, settings)

    vid = video_id_from_url(url)
    if not data.get("force"):
        existing = find_existing(vid, settings)
        if existing:
            attach_to_playlists(existing)
            return jsonify(job=light(existing), existing=True)
        failed = find_failed(vid)
        if failed:  # thử lại bản lỗi ngay tại vị trí cũ của nó (kể cả trong playlist)
            requeue(failed, settings)
            return jsonify(job=light(failed))
    job = new_job(url, settings, video_id=vid)
    attach_to_playlists(job)
    return jsonify(job=light(job))


def sync_playlist(url, settings):
    """Tạo playlist mới, hoặc cập nhật playlist đã có (cùng list=): thêm video mới,
    lồng tiếng lại video đã xoá và thử lại video lỗi, giữ nguyên video đã xong."""
    try:
        title, found = expand_playlist(url, settings["cookies"])
    except Exception as e:
        return jsonify(error=f"Không đọc được playlist: {e}"), 400
    if not found:
        return jsonify(error="Playlist không có video nào xem được"), 400
    lid = list_id_from_url(url)
    pl = next((p for p in playlists.values() if lid and list_id_from_url(p["url"]) == lid), None)
    is_new = pl is None
    if is_new:
        pl = {"id": new_id(), "url": url, "entries": [], "settings": settings, "created": time.time()}
        playlists[pl["id"]] = pl
    pl["title"] = title
    old = {e["video_id"]: e for e in pl["entries"] if e["video_id"]}
    entries, added = [], 0
    for f in found:
        slot = old.pop(f["id"], None) or {"video_id": f["id"], "job_id": None}
        slot["title"] = f.get("title")
        job = jobs.get(slot["job_id"])
        if job and job["status"] == "error":
            requeue(job)
            added += 1
        elif not job:
            job = find_existing(f["id"], settings)
            if not job:
                job = new_job(f"https://www.youtube.com/watch?v={f['id']}", settings, f.get("title"), f["id"], pl["id"])
                added += 1
            slot["job_id"] = job["id"]
        entries.append(slot)
    # video đã bị gỡ khỏi playlist trên YouTube nhưng đã lồng tiếng thì vẫn giữ
    entries += [e for e in old.values() if e["job_id"] in jobs]
    pl["entries"] = entries
    save_playlist(pl)
    return jsonify(playlist=pl, count=len(found), added=added, updated=not is_new)


@app.post("/api/playlists/<pl_id>/sync")
def sync_existing_playlist(pl_id):
    pl = playlists.get(pl_id) or abort(404)
    return sync_playlist(pl["url"], settings_of(pl.get("settings", {})))


@app.post("/api/playlists/<pl_id>/slots/<int:idx>")
def redo_slot(pl_id, idx):
    """Lồng tiếng lại một video đã bị xoá khỏi playlist, đặt vào đúng vị trí cũ."""
    pl = playlists.get(pl_id) or abort(404)
    if not 0 <= idx < len(pl["entries"]):
        abort(404)
    e = pl["entries"][idx]
    if e["job_id"] in jobs:
        return jsonify(error="Vị trí này đã có video"), 400
    if not e["video_id"]:
        return jsonify(error="Không rõ video ở vị trí này — hãy bấm 'Cập nhật từ YouTube'"), 400
    settings = settings_of(pl.get("settings", {}))
    job = find_existing(e["video_id"], settings) or new_job(
        f"https://www.youtube.com/watch?v={e['video_id']}", settings, e["title"], e["video_id"], pl_id)
    e["job_id"] = job["id"]
    save_playlist(pl)
    return jsonify(job=light(job))


@app.post("/api/playlists/<pl_id>/retry-failed")
def retry_failed(pl_id):
    pl = playlists.get(pl_id) or abort(404)
    failed = [jobs[e["job_id"]] for e in pl["entries"] if e["job_id"] in jobs and jobs[e["job_id"]]["status"] == "error"]
    for job in failed:
        requeue(job)
    return jsonify(count=len(failed))


@app.get("/api/library")
def library():
    pls = []
    for pl in sorted(playlists.values(), key=lambda p: p["created"], reverse=True):
        items = [jobs[e["job_id"]] for e in pl["entries"] if e["job_id"] in jobs]
        pls.append({**pl, "job_ids": [j["id"] for j in items], "total": len(pl["entries"]),
                    "done": sum(j["status"] == "done" for j in items),
                    "failed": sum(j["status"] == "error" for j in items),
                    "missing": len(pl["entries"]) - len(items)})
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
    requeue(job)
    return jsonify(light(job))


def remove_job(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] == "running":
        return False
    job["status"] = "deleted"  # để worker bỏ qua nếu còn trong hàng đợi
    shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
    jobs.pop(job_id)
    # giữ lại vị trí trong playlist để sau này lồng tiếng lại đúng chỗ
    for pl in playlists.values():
        for e in pl["entries"]:
            if e["job_id"] == job_id:
                e["job_id"] = None
                save_playlist(pl)
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
    others = {e["job_id"] for p in playlists.values() for e in p["entries"]}
    for e in pl["entries"]:
        if e["job_id"] and e["job_id"] not in others:
            remove_job(e["job_id"])
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
