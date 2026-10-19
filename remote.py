"""Xem trên điện thoại / máy khác cùng Wi-Fi: bật/tắt, mã PIN, mã QR.

Laptop luôn vào được không cần PIN. Máy khác chỉ vào được khi đã bật tính năng,
ở cùng mạng nội bộ và nhập đúng mã PIN (một lần, sau đó nhớ bằng cookie).
"""
import hashlib
import hmac
import ipaddress
import secrets
import subprocess
import time

import qrcode
import qrcode.image.svg

import storage

PORT = 5000
COOKIE = "ltv_auth"
MAX_FAILS = 10
LOCK_SECONDS = 15 * 60
_fails = {}  # ip -> [số lần sai, thời điểm sai gần nhất]


def settings():
    cfg = storage.load_config()
    r = cfg.get("remote") or {}
    if "pin" not in r or "secret" not in r:
        r.setdefault("enabled", False)
        r.setdefault("pin", f"{secrets.randbelow(10 ** 6):06d}")
        r.setdefault("secret", secrets.token_hex(16))
        cfg["remote"] = r
        storage.save_config(cfg)
    return r


def _update(**fields):
    cfg = storage.load_config()
    cfg["remote"] = {**settings(), **fields}
    storage.save_config(cfg)


def set_enabled(on):
    _update(enabled=bool(on))


def new_pin():
    """Đổi PIN: mọi máy đã đăng nhập trước đó phải nhập PIN mới."""
    _update(pin=f"{secrets.randbelow(10 ** 6):06d}", secret=secrets.token_hex(16))


def token():
    return hmac.new(settings()["secret"].encode(), b"ok", hashlib.sha256).hexdigest()


def lan_ips():
    out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True).stdout
    ips = []
    for line in out.splitlines():
        parts = line.split()
        dev, ip = parts[1], parts[3].split("/")[0]
        if dev == "lo" or dev.startswith(("docker", "br-", "veth", "virbr")):
            continue
        ips.append(ip)
    return ips


def wifi_name():
    out = subprocess.run(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"], capture_output=True, text=True).stdout
    return next((l.split(":", 1)[1] for l in out.splitlines() if l.startswith("yes:")), None)


def qr_svg(text):
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=7, border=2)
    return img.to_string(encoding="unicode")


def is_local(addr):
    return addr in ("127.0.0.1", "::1")


def is_private(addr):
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_private and not ip.is_loopback


def locked(addr):
    f = _fails.get(addr)
    if f and f[0] >= MAX_FAILS:
        if time.time() - f[1] < LOCK_SECONDS:
            return True
        _fails.pop(addr, None)
    return False


def check_pin(addr, pin):
    if locked(addr):
        return False
    if hmac.compare_digest(str(pin or "").strip(), settings()["pin"]):
        _fails.pop(addr, None)
        return True
    f = _fails.setdefault(addr, [0, 0])
    f[0] += 1
    f[1] = time.time()
    return False
