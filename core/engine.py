"""哨兵引擎：蓝牙门禁 -> 摄像头取帧 -> 帧差/人脸 -> Telegram 告警。"""

from __future__ import annotations

import os
import threading
import time
import traceback
from collections import deque

from .camera import Camera
from .config import BASE_DIR, ConfigStore
from .detector import Analyzer
from .events import EventLog
from .notifier import TelegramNotifier
from .bt import BluetoothMonitor
from .presence import PresenceMonitor

MODEL_PATH = os.path.join(BASE_DIR, "models", "face_detection_yunet_2023mar.onnx")


class SentinelEngine:
    def __init__(self, cfg: ConfigStore):
        self.cfg = cfg
        self.bt = BluetoothMonitor()
        self.presence = PresenceMonitor()
        self.camera = Camera(cfg.get)
        self.analyzer = Analyzer(MODEL_PATH)
        self.notifier = TelegramNotifier(cfg.get)
        self.events = EventLog(
            keep=int(cfg.get()["storage"]["keep_events"]),
            snapshot_dir_getter=lambda: cfg.get()["storage"]["snapshot_dir"],
        )

        self._lock = threading.RLock()
        self._history: deque[tuple[float, "object"]] = deque(maxlen=240)
        self._prev_small = None
        self._last_analyze = 0.0
        self._metrics: dict | None = None
        self._gate: dict = {"active": False, "reason": "未启动", "phone_away": None}
        self._bt_snap: dict = {}
        self._alerts = 0
        self._last_sharpness = 0.0
        self._last_alert: dict[str, float] = {}
        self._last_send = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="sentinel", daemon=True)
        self.started_at = time.time()

    # ---- 生命周期 -------------------------------------------------
    def start(self) -> None:
        self.camera.start()
        self._thread.start()
        self.events.add("system", "哨兵服务启动", f"监听端口 {self.cfg.get()['api']['port']}")

    def stop(self) -> None:
        self._stop.set()
        self.camera.stop()

    # ---- 主循环 ---------------------------------------------------
    def _loop(self) -> None:
        last_bt = 0.0
        last_presence = 0.0
        error_streak = 0
        while not self._stop.is_set():
            loop_start = time.time()
            try:
                cfg = self.cfg.get()
                if loop_start - last_bt >= float(cfg["bt"]["poll_seconds"]):
                    self.bt.refresh()
                    last_bt = loop_start
                if cfg["presence"]["enabled"] and (
                    last_presence == 0.0 or loop_start - last_presence >= float(cfg["presence"]["poll_seconds"])
                ):
                    self.presence.refresh()
                    last_presence = loop_start
                bt_snap = self.bt.snapshot(cfg["bt"]["target_name"])
                gate = self._gate_for(cfg, bt_snap)
                with self._lock:
                    self._bt_snap = bt_snap
                    self._gate = gate

                if gate["active"]:
                    self.camera.touch(3)
                    frame = self.camera.get_frame(timeout=1.5)
                    if frame is not None:
                        self._process_frame(frame, cfg, bt_snap)
                else:
                    with self._lock:
                        self._history.clear()
                        self._prev_small = None
                        self._metrics = None
                error_streak = 0
            except Exception:  # noqa: BLE001 - 循环不能死
                error_streak += 1
                if error_streak in (1, 10, 100):
                    self.events.add("error", "检测循环异常", traceback.format_exc()[-800:])
                time.sleep(1.0)
            elapsed = time.time() - loop_start
            time.sleep(max(0.05, float(self._current_loop_seconds()) - elapsed))

    def _current_loop_seconds(self) -> float:
        return self.cfg.get()["detect"]["loop_seconds"]

    def _gate_for(self, cfg: dict, bt_snap: dict) -> dict:
        bt_cfg = cfg["bt"]
        if not cfg["armed"]:
            return {"active": False, "reason": "哨兵未启用", "phone_away": None}

        # 1) 手机门禁
        if not bt_cfg["enabled"]:
            prefix = "蓝牙门禁已关闭"
            warning = False
        elif bt_snap.get("error"):
            prefix = "蓝牙状态不可用，按「未连接」处理"
            warning = True
        elif bt_snap.get("connected"):
            if bt_cfg["require_disconnected"]:
                return {
                    "active": False,
                    "reason": f"{bt_cfg['target_name']} 已连接，哨兵待机",
                    "phone_away": False,
                }
            prefix = "手机已连接（已勾选忽略）"
            warning = False
        elif bt_snap.get("found"):
            prefix = f"{bt_cfg['target_name']} 已断开"
            warning = False
        else:
            prefix = f"未找到 {bt_cfg['target_name']}，视为离家"
            warning = True

        # 2) 电脑在场门禁：屏幕最暗 或 已锁屏 才允许报警
        suffix = ""
        presence = self.presence.snapshot()
        pcfg = cfg["presence"]
        if pcfg["enabled"]:
            tol = float(pcfg["brightness_tolerance"])
            br = presence.get("brightness")
            lo = presence.get("brightness_min")
            if presence.get("locked"):
                suffix = "电脑已锁屏"
            elif br is not None and lo is not None and br <= lo + tol:
                suffix = f"屏幕已调至最低（{presence.get('brightness_pct')}%）"
            elif br is None:
                suffix = "读不到屏幕亮度，按离家处理"
                warning = True
            else:
                return {
                    "active": False,
                    "reason": (
                        f"屏幕亮度 {presence.get('brightness_pct')}% 且未锁屏，"
                        "按在电脑前处理，哨兵待机"
                    ),
                    "phone_away": True,
                    "presence": presence,
                    "warning": warning,
                }

        reason = "，".join(part for part in (prefix, suffix) if part) + "，哨兵生效"
        return {"active": True, "reason": reason, "phone_away": True, "warning": warning}

    def _find_ref(self, target_ts: float):
        best = None
        best_gap = 1e9
        with self._lock:
            items = list(self._history)
        for ts, small in items:
            gap = abs(ts - target_ts)
            if gap < best_gap:
                best, best_gap = small, gap
        return best if best_gap <= 2.5 else None

    def _process_frame(self, frame, cfg: dict, bt_snap: dict) -> None:
        now = time.time()
        ref = self._find_ref(now - float(cfg["detect"]["compare_seconds"]))
        dt = now - self._last_analyze if self._last_analyze else 1.0
        metrics = self.analyzer.analyze(frame, ref, self._prev_small, dt, cfg["detect"])
        with self._lock:
            self._metrics = metrics
            self._prev_small = metrics["small"]
            self._last_analyze = now
            self._history.append((now, metrics["small"]))
        self._evaluate(cfg, metrics, frame, bt_snap)

    # ---- 告警判定 -------------------------------------------------
    def _evaluate(self, cfg: dict, metrics: dict, frame, bt_snap: dict) -> None:
        d = cfg["detect"]
        now = time.time()
        change_hit = metrics["change_ratio"] >= float(d["change_ratio"])
        speed_hit = metrics["speed"] >= float(d["motion_speed"])
        human_hit = metrics["face_count"] > 0 or metrics["body_found"] or speed_hit
        if not (change_hit or human_hit):
            return

        reason = "human" if human_hit else "change"
        cooldown = float(d["cooldown_seconds"])
        if now - self._last_alert.get(reason, 0.0) < cooldown:
            return
        min_gap = float(cfg["notify"]["min_gap_seconds"])
        if now - self._last_send < max(0.0, min_gap):
            return

        self._last_alert[reason] = now
        self._last_send = now
        self._alerts += 1
        self._emit(cfg, metrics, frame, bt_snap, reason)

    def _emit(self, cfg: dict, metrics: dict, frame, bt_snap: dict, reason: str,
              simulated: bool = False) -> None:
        d = cfg["detect"]
        speed_hit = metrics["speed"] >= float(d["motion_speed"])
        quality = int(cfg["notify"].get("photo_quality", 82))
        keep = int(cfg["storage"]["keep_snapshots"])
        send_photo = bool(cfg["notify"]["send_photo"]) and reason == "human"

        jpegs: list[bytes] = []
        burst_count = 0
        if send_photo:
            if cfg["burst"]["enabled"]:
                jpegs, burst_count = self._burst_jpegs(cfg, metrics)
            if not jpegs:
                jpegs = [self.analyzer.encode_jpeg(self.analyzer.annotate(frame, metrics, cfg), quality)]
        snapshot = self.events.save_snapshot(jpegs[0], keep) if jpegs else None

        phone = "未连接" if not bt_snap.get("connected") else "已连接"
        target = cfg["bt"]["target_name"]
        if reason == "human":
            parts = []
            if metrics["face_count"]:
                parts.append(f"人脸 {metrics['face_count']} 张（最高 {metrics['face_max_score']:.2f}）")
            if metrics["body_found"]:
                parts.append(f"人形轮廓 {metrics['body']['area_ratio'] * 100:.1f}%")
            if speed_hit:
                parts.append(f"运动速度 {metrics['speed']:.1f}%/s")
            if burst_count and len(jpegs) > 1:
                parts.append(f"连拍 {burst_count} 张取最清晰 {len(jpegs)} 张")
            title = "检测到人形活动"
            caption = (
                f"🛰 <b>守望 · {title}</b>\n"
                + "；".join(parts) + "\n"
                + f"画面变化 {metrics['change_ratio'] * 100:.1f}%\n"
                + f"时间 {time.strftime('%H:%M:%S')} · 手机{phone}（{target}）"
            )
            detail = "；".join(parts)
        else:
            title = "画面发生变化"
            caption = (
                "🛰 <b>守望 · 画面变化</b>\n"
                f"变化像素 <b>{metrics['change_ratio'] * 100:.1f}%</b>（阈值 {float(d['change_ratio']) * 100:.1f}%）\n"
                f"运动速度 {metrics['speed']:.1f}%/s\n"
                f"时间 {time.strftime('%H:%M:%S')} · 手机{phone}（{target}）"
            )
            detail = f"变化 {metrics['change_ratio'] * 100:.1f}%"

        self.events.add("alert", title, detail, metrics=self._public_metrics(metrics), snapshot=snapshot)

        if simulated:
            caption = "🧪 <b>测试告警（非真实触发）</b>\n" + caption

        if reason == "change" and not cfg["notify"]["send_text_for_change"]:
            return
        threading.Thread(
            target=self._deliver_safe, args=(caption, jpegs), name="tg-send", daemon=True,
        ).start()

    def _burst_jpegs(self, cfg: dict, base_metrics: dict) -> tuple[list[bytes], int]:
        """连拍：每 interval 抓一张，按清晰度（Laplacian 方差）取前 keep 张并标注。"""
        b = cfg["burst"]
        interval = max(0.05, float(b.get("interval_ms", 150)) / 1000.0)
        count = max(1, int(b.get("count", 6)))
        count = max(1, min(count, int(8.0 / interval) + 1))  # 总时长上限 8 s
        frames = []
        for i in range(count):
            shot = self.camera.get_frame(timeout=2.0)
            if shot is not None:
                frames.append(shot)
            if i < count - 1:
                time.sleep(interval)
        if not frames:
            return [], 0
        scored = sorted(
            ((self.analyzer.sharpness(f), f) for f in frames),
            key=lambda item: item[0], reverse=True,
        )
        keep = max(1, min(int(b.get("keep", 3)), len(scored), 10))
        quality = int(cfg["notify"].get("photo_quality", 82))
        out: list[bytes] = []
        for _, shot in scored[:keep]:
            h, w = shot.shape[:2]
            faces = (
                self.analyzer.detect_faces(shot, cfg["detect"])
                if cfg["detect"].get("face_enabled") else []
            )
            local = {
                "faces": faces,
                "face_count": len(faces),
                "face_max_score": max((f["score"] for f in faces), default=0.0),
                "change_ratio": base_metrics.get("change_ratio", 0.0),
                "speed": base_metrics.get("speed", 0.0),
                "small_size": (320, max(1, round(h * 320 / w))),
                "ms": 0,
            }
            out.append(self.analyzer.encode_jpeg(self.analyzer.annotate(shot, local, cfg), quality))
        self._last_sharpness = round(scored[0][0], 1)
        return out, len(frames)

    def drill(self, seconds: float = 10.0) -> dict:
        """演练：按当前阈值跑一段，只汇报「会不会触发」，不发 TG、不改告警状态。"""
        cfg = self.cfg.get()
        d = cfg["detect"]
        frame = self.camera.get_frame(timeout=6.0)
        if frame is None:
            return {"ok": False, "error": "摄像头没有画面（被占用或未打开）"}
        samples = []
        history = []
        prev = None
        last_ts = None
        started = time.time()
        deadline = started + max(1.0, min(60.0, float(seconds)))
        best_change = -1.0
        best_frame = None
        best_metrics = None
        while time.time() < deadline:
            frame = self.camera.get_frame(timeout=2.0)
            now = time.time()
            if frame is None:
                break
            target = now - float(d["compare_seconds"])
            ref = None
            if history:
                best = min(history, key=lambda item: abs(item[0] - target))
                if abs(best[0] - target) <= 2.5:
                    ref = best[1]
            dt = now - last_ts if last_ts else 1.0
            m = self.analyzer.analyze(frame, ref, prev, dt, d)
            prev = m["small"]
            last_ts = now
            history.append((now, m["small"]))
            if m["change_ratio"] > best_change:
                best_change = m["change_ratio"]
                best_frame = frame.copy()
                best_metrics = m
            samples.append({
                "t": round(now - started, 1),
                "change": round(m["change_ratio"], 4),
                "speed": round(m["speed"], 2),
                "faces": m["face_count"],
                "body": bool(m["body_found"]),
            })
            time.sleep(max(0.2, float(d["loop_seconds"])))
        if not samples:
            return {"ok": False, "error": "没有采到画面"}
        peak_change = max(s["change"] for s in samples)
        peak_speed = max(s["speed"] for s in samples)
        face_max = max(s["faces"] for s in samples)
        body_any = any(s["body"] for s in samples)
        change_hit = peak_change >= float(d["change_ratio"])
        speed_hit = peak_speed >= float(d["motion_speed"])
        human_hit = face_max > 0 or body_any or speed_hit
        detail = (
            f"峰值变化 {peak_change * 100:.1f}%（阈值 {float(d['change_ratio']) * 100:.1f}%）· "
            f"峰值速度 {peak_speed:.1f}%/s（阈值 {float(d['motion_speed']):.0f}）· "
            f"人脸 {face_max} · 人形 {'有' if body_any else '无'}"
        )
        snapshot = None
        if best_frame is not None and best_metrics is not None:
            annotated = self.analyzer.annotate(best_frame, best_metrics, cfg)
            jpeg = self.analyzer.encode_jpeg(annotated, int(cfg["notify"].get("photo_quality", 82)))
            snapshot = self.events.save_snapshot(jpeg, int(cfg["storage"]["keep_snapshots"]))
        self.events.add(
            "drill",
            "演练：" + ("会触发" if (change_hit or human_hit) else "不会触发"),
            detail,
            metrics={"peak_change": peak_change, "peak_speed": peak_speed, "faces": face_max},
            snapshot=snapshot,
        )
        return {
            "ok": True,
            "seconds": round(time.time() - started, 1),
            "samples": samples,
            "peak": {"change": peak_change, "speed": peak_speed, "faces": face_max, "body": body_any},
            "thresholds": {
                "change": float(d["change_ratio"]),
                "speed": float(d["motion_speed"]),
            },
            "would_trigger": {"change": change_hit, "human": human_hit},
            "detail": detail,
            "snapshot": snapshot,
        }

    def simulate_alert(self, reason: str = "human") -> tuple[bool, str]:
        """用当前画面伪造一次告警，验证「截图 + 推送」整条链路。"""
        frame = self.camera.get_frame(timeout=2.5)
        if frame is None:
            return False, "摄像头没有画面（未打开或被占用）"
        cfg = self.cfg.get()
        metrics = {
            "change_ratio": 0.043,
            "speed": 18.6,
            "faces": [{"x": 250.0, "y": 110.0, "w": 90.0, "h": 90.0, "score": 0.93}],
            "face_count": 1,
            "face_max_score": 0.93,
            "body": {"x": 90, "y": 40, "w": 60, "h": 120, "area_ratio": 0.12,
                     "h_ratio": 0.5, "aspect": 2.0, "small_size": (320, 240)},
            "body_found": True,
            "changed_pixels": 3302,
            "total_pixels": 76800,
            "ms": 0.0,
            "small_size": (320, 240),
        }
        if reason == "change":
            metrics.update({"speed": 3.2, "faces": [], "face_count": 0,
                            "face_max_score": 0.0, "body_found": False, "body": None})
        bt_snap = self.bt.snapshot(cfg["bt"]["target_name"])
        self._alerts += 1
        self._emit(cfg, metrics, frame, bt_snap, reason, simulated=True)
        return True, "测试告警已发送"

    def _deliver_safe(self, caption: str, jpegs: list[bytes]) -> None:
        try:
            self._deliver(caption, jpegs)
        except Exception:  # noqa: BLE001
            self.events.add("notify_error", "Telegram 推送异常", traceback.format_exc()[-700:])

    def _deliver(self, caption: str, jpegs: list[bytes]) -> None:
        if not jpegs:
            ok, err = self.notifier.send_text(caption)
        elif len(jpegs) == 1:
            ok, err = self.notifier.send_photo(caption, jpegs[0])
        else:
            ok, err = self.notifier.send_media_group(caption, jpegs)
        if not ok:
            self.events.add("notify_error", "Telegram 推送失败", str(err)[:500])

    # ---- 对外状态 -------------------------------------------------
    @staticmethod
    def _public_metrics(metrics: dict | None) -> dict | None:
        if not metrics:
            return None
        return {k: v for k, v in metrics.items() if k != "small"}

    def touch_camera(self, seconds: float = 30) -> None:
        self.camera.touch(seconds)

    def status(self) -> dict:
        with self._lock:
            return {
                "armed": bool(self.cfg.get()["armed"]),
                "gate": dict(self._gate),
                "bt": self._bt_snap,
                "presence": self.presence.snapshot(),
                "camera": self.camera.status(),
                "metrics": self._public_metrics(self._metrics),
                "tg": self.notifier.status(),
                "alerts": self._alerts,
                "analyzer": {"face_ready": self.analyzer.face_ready, "error": self.analyzer.error},
                "started_at": self.started_at,
            }

    def test_telegram(self) -> tuple[bool, str]:
        q = self.camera.get_frame(timeout=2.0)
        text = (
            "🛰 <b>守望 · 联通性测试</b>\n"
            f"时间 {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            "TG 推送链路正常。"
        )
        if q is not None and self.cfg.get()["notify"]["send_photo"]:
            return self.notifier.send_photo(text, self.analyzer.encode_jpeg(q, 80))
        return self.notifier.send_text(text)
