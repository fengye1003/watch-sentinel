"""低耗视觉分析：帧差变化 + YuNet 人脸检测 + 大块运动目标（人形近似）。

只用 CPU（无 GPU、无 PyTorch），640x480 下单次分析约 10-25 ms。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time

import cv2
import numpy as np

SMALL_W = 320


def ascii_model_path(path: str) -> str:
    """OpenCV 的 DNN 读模型时用 ANSI 路径，中文路径会报 Can't read ONNX file。
    这里把模型复制到纯 ASCII 的缓存目录再加载。"""
    if path.isascii():
        return path
    cache = os.path.join(
        os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "sentinel", "models"
    )
    os.makedirs(cache, exist_ok=True)
    dest = os.path.join(cache, os.path.basename(path))
    try:
        if not os.path.exists(dest) or os.path.getsize(dest) != os.path.getsize(path):
            shutil.copyfile(path, dest)
        return dest
    except OSError:
        return path


class Analyzer:
    def __init__(self, model_path: str):
        self.model_path = model_path
        self._detector = None
        self._input_size = None
        self._load_error = None
        try:
            self._detector = cv2.FaceDetectorYN.create(
                ascii_model_path(model_path), "", (SMALL_W, SMALL_W), score_threshold=0.5,
                nms_threshold=0.3, top_k=5000,
            )
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"{type(exc).__name__}: {exc}"

    @property
    def face_ready(self) -> bool:
        return self._detector is not None

    @property
    def error(self) -> str | None:
        return self._load_error

    # ---- 基础工具 -------------------------------------------------
    @staticmethod
    def small_gray(frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        nw = SMALL_W
        nh = max(1, round(h * nw / w))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.resize(gray, (nw, nh), interpolation=cv2.INTER_AREA)

    def detect_faces(self, frame: np.ndarray, cfg: dict) -> list[dict]:
        if self._detector is None:
            return []
        h, w = frame.shape[:2]
        if self._input_size != (w, h):
            self._detector.setInputSize((w, h))
            self._input_size = (w, h)
        _, faces = self._detector.detect(frame)
        out = []
        if faces is None:
            return out
        score_thr = float(cfg.get("face_score", 0.75))
        min_size = float(cfg.get("face_min_size", 40))
        for row in faces:
            x, y, fw, fh, score = row[0], row[1], row[2], row[3], row[14]
            if score < score_thr or fw < min_size:
                continue
            out.append({"x": float(x), "y": float(y), "w": float(fw), "h": float(fh), "score": float(score)})
        out.sort(key=lambda f: f["score"], reverse=True)
        return out

    # ---- 清晰度 ---------------------------------------------------
    @staticmethod
    def sharpness(frame: np.ndarray) -> float:
        """Laplacian 方差：值越大边缘越锐利，是经典且几乎零成本的清晰度判据。"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())

    # ---- 主分析 ---------------------------------------------------
    def analyze(self, frame: np.ndarray, ref_small, prev_small, dt: float, cfg: dict) -> dict:
        t0 = time.perf_counter()
        small = self.small_gray(frame)
        if ref_small is None or ref_small.shape != small.shape:
            ref_small = small.copy()
        if prev_small is None or prev_small.shape != small.shape:
            prev_small = small.copy()

        diff = cv2.absdiff(small, ref_small)
        _, mask = cv2.threshold(diff, int(cfg.get("pixel_diff", 25)), 255, cv2.THRESH_BINARY)
        changed = int(cv2.countNonZero(mask))
        total = mask.size
        change_ratio = changed / total if total else 0.0

        speed = 0.0
        if prev_small is not None and dt > 0:
            prev_diff = cv2.absdiff(small, prev_small)
            prev_mask = cv2.threshold(prev_diff, int(cfg.get("pixel_diff", 25)), 255, cv2.THRESH_BINARY)[1]
            speed = (cv2.countNonZero(prev_mask) / total) * 100.0 / max(dt, 0.1)

        body = self._largest_blob(mask)
        faces = self.detect_faces(frame, cfg) if cfg.get("face_enabled", True) else []
        found_body = bool(
            cfg.get("body_enabled", True)
            and body
            and body["area_ratio"] >= float(cfg.get("body_ratio", 0.08))
            and body["h_ratio"] >= 0.18
            and body["aspect"] >= 1.05
        )

        elapsed_ms = (time.perf_counter() - t0) * 1000
        return {
            "ts": time.time(),
            "change_ratio": round(change_ratio, 4),
            "speed": round(speed, 2),
            "faces": faces,
            "face_count": len(faces),
            "face_max_score": round(max((f["score"] for f in faces), default=0.0), 3),
            "body": body,
            "body_found": found_body,
            "changed_pixels": changed,
            "total_pixels": total,
            "ms": round(elapsed_ms, 1),
            "small_size": (small.shape[1], small.shape[0]),
            "small": small,
        }

    @staticmethod
    def _largest_blob(mask: np.ndarray):
        kernel = np.ones((3, 3), np.uint8)
        cleaned = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        biggest = max(contours, key=cv2.contourArea)
        x, y, w, h = cv2.boundingRect(biggest)
        area = cv2.contourArea(biggest)
        total = mask.size
        return {
            "x": int(x), "y": int(y), "w": int(w), "h": int(h),
            "area_ratio": round(area / total, 4) if total else 0.0,
            "h_ratio": round(h / mask.shape[0], 3),
            "aspect": round(h / max(w, 1), 2),
            "small_size": (mask.shape[1], mask.shape[0]),
        }

    # ---- 出图 -----------------------------------------------------
    @staticmethod
    def annotate(frame: np.ndarray, metrics: dict, cfg: dict) -> np.ndarray:
        out = frame.copy()
        h, w = out.shape[:2]
        small_w, small_h = metrics.get("small_size") or (SMALL_W, max(1, round(h * SMALL_W / w)))
        sx, sy = w / max(small_w, 1), h / max(small_h, 1)

        for face in metrics.get("faces", []):
            x, y, fw, fh = (int(face["x"]), int(face["y"]), int(face["w"]), int(face["h"]))
            cv2.rectangle(out, (x, y), (x + fw, y + fh), (122, 229, 130), 2)
            cv2.putText(out, f"face {face['score']:.2f}", (x, max(16, y - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (122, 229, 130), 1, cv2.LINE_AA)

        body = metrics.get("body")
        if metrics.get("body_found") and body:
            x, y = int(body["x"] * sx), int(body["y"] * sy)
            bw, bh = int(body["w"] * sx), int(body["h"] * sy)
            cv2.rectangle(out, (x, y), (x + bw, y + bh), (140, 184, 255), 2)
            cv2.putText(out, "motion body", (x, max(16, y - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (140, 184, 255), 1, cv2.LINE_AA)

        bar = out.copy()
        cv2.rectangle(bar, (0, h - 34), (w, h), (20, 20, 24), -1)
        cv2.addWeighted(bar, 0.55, out, 0.45, 0, out)
        text = (
            f"{time.strftime('%H:%M:%S')}  change {metrics.get('change_ratio', 0) * 100:.1f}%"
            f"  speed {metrics.get('speed', 0):.1f}%/s  faces {metrics.get('face_count', 0)}"
            f"  {metrics.get('ms', 0):.0f}ms"
        )
        cv2.putText(out, text, (10, h - 11), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (235, 235, 240), 1, cv2.LINE_AA)
        return out

    @staticmethod
    def encode_jpeg(frame: np.ndarray, quality: int = 82) -> bytes:
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return buf.tobytes() if ok else b""
