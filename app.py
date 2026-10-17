import json
import queue
import threading
import time
import traceback
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

from pipeline import STEPS, run_pipeline

BASE = Path(__file__).parent
JOBS_DIR = BASE / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="/static")
jobs = {}
lock = threading.Lock()
work_queue = queue.Queue()


def save(job):
    (JOBS_DIR / job["id"] / "job.json").write_text(json.dumps(job, ensure_ascii=False), "utf-8")


def load_existing():
    for f in JOBS_DIR.glob("*/job.json"):
        job = json.loads(f.read_text("utf-8"))
        if job["status"] in ("queued", "running"):
            job["status"] = "error"
            job["error"] = "Bị gián đoạn do máy chủ khởi động lại"
        jobs[job["id"]] = job


def worker():
    while True:
        job_id = work_queue.get()
        job = jobs[job_id]
        workdir = JOBS_DIR / job_id

        def update(step=None, progress=None, log=None, title=None):
            with lock:
                if step:
                    job["step"] = step
                if progress is not None:
                    job["progress"] = round(progress, 3)
                if title:
                    job["title"] = title
                if log:
                    job["logs"].append(f"[{time.strftime('%H:%M:%S')}] {log}")
                save(job)

        job["status"] = "running"
        try:
            run_pipeline(job, workdir, update)
            job["status"] = "done"
        except Exception as e:
            traceback.print_exc()
            job["status"] = "error"
            job["error"] = str(e)
        job["finished"] = time.time()
        save(job)


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
    job = {
        "id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4],
        "url": url,
        "language": data.get("language", "auto"),
        "voice": data.get("voice", "female"),
        "model": data.get("model", "large-v3-turbo"),
        "translator": data.get("translator", "offline"),
        "bg_volume": float(data.get("bg_volume", 0.25)),
        "status": "queued",
        "step": None,
        "progress": 0,
        "title": url,
        "logs": [],
        "created": time.time(),
    }
    (JOBS_DIR / job["id"]).mkdir()
    jobs[job["id"]] = job
    save(job)
    work_queue.put(job["id"])
    return jsonify(job)


@app.get("/api/jobs")
def list_jobs():
    return jsonify(sorted(jobs.values(), key=lambda j: j["created"], reverse=True))


@app.get("/api/jobs/<job_id>")
def get_job(job_id):
    job = jobs.get(job_id) or abort(404)
    out = dict(job)
    seg_file = JOBS_DIR / job_id / "segments.json"
    if seg_file.exists():
        out["segments"] = json.loads(seg_file.read_text("utf-8"))
    return jsonify(out)


@app.delete("/api/jobs/<job_id>")
def delete_job(job_id):
    import shutil
    job = jobs.get(job_id) or abort(404)
    if job["status"] in ("queued", "running"):
        return jsonify(error="Không thể xoá khi đang xử lý"), 400
    shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
    jobs.pop(job_id)
    return jsonify(ok=True)


@app.get("/files/<job_id>/<name>")
def files(job_id, name):
    if name not in ("dubbed.mp4", "vi.srt", "original.srt"):
        abort(404)
    return send_from_directory(JOBS_DIR / job_id, name, as_attachment=request.args.get("dl") == "1")


if __name__ == "__main__":
    load_existing()
    threading.Thread(target=worker, daemon=True).start()
    print("\n  Mở trình duyệt tại: http://localhost:5000\n")
    app.run(host="127.0.0.1", port=5000, threaded=True)
