"""摄像头采集线程：按需打开、空闲自动释放（省电 + 关掉相机指示灯）。"""

from __future__ import annotations

import threading
import time

import cv2


BACKENDS = {"dshow": cv2.CAP_DSHOW, "msmf": cv2.CAP_MSMF}


class Camera:
    def __init__(self, cfg_getter):
        self._get_cfg = cfg_getter
        self._lock = threading.Lock()
        self._cap = None
        self._frame = None
        self._frame_ts = 0.0
        self._opened_at = 0.0
        self._demand_ts = 0.0
        self._error: str | None = None
        self._frames = 0
        self._fps_ts = time.time()
        self._fps = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="camera", daemon=True)

    # ---- 生命周期 -------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close()

    def reset(self) -> None:
        """配置变更后强制重开设备。"""
        self._close()

    def touch(self, seconds: float = 0) -> None:
        """标记「现在需要画面」，空闲计时重置。"""
        now = time.time()
        self._demand_ts = max(self._demand_ts, now + seconds)

    # ---- 对外读取 -------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return {
                "open": self._cap is not None,
                "width": int(self._frame.shape[1]) if self._frame is not None else 0,
                "height": int(self._frame.shape[0]) if self._frame is not None else 0,
                "fps": round(self._fps, 1),
                "frames": self._frames,
                "last_frame_at": self._frame_ts,
                "error": self._error,
                "backend": self._backend_name(),
            }

    def get_frame(self, timeout: float = 2.0):
        """取一帧的拷贝；超时返回 None。会重置空闲计时。"""
        self.touch()
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                frame = None if self._frame is None else self._frame.copy()
                fresh = time.time() - self._frame_ts < 0.5
            if frame is not None and fresh:
                return frame
            time.sleep(0.03)
        return None

    def last_frame(self):
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    # ---- 内部 -----------------------------------------------------
    def _backend_name(self) -> str:
        return self._get_cfg().get("camera", {}).get("backend", "dshow")

    def _open(self) -> bool:
        cfg = self._get_cfg().get("camera", {})
        idx = int(cfg.get("index", 0))
        backend = str(cfg.get("backend", "dshow"))
        order = [backend] if backend in BACKENDS else ["dshow", "msmf"]
        errors = []
        for name in order:
            cap = cv2.VideoCapture(idx, BACKENDS[name])
            if not cap.isOpened():
                cap.release()
                errors.append(f"{name}: 打不开")
                continue
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(cfg.get("width", 640)))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(cfg.get("height", 480)))
            cap.set(cv2.CAP_PROP_FPS, int(cfg.get("fps", 15)))
            frame = None
            for _ in range(30):
                ok, f = cap.read()
                if ok and f is not None and f.size:
                    frame = f
                    break
                time.sleep(0.03)
            if frame is None:
                cap.release()
                errors.append(f"{name}: 无画面")
                continue
            with self._lock:
                self._cap = cap
                self._frame = frame
                self._frame_ts = time.time()
                self._opened_at = time.time()
                self._error = None
            return True
        with self._lock:
            self._error = "摄像头打开失败（" + "；".join(errors or ["未知原因"]) + "）"
        return False

    def _close(self) -> None:
        with self._lock:
            cap = self._cap
            self._cap = None
            self._frame = None
        if cap is not None:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass

    def _loop(self) -> None:
        fail_backoff = 0.0
        while not self._stop.is_set():
            cfg = self._get_cfg().get("camera", {})
            idle = float(cfg.get("idle_release_seconds", 45))
            if time.time() - self._demand_ts > idle:
                if self._cap is not None:
                    self._close()
                time.sleep(0.4)
                continue
            if self._cap is None:
                if time.time() < fail_backoff:
                    time.sleep(1.0)
                    continue
                if not self._open():
                    fail_backoff = time.time() + 5.0
                    time.sleep(1.0)
                    continue
                fail_backoff = 0.0
            ok, frame = self._cap.read()
            if not ok or frame is None:
                with self._lock:
                    self._error = "读取画面失败，正在重连"
                self._close()
                time.sleep(0.5)
                continue
            now = time.time()
            with self._lock:
                prev_ts = self._frame_ts
                self._frame = frame
                self._frame_ts = now
                self._frames += 1
                dt = now - prev_ts
                if 0.001 < dt < 1.0:
                    self._fps = self._fps * 0.85 + (1.0 / dt) * 0.15
            target_fps = max(1, int(cfg.get("fps", 15)))
            time.sleep(max(0.0, 1.0 / target_fps - (time.time() - now)))
