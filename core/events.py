"""事件日志：内存环形 + JSONL 落盘 + 截图清理。

截图默认落在 storage.snapshot_dir（`data/snapshots`），可在配置里改到
大容量目录或另一块盘。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque

from .config import EVENT_LOG, SNAPSHOT_DIR


class EventLog:
    def __init__(self, keep: int = 500, snapshot_dir_getter=None):
        self._lock = threading.Lock()
        self._items: deque[dict] = deque(maxlen=keep)
        self._get_dir = snapshot_dir_getter or (lambda: SNAPSHOT_DIR)
        os.makedirs(os.path.dirname(EVENT_LOG), exist_ok=True)
        self._ensure_dir()
        self._load_tail(keep)

    def _ensure_dir(self) -> str:
        want = self._get_dir() or SNAPSHOT_DIR
        try:
            os.makedirs(want, exist_ok=True)
            return want
        except OSError:
            os.makedirs(SNAPSHOT_DIR, exist_ok=True)
            return SNAPSHOT_DIR

    def _load_tail(self, limit: int) -> None:
        try:
            with open(EVENT_LOG, encoding="utf-8") as fh:
                lines = fh.readlines()[-limit:]
            for line in lines:
                try:
                    self._items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass

    def add(self, kind: str, title: str, detail: str = "", metrics: dict | None = None,
            snapshot: str | None = None) -> dict:
        event = {
            "id": f"{int(time.time() * 1000)}-{len(self._items)}",
            "ts": time.time(),
            "time": time.strftime("%H:%M:%S"),
            "date": time.strftime("%Y-%m-%d"),
            "kind": kind,
            "title": title,
            "detail": detail,
            "metrics": metrics or {},
            "snapshot": snapshot,
        }
        with self._lock:
            self._items.append(event)
        try:
            with open(EVENT_LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return event

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(self._items)[-limit:][::-1]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
        try:
            open(EVENT_LOG, "w", encoding="utf-8").close()
        except OSError:
            pass

    def save_snapshot(self, jpeg: bytes, keep: int = 200) -> str | None:
        if not jpeg:
            return None
        folder = self._ensure_dir()
        name = time.strftime("sentinel-%Y%m%d-%H%M%S.jpg")
        path = os.path.join(folder, name)
        try:
            with open(path, "wb") as fh:
                fh.write(jpeg)
        except OSError:
            return None
        self._trim_snapshots(folder, keep)
        return name

    @staticmethod
    def _trim_snapshots(folder: str, keep: int) -> None:
        try:
            files = sorted(
                (os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".jpg")),
                key=os.path.getmtime,
            )
            for old in files[: max(0, len(files) - keep)]:
                os.remove(old)
        except OSError:
            pass

    def snapshot_path(self, name: str) -> str | None:
        if not name:
            return None
        path = os.path.join(self._ensure_dir(), os.path.basename(name))
        return path if os.path.exists(path) else None
