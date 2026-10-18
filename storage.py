"""Nơi lưu video: ngay trên laptop, hoặc một phân vùng / ổ cứng gắn ngoài (nhận theo UUID).

Thông tin thư viện (job.json, ảnh bìa, phụ đề, lời thoại) luôn nằm trên laptop trong jobs/<id>/.
Chỉ file video thành phẩm và file tạm khi xử lý được đặt trên nơi lưu đã chọn:
    <ổ>/LongTiengVideos/<id>/dubbed.mp4
    <ổ>/LongTiengVideos/_tmp/<id>/
"""
import errno
import json
import shutil
import subprocess
import time
from pathlib import Path

BASE = Path(__file__).parent
JOBS_DIR = BASE / "jobs"
CONFIG = BASE / "config.json"
SUBDIR = "LongTiengVideos"
MIN_FREE = 3 * 1024 ** 3  # còn dưới 3GB thì tạm dừng xử lý
FS_OK = {"ext4", "ext3", "ext2", "ntfs", "ntfs3", "exfat", "vfat", "btrfs", "xfs", "f2fs"}

_cache = {"t": 0, "parts": []}
held = set()  # ổ đang được "tháo an toàn": không ghi, không tự mở lại cho tới khi rút ra / dùng lại
_mount_fail = {}  # uuid -> (thời điểm, lý do) để không thử mount liên tục


def load_config():
    try:
        return json.loads(CONFIG.read_text("utf-8"))
    except Exception:
        return {}


def current():
    return load_config().get("location", "local")


def set_current(loc):
    cfg = load_config()
    cfg["location"] = loc
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), "utf-8")


def _is_system(mountpoint):
    if not mountpoint:
        return False
    if mountpoint in ("/", "/boot", "/boot/efi", "[SWAP]"):
        return True
    try:  # phân vùng chứa chính repo này = "Laptop"
        BASE.resolve().relative_to(mountpoint)
        return True
    except ValueError:
        return False


def partitions(fresh=False):
    if not fresh and time.time() - _cache["t"] < 3:
        return _cache["parts"]
    out = subprocess.run(
        ["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINT,FSAVAIL,TRAN,TYPE"],
        capture_output=True, text=True,
    )
    parts = []

    def walk(dev, tran):
        tran = dev.get("tran") or tran
        if (dev.get("type") == "part" or not dev.get("children")) and dev.get("uuid") \
                and dev.get("fstype") in FS_OK and int(dev.get("size") or 0) > 5 * 1024 ** 3 \
                and not _is_system(dev.get("mountpoint")):
            parts.append({
                "uuid": dev["uuid"], "path": dev["path"], "label": dev.get("label") or dev["name"],
                "fstype": dev["fstype"], "size": int(dev["size"]), "mountpoint": dev.get("mountpoint"),
                "external": tran == "usb",
            })
        for c in dev.get("children") or []:
            walk(c, tran)

    try:
        for d in json.loads(out.stdout).get("blockdevices", []):
            walk(d, None)
    except Exception:
        pass
    _cache.update(t=time.time(), parts=parts)
    return parts


def find(uuid, fresh=False):
    return next((p for p in partitions(fresh) if p["uuid"] == uuid), None)


def _mount_error_hint(part, msg):
    if part["fstype"] in ("ntfs", "ntfs3"):
        return ("Không mở được ổ NTFS — thường do ổ chưa được tháo an toàn hoặc Windows bật Fast Startup "
                "(ổ bị đánh dấu 'dirty'). Hãy cắm ổ vào Windows, chạy chkdsk rồi tháo an toàn; "
                f"hoặc trên Linux chạy: sudo ntfsfix -d {part['path']}")
    return f"Không mở được ổ: {msg.strip()[-200:]}"


def mountpoint(uuid, auto_mount=True):
    """Trả về thư mục gốc của ổ; tự mount nếu ổ đã cắm mà chưa mở. Ném lỗi kèm lý do nếu không dùng được."""
    if uuid in held:
        raise RuntimeError("Ổ đang được tháo an toàn — có thể rút ổ ra")
    part = find(uuid)
    if not part:
        raise RuntimeError("Ổ lưu video chưa được cắm vào máy")
    if part["mountpoint"]:
        return Path(part["mountpoint"])
    if not auto_mount:
        raise RuntimeError("Ổ lưu video chưa được mở")
    failed = _mount_fail.get(uuid)
    if failed and time.time() - failed[0] < 30:
        raise RuntimeError(failed[1])
    r = subprocess.run(["udisksctl", "mount", "-b", part["path"], "--no-user-interaction"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        reason = _mount_error_hint(part, r.stderr)
        _mount_fail[uuid] = (time.time(), reason)
        raise RuntimeError(reason)
    _mount_fail.pop(uuid, None)
    part = find(uuid, fresh=True)
    if not part or not part["mountpoint"]:
        raise RuntimeError("Không mở được ổ lưu video")
    return Path(part["mountpoint"])


def release_unplugged():
    """Ổ đã tháo an toàn và đã rút ra -> lần cắm sau dùng lại bình thường."""
    for uuid in list(held):
        if not find(uuid, fresh=True):
            held.discard(uuid)


def eject(uuid):
    """Unmount rồi tắt nguồn ổ để rút ra an toàn. Ném lỗi kèm lý do nếu ổ đang bận."""
    part = find(uuid, fresh=True)
    if not part:
        return
    if part["mountpoint"]:
        r = subprocess.run(["udisksctl", "unmount", "-b", part["path"], "--no-user-interaction"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            if "busy" in r.stderr.lower():
                raise RuntimeError("Ổ đang được một chương trình khác mở (ví dụ cửa sổ Files, Terminal đang ở trong ổ) — "
                                   "hãy đóng chúng rồi thử lại")
            raise RuntimeError(f"Không tháo được ổ: {r.stderr.strip()[-200:]}")
    # tắt nguồn ổ (đèn ổ tắt); không được cũng không sao vì đã unmount
    subprocess.run(["udisksctl", "power-off", "-b", part["path"], "--no-user-interaction"], capture_output=True)
    _cache["t"] = 0


def root(loc):
    """Thư mục chứa video của nơi lưu (ném lỗi nếu không dùng được)."""
    if loc in (None, "local"):
        return JOBS_DIR
    return mountpoint(loc) / SUBDIR


def video_path(job):
    """Đường dẫn file video để phát: ưu tiên bản giữ trên laptop. None nếu ổ chưa cắm."""
    local = JOBS_DIR / job["id"] / "dubbed.mp4"
    if local.exists():
        return local
    loc = job.get("video_at", "local")
    if loc == "local":
        return None
    try:
        p = root(loc) / job["id"] / "dubbed.mp4"
    except RuntimeError:
        return None
    return p if p.exists() else None


def tmp_dir(loc, job_id):
    base = BASE / "tmp" if loc == "local" else root(loc) / "_tmp"
    return base / job_id


def final_dir(loc, job_id):
    return root(loc) / job_id


def check(loc):
    """(dùng được?, lý do) — đã cắm, ghi được và còn đủ chỗ trống."""
    try:
        r = root(loc)
        r.mkdir(parents=True, exist_ok=True)
        test = r / ".ghi_thu"
        test.write_text("ok")
        test.unlink()
    except RuntimeError as e:
        return False, str(e)
    except OSError as e:
        if e.errno == errno.EACCES:
            mp = r.parent if loc != "local" else r
            return False, f"Không có quyền ghi vào ổ này — chạy lệnh: sudo chown $USER \"{mp}\""
        if e.errno == errno.EROFS:
            return False, "Ổ đang ở chế độ chỉ đọc (thường do Windows Fast Startup hoặc ổ chưa tháo an toàn)"
        return False, f"Không ghi được vào ổ lưu video ({e.strerror})"
    if shutil.disk_usage(r).free < MIN_FREE:
        return False, "Ổ lưu video sắp đầy (còn dưới 3GB) — hãy xoá bớt video hoặc chọn ổ khác"
    return True, None


def describe(loc):
    if loc == "local":
        return "Laptop"
    p = find(loc)
    if p:
        return f"ổ “{p['label']}”"
    label = load_config().get("labels", {}).get(loc)
    return f"ổ “{label}”" if label else "ổ cứng ngoài"


def options():
    """Danh sách nơi có thể chọn để lưu video, kèm dung lượng trống và trạng thái."""
    u = shutil.disk_usage(BASE)
    opts = [{"id": "local", "label": "Laptop (ổ trong máy)", "path": str(JOBS_DIR), "fstype": "",
             "size": u.total, "free": u.free, "connected": True, "ok": u.free >= MIN_FREE,
             "reason": None if u.free >= MIN_FREE else "Sắp đầy (còn dưới 3GB)", "external": False}]
    seen = set()
    for p in partitions(fresh=True):
        seen.add(p["uuid"])
        ok, reason = check(p["uuid"])
        p = find(p["uuid"]) or p  # có thể vừa được mount
        free = shutil.disk_usage(p["mountpoint"]).free if p.get("mountpoint") else None
        opts.append({"id": p["uuid"], "label": p["label"], "path": p.get("mountpoint") or p["path"],
                     "fstype": p["fstype"], "size": p["size"], "free": free, "connected": True,
                     "ok": ok, "reason": reason, "external": p["external"]})
    cur = current()
    if cur != "local" and cur not in seen:  # ổ đang chọn nhưng đã rút ra
        name = load_config().get("labels", {}).get(cur, "Ổ cứng ngoài")
        opts.append({"id": cur, "label": name, "path": "", "fstype": "", "size": None, "free": None,
                     "connected": False, "ok": False, "reason": "Chưa cắm vào máy", "external": True})
    return opts


def remember_label(loc):
    if loc == "local":
        return
    p = find(loc)
    if p:
        cfg = load_config()
        cfg.setdefault("labels", {})[loc] = p["label"]
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), "utf-8")
