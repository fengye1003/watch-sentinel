"""TOTP 认证（RFC 6238 / SHA-1 / 6 位 / 30 s / ±1 步），可选。

密钥文件默认 `~/.watch-sentinel/totp.json`，内容 `{"secret": "<Base32>"}`。
也可以把 `auth.secret_file` 指向别的程序（例如 DSH Web 面板的
`~/.dsh/auth/state.json`）复用同一个验证器条目。

会话：HMAC 签名的过期时间写进 `sentinel_auth` cookie（HttpOnly / SameSite=Lax），
默认 30 天；密钥落在 `data/session.key`（删掉 = 所有会话失效）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import threading
import time

COOKIE_NAME = "sentinel_auth"
TOTP_PERIOD = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1


def totp_at(secret_b32: str, timestamp: int, period: int = TOTP_PERIOD, digits: int = TOTP_DIGITS) -> str:
    padded = secret_b32.strip().upper()
    padded += "=" * ((8 - len(padded) % 8) % 8)
    key = base64.b32decode(padded)
    counter = struct.pack(">Q", int(timestamp) // period)
    mac = hmac.new(key, counter, hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def verify_totp(secret_b32: str, code: str, now: float | None = None, window: int = TOTP_WINDOW) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != TOTP_DIGITS:
        return False
    now = time.time() if now is None else now
    for offset in range(-window, window + 1):
        if hmac.compare_digest(totp_at(secret_b32, int(now) + offset * TOTP_PERIOD), code):
            return True
    return False


class AuthManager:
    def __init__(self, cfg_getter, data_dir: str):
        self._get_cfg = cfg_getter
        self._lock = threading.Lock()
        self._key_path = os.path.join(data_dir, "session.key")
        self._key = self._load_or_create_key()
        self._secret_cache: tuple[str, float, str] | None = None
        self._failures: dict[str, list[float]] = {}

    # ---- 会话密钥 -------------------------------------------------
    def _load_or_create_key(self) -> bytes:
        try:
            with open(self._key_path, "rb") as fh:
                key = fh.read()
            if len(key) >= 32:
                return key
        except OSError:
            pass
        key = secrets.token_bytes(48)
        os.makedirs(os.path.dirname(self._key_path), exist_ok=True)
        with open(self._key_path, "wb") as fh:
            fh.write(key)
        return key

    # ---- 共享 TOTP secret -----------------------------------------
    def _secret(self) -> str:
        path = os.path.expanduser(str(self._get_cfg()["auth"].get("secret_file", "~/.dsh/auth/state.json")))
        try:
            mtime = os.path.getmtime(path)
        except OSError as exc:
            raise RuntimeError(f"读取 TOTP 密钥文件失败：{exc}") from exc
        with self._lock:
            cached = self._secret_cache
            if cached and cached[0] == path and cached[1] == mtime:
                return cached[2]
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        secret = str(data.get("secret") or "").strip()
        if len(secret) < 16:
            raise RuntimeError("state.json 里没有可用的 TOTP secret")
        with self._lock:
            self._secret_cache = (path, mtime, secret)
        return secret

    # ---- 验证 / 会话 ----------------------------------------------
    def verify_code(self, code: str) -> bool:
        return verify_totp(self._secret(), code)

    def _sign(self, expires: int) -> str:
        msg = f"sentinel-auth-v1|{expires}".encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def issue_cookie(self) -> str:
        days = max(1, int(self._get_cfg()["auth"].get("session_days", 30)))
        expires = int(time.time()) + days * 86400
        return f"{expires}.{self._sign(expires)}"

    def check_cookie(self, raw: str | None) -> bool:
        if not raw or "." not in raw:
            return False
        expires_text, signature = raw.split(".", 1)
        try:
            expires = int(expires_text)
        except ValueError:
            return False
        if expires < time.time():
            return False
        return hmac.compare_digest(self._sign(expires), signature)

    # ---- 失败限速（每 IP 5 次 / 60 s，和 DSH 一致） ----------------
    def retry_after(self, ip: str) -> int:
        cfg = self._get_cfg()["auth"]
        limit = int(cfg.get("max_failures", 5))
        window = int(cfg.get("window_seconds", 60))
        now = time.time()
        with self._lock:
            bucket = [t for t in self._failures.get(ip, []) if now - t < window]
            self._failures[ip] = bucket
            if len(bucket) >= limit:
                return max(1, int(window - (now - bucket[0])))
        return 0

    def record_failure(self, ip: str) -> None:
        with self._lock:
            self._failures.setdefault(ip, []).append(time.time())
