"""Nơi lưu video: ngay trên laptop, hoặc một phân vùng / ổ cứng gắn ngoài (nhận theo UUID).

Thông tin thư viện (job.json, ảnh bìa, phụ đề, lời thoại) luôn nằm trên laptop trong jobs/<id>/.
Chỉ file video thành phẩm và file tạm khi xử lý được đặt trên nơi lưu đã chọn:
    <ổ>/LongTiengVideos/<id>/dubbed.mp4
    <ổ>/LongTiengVideos/_tmp/<id>/
"""
import errno
import json
import os
import shutil
import subprocess
import threading
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


_cfg_lock = threading.RLock()
_last_good = {"cfg": {}}


def load_config():
    """Đọc config.json. Đọc lỗi (file đang ghi dở...) thì dùng bản đọc được gần nhất,
    không bao giờ coi là trống — nếu không sẽ tự sinh lại mã PIN, quên nơi lưu video."""
    with _cfg_lock:
        try:
            cfg = json.loads(CONFIG.read_text("utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:
            return json.loads(json.dumps(_last_good["cfg"]))
        _last_good["cfg"] = cfg
        return json.loads(json.dumps(cfg))


def save_config(cfg):
    # ghi ra file tạm rồi thay thế: lúc nào đọc cũng thấy bản cũ hoặc bản mới đầy đủ
    with _cfg_lock:
        tmp = CONFIG.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, CONFIG)
        _last_good["cfg"] = json.loads(json.dumps(cfg))


def update_config(change):
    """Đọc - sửa - ghi config trong cùng một khoá để các luồng không ghi đè lên nhau."""
    with _cfg_lock:
        cfg = load_config()
        change(cfg)
        save_config(cfg)
        return cfg


def current():
    return load_config().get("location", "local")


def set_current(loc):
    update_config(lambda cfg: cfg.__setitem__("location", loc))


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


FORCE_SCRIPT = BASE / "allow-ntfs-force.sh"


def _mount_error_hint(part, msg):
    if part["fstype"] in ("ntfs", "ntfs3"):
        if "not allowed" in msg:
            return ("The drive was unplugged while in use (marked 'dirty'). To let the app reopen it automatically, "
                    f"run once in a terminal: pkexec bash \"{FORCE_SCRIPT}\"")
        return ("Cannot open the NTFS drive — usually because it was not safely ejected or Windows Fast Startup "
                "is on (the drive is marked 'dirty'). Plug it into Windows, run chkdsk and eject it safely.")
    return f"Cannot open the drive: {msg.strip()[-200:]}"


def _udisks_mount(part, *opts):
    return subprocess.run(["udisksctl", "mount", "-b", part["path"], "--no-user-interaction", *opts],
                          capture_output=True, text=True)


def _known(uuid):
    """Ổ web đã dùng để lưu video (nơi lưu hiện tại hoặc từng lưu) -> được phép mở lại khi bị 'dirty'."""
    cfg = load_config()
    return uuid == cfg.get("location") or uuid in cfg.get("labels", {})


def mountpoint(uuid, auto_mount=True):
    """Trả về thư mục gốc của ổ; tự mount nếu ổ đã cắm mà chưa mở. Ném lỗi kèm lý do nếu không dùng được."""
    if uuid in held:
        raise RuntimeError("The drive was safely ejected — you can unplug it")
    part = find(uuid)
    if not part:
        raise RuntimeError("The storage drive is not plugged in")
    if part["mountpoint"]:
        return Path(part["mountpoint"])
    if not auto_mount:
        raise RuntimeError("The storage drive is not mounted")
    failed = _mount_fail.get(uuid)
    if failed and time.time() - failed[0] < 30:
        raise RuntimeError(failed[1])
    r = _udisks_mount(part)
    if r.returncode != 0 and part["fstype"] in ("ntfs", "ntfs3") and _known(uuid):
        # ổ NTFS bị rút / mất kết nối khi đang mở thì bị đánh dấu "dirty" và ntfs3 không chịu mở.
        # Tuỳ chọn force chỉ bỏ qua lời từ chối đó: ntfs3 vẫn đọc lại nhật ký NTFS như khi mở bình thường,
        # không sửa / xoá gì trên ổ (khác với ntfsfix là xoá nhật ký).
        # phải chỉ rõ driver ntfs3, nếu không udisks kiểm tra theo danh sách của driver ntfs cũ và từ chối "force"
        forced = _udisks_mount(part, "-t", "ntfs3", "-o", "force")
        if forced.returncode == 0:
            print(f"  Reopened {part['path']} (was not cleanly unmounted)", flush=True)
            r = forced
        elif "not allowed" in forced.stderr:
            r = forced
    if r.returncode != 0:
        reason = _mount_error_hint(part, r.stderr)
        _mount_fail[uuid] = (time.time(), reason)
        raise RuntimeError(reason)
    _mount_fail.pop(uuid, None)
    part = find(uuid, fresh=True)
    if not part or not part["mountpoint"]:
        raise RuntimeError("Cannot open the storage drive")
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
                raise RuntimeError("The drive is in use by another program (e.g. a Files window or a Terminal "
                                   "inside the drive) — close them and try again")
            raise RuntimeError(f"Cannot eject the drive: {r.stderr.strip()[-200:]}")
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
            return False, f"No permission to write to this drive — run: sudo chown $USER \"{mp}\""
        if e.errno == errno.EROFS:
            return False, "The drive is read-only (usually Windows Fast Startup or it was not safely ejected)"
        return False, f"Cannot write to the storage drive ({e.strerror})"
    if shutil.disk_usage(r).free < MIN_FREE:
        return False, "The storage drive is almost full (under 3 GB left) — delete some videos or choose another drive"
    return True, None


def describe(loc):
    if loc == "local":
        return "Laptop"
    p = find(loc)
    if p:
        return f"drive “{p['label']}”"
    label = load_config().get("labels", {}).get(loc)
    return f"drive “{label}”" if label else "external drive"


def options():
    """Danh sách nơi có thể chọn để lưu video, kèm dung lượng trống và trạng thái."""
    u = shutil.disk_usage(BASE)
    opts = [{"id": "local", "label": "Laptop (internal drive)", "path": str(JOBS_DIR), "fstype": "",
             "size": u.total, "free": u.free, "connected": True, "ok": u.free >= MIN_FREE,
             "reason": None if u.free >= MIN_FREE else "Almost full (under 3 GB left)", "external": False}]
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
        name = load_config().get("labels", {}).get(cur, "External drive")
        opts.append({"id": cur, "label": name, "path": "", "fstype": "", "size": None, "free": None,
                     "connected": False, "ok": False, "reason": "Not plugged in", "external": True})
    return opts


def remember_label(loc):
    if loc == "local":
        return
    p = find(loc)
    if p:
        update_config(lambda cfg: cfg.setdefault("labels", {}).__setitem__(loc, p["label"]))
