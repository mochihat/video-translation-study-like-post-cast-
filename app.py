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
from flask import Flask, abort, jsonify, redirect, request, send_from_directory

import remote
import storage
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
        job.setdefault("video_at", "local")
        if job["status"] != "done":
            # bản cũ để file tạm (video gốc, âm thanh...) ngay trong jobs/<id>; giờ file tạm nằm ở tmp/
            for p in f.parent.iterdir():
                if p.name != "job.json":
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
    shutil.rmtree(BASE / "tmp", ignore_errors=True)  # file tạm của lần chạy trước
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

        def report(**kw):
            # đang tháo ổ an toàn -> dừng ghi ngay, video này sẽ làm lại khi cắm ổ
            if current.get("loc") in storage.held:
                raise RuntimeError("Dừng để tháo ổ an toàn")
            update(**kw)

        job["status"] = "running"
        job.pop("error", None)
        attempt = 0
        waits = iter([5, 10, 20] + [30] * 12)  # phút chờ khi YouTube chặn (tổng ~7 giờ)
        while True:
            loc = wait_for_storage(job)
            if job["status"] == "deleted":
                break
            try:
                current["loc"] = loc
                workdir = fresh_workdir(loc, job_id)
                run_pipeline(job, workdir, report)
                # nếu vừa đổi nơi lưu trong lúc xử lý thì lưu thẳng vào nơi mới
                new_loc = storage.current()
                finalize(job, workdir, new_loc if new_loc != loc and storage.check(new_loc)[0] else loc)
                job["status"] = "done"
                break
            except Exception as e:
                current["loc"] = None
                if loc in storage.held:
                    update(log="Tạm dừng để tháo ổ an toàn — video này sẽ làm lại khi cắm ổ vào")
                    continue
                traceback.print_exc()
                if not storage.check(loc)[0]:
                    # ổ bị rút ra / đầy giữa chừng: chờ ổ sẵn sàng rồi làm lại video này
                    update(log=f"{storage.check(loc)[1]} — tạm dừng, sẽ làm lại video này khi ổ sẵn sàng")
                    continue
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
        current["loc"] = None
        save(job)
        if not work_queue.empty():
            time.sleep(random.uniform(20, 40))  # nghỉ giữa các video để YouTube không chặn


def wait_for_storage(job):
    """Chờ tới khi nơi lưu video đang chọn dùng được (đã cắm, ghi được, còn chỗ)."""
    current["loc"] = None
    while True:
        storage.release_unplugged()
        loc = storage.current()
        ok, reason = storage.check(loc)
        if ok or job["status"] == "deleted":
            current["paused_until"] = None
            return loc
        wait = 5 if loc in storage.held else 30
        current["paused_until"] = time.time() + wait
        current["pause_reason"] = f"{storage.describe(loc)}: {reason}"
        time.sleep(wait)


def fresh_workdir(loc, job_id):
    """Thư mục tạm trống cho video. Thư mục cũ không xoá được (vd. hỏng do ổ bị rút giữa chừng)
    thì để nguyên và dùng thư mục mới, thay vì đánh lỗi cả video."""
    workdir = storage.tmp_dir(loc, job_id)
    try:
        if workdir.exists():
            shutil.rmtree(workdir)
    except OSError as e:
        print(f"  Không dọn được {workdir} ({e}), dùng thư mục tạm mới", flush=True)
        workdir = workdir.with_name(f"{job_id}-{uuid.uuid4().hex[:4]}")
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def finalize(job, workdir, loc):
    """Video thành phẩm để lại trên nơi lưu; phụ đề, lời thoại, ảnh bìa đưa về laptop."""
    meta = JOBS_DIR / job["id"]
    for name in ("vi.srt", "original.srt", "segments.json", "cover.jpg"):
        if (workdir / name).exists():
            (meta / name).unlink(missing_ok=True)
            shutil.move(str(workdir / name), meta / name)
    dest = storage.final_dir(loc, job["id"])
    dest.mkdir(parents=True, exist_ok=True)
    shutil.move(str(workdir / "dubbed.mp4"), dest / "dubbed.mp4.part")
    (dest / "dubbed.mp4.part").replace(dest / "dubbed.mp4")
    if workdir != dest:
        shutil.rmtree(workdir, ignore_errors=True)
    job["video_at"] = loc
    storage.remember_label(loc)


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


LOGIN_PAGE = """<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Nhập mã PIN</title>
<style>
 :root{--bg:#f5f6f8;--card:#fff;--text:#1d2330;--muted:#6b7385;--line:#e3e6ec;--accent:#d93025}
 @media (prefers-color-scheme: dark){:root{--bg:#14161b;--card:#1d2027;--text:#e8eaef;--muted:#9aa1b0;--line:#2d313b;--accent:#ff6b5e}}
 body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px;box-sizing:border-box}
 form{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px;width:100%;max-width:340px;text-align:center}
 input{width:100%;box-sizing:border-box;font:inherit;font-size:28px;letter-spacing:8px;text-align:center;padding:10px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--text)}
 button{margin-top:14px;width:100%;background:var(--accent);color:#fff;border:0;border-radius:10px;padding:12px;font:inherit;font-weight:600}
 p{color:var(--muted);font-size:14px} .err{color:var(--accent)}
</style></head><body><form method="post" action="/login">
<h2 style="margin-top:0">🎬 Lồng tiếng Việt</h2>
<p>Nhập mã PIN hiện trên laptop<br>(💾 Nơi lưu video → 📱 Xem trên điện thoại)</p>
<input name="pin" inputmode="numeric" autocomplete="one-time-code" maxlength="6" autofocus required>
%ERROR%<button>Vào xem</button></form></body></html>"""


@app.before_request
def guard():
    """Laptop vào thẳng; máy khác phải bật tính năng, ở mạng nội bộ và nhập đúng PIN."""
    addr = request.remote_addr or ""
    if remote.is_local(addr):
        return None
    if not remote.settings()["enabled"] or not remote.is_private(addr):
        return ("Tính năng xem từ điện thoại / máy khác đang tắt. "
                "Bật trong trang 💾 Nơi lưu video trên laptop."), 403
    if request.cookies.get(remote.COOKIE) == remote.token():
        return None
    pin = request.form.get("pin") if request.method == "POST" and request.path == "/login" else request.args.get("pin")
    if pin is not None:
        if remote.check_pin(addr, pin):
            resp = redirect("/")
            resp.set_cookie(remote.COOKIE, remote.token(), max_age=365 * 86400, httponly=True, samesite="Lax")
            return resp
        msg = ("Nhập sai quá nhiều lần — thử lại sau 15 phút." if remote.locked(addr) else "Mã PIN không đúng.")
        return LOGIN_PAGE.replace("%ERROR%", f'<p class="err">{msg}</p>'), 401
    if request.path.startswith(("/api/", "/files/")):
        return jsonify(error="Cần nhập mã PIN"), 401
    return LOGIN_PAGE.replace("%ERROR%", ""), 401


@app.get("/api/remote")
def get_remote():
    r = remote.settings()
    urls = [f"http://{ip}:{remote.PORT}" for ip in remote.lan_ips()]
    return jsonify(enabled=r["enabled"], pin=r["pin"], wifi=remote.wifi_name(),
                   urls=[{"url": u, "qr": remote.qr_svg(f"{u}/?pin={r['pin']}")} for u in urls])


@app.post("/api/remote")
def set_remote():
    data = request.get_json(force=True)
    if "enabled" in data:
        remote.set_enabled(data["enabled"])
    if data.get("new_pin"):
        remote.new_pin()
    return get_remote()


@app.get("/")
def index():
    resp = send_from_directory(BASE / "static", "index.html")
    resp.headers["Cache-Control"] = "no-cache"  # luôn lấy giao diện mới nhất sau khi cập nhật
    return resp


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


def used_drives():
    """Các ổ ngoài web đang dùng: nơi lưu hiện tại và các ổ đang chứa video."""
    locs = {storage.current()} | {j.get("video_at", "local") for j in jobs.values()}
    locs.discard("local")
    return locs


def ejectable():
    """Ổ ngoài web đang dùng mà còn đang mở -> cần 'Tháo ổ an toàn' trước khi rút."""
    out = []
    for loc in used_drives():
        part = storage.find(loc)
        if loc not in storage.held and part and part["mountpoint"]:
            out.append({"id": loc, "name": storage.describe(loc)})
    return out


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
        jobs=[with_video_state(j) for j in sorted(jobs.values(), key=lambda j: j["created"], reverse=True)],
        storage=storage.describe(storage.current()),
        ejectable=ejectable(),
        ejecting=[{"id": u, "name": storage.describe(u)} for u in storage.held],
        playlists=pls,
        current=current["id"],
        paused_until=current["paused_until"],
        pause_reason=current.get("pause_reason"),
        queued=sum(j["status"] == "queued" for j in jobs.values()),
    )


@app.get("/api/jobs/<job_id>")
def get_job(job_id):
    job = jobs.get(job_id) or abort(404)
    out = {**job, **with_video_state(job)}
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
    if job.get("video_at", "local") != "local":
        try:  # ổ ngoài chưa cắm thì không xoá được file video trên đó
            shutil.rmtree(storage.final_dir(job["video_at"], job_id), ignore_errors=True)
        except RuntimeError:
            pass
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
    folder = JOBS_DIR / job_id
    if name == "dubbed.mp4":
        path = storage.video_path(jobs[job_id])
        if not path:
            return jsonify(error=f"Video nằm trên {storage.describe(jobs[job_id]['video_at'])} — hãy cắm ổ vào"), 404
        folder = path.parent
    return send_from_directory(folder, name, as_attachment=request.args.get("dl") == "1")


def with_video_state(job):
    out = light(job)
    if job["status"] == "done":
        out["video_ok"] = storage.video_path(job) is not None
        out["local_copy"] = job.get("video_at", "local") != "local" and (JOBS_DIR / job["id"] / "dubbed.mp4").exists()
        out["video_place"] = storage.describe(job.get("video_at", "local"))
    return out


@app.post("/api/jobs/<job_id>/keep-local")
def keep_local(job_id):
    """Chép / bỏ bản video trên laptop để xem được cả khi không cắm ổ ngoài."""
    job = jobs.get(job_id) or abort(404)
    local = JOBS_DIR / job_id / "dubbed.mp4"
    if job.get("video_at", "local") == "local":
        return jsonify(error="Video này vốn đã nằm trên laptop"), 400
    if request.get_json(force=True).get("keep"):
        try:
            src = storage.root(job["video_at"]) / job_id / "dubbed.mp4"
        except RuntimeError as e:
            return jsonify(error=str(e)), 400
        if shutil.disk_usage(JOBS_DIR).free - src.stat().st_size < storage.MIN_FREE:
            return jsonify(error="Laptop không đủ chỗ trống để giữ video này"), 400
        shutil.copy2(src, local.with_suffix(".part"))
        local.with_suffix(".part").replace(local)
    else:
        local.unlink(missing_ok=True)
    return jsonify(with_video_state(job))


move_state = {"running": False, "total": 0, "done": 0, "skipped": 0, "title": None, "error": None}


def move_videos(target):
    """Chuyển các video đã xong sang nơi lưu mới (chép xong, kiểm tra kích thước rồi mới xoá bản cũ)."""
    todo = [j for j in jobs.values() if j["status"] == "done" and j.get("video_at", "local") != target]
    move_state.update(running=True, total=len(todo), done=0, skipped=0, title=None, error=None)
    try:
        for job in todo:
            move_state["title"] = job["title"]
            old_loc = job.get("video_at", "local")
            try:
                src = storage.root(old_loc) / job["id"] / "dubbed.mp4"
            except RuntimeError:
                src = None
            if not src or not src.exists():
                move_state["skipped"] += 1  # ổ cũ chưa cắm: để nguyên
                continue
            dest = storage.final_dir(target, job["id"])
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / "dubbed.mp4.part")
            if (dest / "dubbed.mp4.part").stat().st_size != src.stat().st_size:
                raise RuntimeError(f"Chép không trọn vẹn: {job['title']}")
            (dest / "dubbed.mp4.part").replace(dest / "dubbed.mp4")
            job["video_at"] = target
            save(job)
            if old_loc != "local":
                shutil.rmtree(src.parent, ignore_errors=True)
            elif target != "local":
                src.unlink()
            move_state["done"] += 1
    except Exception as e:
        traceback.print_exc()
        move_state["error"] = str(e)
    move_state.update(running=False, title=None)


@app.post("/api/storage/eject")
def eject_storage():
    """Tháo ổ an toàn: dừng ghi (video đang làm sẽ làm lại sau), unmount, tắt nguồn ổ."""
    loc = (request.get_json(silent=True) or {}).get("location") or storage.current()
    if loc == "local" or loc not in used_drives():
        return jsonify(error="Không có ổ ngoài nào để tháo"), 400
    name = storage.describe(loc)
    if move_state["running"]:
        return jsonify(error="Đang chuyển video sang ổ — chờ chuyển xong rồi hãy tháo"), 400
    storage.held.add(loc)
    deadline = time.time() + 180
    while current.get("loc") == loc and time.time() < deadline:  # chờ worker dừng ghi
        time.sleep(1)
    if current.get("loc") == loc:
        storage.held.discard(loc)
        return jsonify(error="Video đang xử lý chưa dừng ghi được (đang ghép video) — thử lại sau 1–2 phút"), 400
    try:
        storage.eject(loc)
    except RuntimeError as e:
        storage.held.discard(loc)
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, message=f"Đã tháo {name} an toàn — giờ có thể rút ổ ra.")


@app.post("/api/storage/resume")
def resume_storage():
    loc = (request.get_json(silent=True) or {}).get("location")
    if loc:
        storage.held.discard(loc)
    else:
        storage.held.clear()
    return jsonify(ok=True)


@app.get("/api/storage")
def get_storage():
    return jsonify(current=storage.current(), options=storage.options(), moving=move_state,
                   pending_move=sum(j["status"] == "done" and j.get("video_at", "local") != storage.current()
                                    for j in jobs.values()))


@app.post("/api/storage")
def set_storage():
    data = request.get_json(force=True)
    loc = data.get("location") or "local"
    ok, reason = storage.check(loc)
    if not ok:
        return jsonify(error=reason), 400
    storage.set_current(loc)
    storage.remember_label(loc)
    if data.get("move") and not move_state["running"]:
        threading.Thread(target=move_videos, args=(loc,), daemon=True).start()
    return get_storage()


if __name__ == "__main__":
    load_existing()
    threading.Thread(target=worker, daemon=True).start()
    print("\n  Mở trình duyệt tại: http://localhost:5000\n", flush=True)
    # nghe trên mọi địa chỉ để điện thoại cùng Wi-Fi vào được; guard() chặn khi chưa bật / sai PIN
    app.run(host="0.0.0.0", port=remote.PORT, threaded=True)
