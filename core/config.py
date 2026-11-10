"""配置读写：默认值 + 深合并 + 原子写盘。"""

from __future__ import annotations

import copy
import json
import os
import threading
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
SNAPSHOT_DIR = os.path.join(DATA_DIR, "snapshots")
EVENT_LOG = os.path.join(DATA_DIR, "events.jsonl")

DEFAULTS = {
    "version": 1,
    "armed": False,
    "bt": {
        "enabled": True,
        "target_name": "My Phone",
        "require_disconnected": True,
        "poll_seconds": 3.0,
    },
    "presence": {
        "enabled": True,
        "brightness_tolerance": 0,
        "poll_seconds": 2.0,
    },
    "burst": {
        "enabled": True,
        "interval_ms": 150,
        "count": 6,
        "keep": 3,
    },
    "camera": {
        "index": 0,
        "backend": "dshow",
        "width": 640,
        "height": 480,
        "fps": 15,
        "idle_release_seconds": 45,
    },
    "detect": {
        "loop_seconds": 1.0,
        "compare_seconds": 5.0,
        "pixel_diff": 25,
        "change_ratio": 0.02,
        "motion_speed": 12.0,
        "face_enabled": True,
        "face_score": 0.8,
        "face_min_size": 48,
        "body_enabled": True,
        "body_ratio": 0.08,
        "cooldown_seconds": 60,
    },
    "notify": {
        "chat_id": "",
        "token_file": "~/.watch-sentinel/token.txt",
        "proxy": "http://127.0.0.1:7897",
        "send_photo": True,
        "send_text_for_change": True,
        "photo_quality": 82,
        "min_gap_seconds": 8,
    },
    "api": {
        "host": "0.0.0.0",
        "port": 8099,
        "token": "",
    },
    "auth": {
        "enabled": False,
        "secret_file": "~/.watch-sentinel/totp.json",
        "session_days": 30,
        "max_failures": 5,
        "window_seconds": 60,
    },
    "storage": {
        "keep_events": 500,
        "keep_snapshots": 200,
        "snapshot_dir": os.path.join(BASE_DIR, "data", "snapshots"),
    },
}

# 数值字段的合法范围 (min, max)；用于清洗前端传来的值
RANGES = {
    ("bt", "poll_seconds"): (1.0, 60.0),
    ("presence", "brightness_tolerance"): (0, 50),
    ("presence", "poll_seconds"): (0.5, 30.0),
    ("burst", "interval_ms"): (50, 2000),
    ("burst", "count"): (2, 20),
    ("burst", "keep"): (1, 10),
    ("camera", "index"): (0, 8),
    ("camera", "width"): (320, 3840),
    ("camera", "height"): (240, 2160),
    ("camera", "fps"): (1, 60),
    ("camera", "idle_release_seconds"): (5, 3600),
    ("detect", "loop_seconds"): (0.3, 10.0),
    ("detect", "compare_seconds"): (1.0, 120.0),
    ("detect", "pixel_diff"): (1, 120),
    ("detect", "change_ratio"): (0.0001, 1.0),
    ("detect", "motion_speed"): (0.1, 500.0),
    ("detect", "face_score"): (0.1, 0.99),
    ("detect", "face_min_size"): (16, 600),
    ("detect", "body_ratio"): (0.001, 1.0),
    ("detect", "cooldown_seconds"): (5, 3600),
    ("notify", "photo_quality"): (40, 95),
    ("notify", "min_gap_seconds"): (0, 300),
    ("api", "port"): (1024, 65535),
    ("auth", "session_days"): (1, 365),
    ("auth", "max_failures"): (1, 20),
    ("auth", "window_seconds"): (10, 3600),
    ("storage", "keep_events"): (20, 5000),
    ("storage", "keep_snapshots"): (10, 2000),
}

ENUMS = {
    ("camera", "backend"): {"dshow", "msmf", "auto"},
}


def deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def sanitize(cfg: dict) -> dict:
    out = deep_merge(DEFAULTS, cfg)
    for (section, key), (lo, hi) in RANGES.items():
        try:
            val = float(out[section][key])
        except (KeyError, TypeError, ValueError):
            out[section][key] = DEFAULTS[section][key]
            continue
        if key in {"pixel_diff", "face_min_size", "width", "height", "fps", "port",
                   "keep_events", "keep_snapshots", "photo_quality", "min_gap_seconds", "index",
                   "brightness_tolerance", "session_days", "max_failures", "window_seconds",
                   "interval_ms", "count", "keep"}:
            out[section][key] = int(max(lo, min(hi, round(val))))
        else:
            out[section][key] = max(lo, min(hi, val))
    for (section, key), allowed in ENUMS.items():
        if out[section].get(key) not in allowed:
            out[section][key] = DEFAULTS[section][key]
    for section in ("armed",):
        out[section] = bool(out.get(section, False))
    for section in ("face_enabled", "body_enabled"):
        out["detect"][section] = bool(out["detect"].get(section, True))
    for section in ("enabled", "require_disconnected"):
        out["bt"][section] = bool(out["bt"].get(section, True))
    out["presence"]["enabled"] = bool(out["presence"].get("enabled", True))
    out["auth"]["enabled"] = bool(out["auth"].get("enabled", True))
    out["auth"]["secret_file"] = str(out["auth"].get("secret_file") or "~/.dsh/auth/state.json")
    out["burst"]["enabled"] = bool(out["burst"].get("enabled", True))
    # 发送张数不能超过连拍张数
    if out["burst"]["keep"] > out["burst"]["count"]:
        out["burst"]["keep"] = out["burst"]["count"]
    for section in ("send_photo", "send_text_for_change"):
        out["notify"][section] = bool(out["notify"].get(section, True))
    out["bt"]["target_name"] = str(out["bt"].get("target_name") or "").strip()
    out["notify"]["chat_id"] = str(out["notify"].get("chat_id") or "").strip()
    out["api"]["token"] = str(out["api"].get("token") or "").strip()
    out["storage"]["snapshot_dir"] = str(
        out["storage"].get("snapshot_dir") or DEFAULTS["storage"]["snapshot_dir"]
    ).strip()
    return out


class ConfigStore:
    def __init__(self, path: str = CONFIG_PATH):
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._cfg = sanitize(self._load_raw())

    def _load_raw(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}

    def get(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._cfg)

    def patch(self, patch: dict) -> dict:
        with self._lock:
            self._cfg = sanitize(deep_merge(self._cfg, patch))
            self._save()
            return copy.deepcopy(self._cfg)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._cfg, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        try:
            os.utime(self.path, None)
        except OSError:
            pass


def now_text(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts if ts is not None else time.time()))
