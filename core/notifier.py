"""Telegram 推送：文本 / 带图。走本机代理（Telegram 需要）。"""

from __future__ import annotations

import json
import os
import threading
import time

import requests


class TelegramNotifier:
    def __init__(self, cfg_getter):
        self._get_cfg = cfg_getter
        self._lock = threading.Lock()
        self.last_ok: bool | None = None
        self.last_error: str | None = None
        self.last_at: float = 0.0

    def _token(self) -> str:
        cfg = self._get_cfg().get("notify", {})
        path = os.path.expanduser(str(cfg.get("token_file", "~/.dsh/tg-bot/token.txt")))
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()

    def _proxies(self):
        cfg = self._get_cfg().get("notify", {})
        proxy = str(cfg.get("proxy", "")).strip()
        if not proxy:
            return [None]
        return [{"http": proxy, "https": proxy}, None]

    def _post(self, method: str, data: dict, files=None, timeout: int = 25) -> tuple[bool, str]:
        try:
            token = self._token()
        except Exception as exc:  # noqa: BLE001
            return False, f"读取 Bot Token 失败：{exc}"
        url = f"https://api.telegram.org/bot{token}/{method}"
        cfg = self._get_cfg().get("notify", {})
        chat_id = str(cfg.get("chat_id", "")).strip()
        if not chat_id:
            return False, "未配置 chat_id"
        data = {**data, "chat_id": chat_id}
        last = "未尝试"
        for proxies in self._proxies():
            try:
                resp = requests.post(url, data=data, files=files, proxies=proxies, timeout=timeout)
                body = resp.text[:300]
                if resp.status_code == 200 and '"ok":true' in body.replace(" ", ""):
                    return True, body
                last = f"HTTP {resp.status_code}: {body}"
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
        return False, last

    def send_text(self, text: str) -> tuple[bool, str]:
        ok, detail = self._post("sendMessage", {"text": text[:3900], "parse_mode": "HTML"})
        self._record(ok, detail)
        return ok, detail

    def send_photo(self, caption: str, jpeg: bytes) -> tuple[bool, str]:
        ok, detail = self._post(
            "sendPhoto",
            {"caption": caption[:1000], "parse_mode": "HTML"},
            files={"photo": ("sentinel.jpg", jpeg, "image/jpeg")},
        )
        self._record(ok, detail)
        return ok, detail

    def send_media_group(self, caption: str, jpegs: list[bytes]) -> tuple[bool, str]:
        """一条消息里带多张图（相册）。Telegram 要求 2–10 张；1 张自动降级为 sendPhoto。"""
        photos = [j for j in jpegs if j][:10]
        if not photos:
            return self.send_text(caption)
        if len(photos) == 1:
            return self.send_photo(caption, photos[0])
        media = []
        files = {}
        for i, jpeg in enumerate(photos):
            key = f"photo{i}"
            item = {"type": "photo", "media": f"attach://{key}"}
            if i == 0:
                item["caption"] = caption[:1024]
                item["parse_mode"] = "HTML"
            media.append(item)
            files[key] = (f"{i}.jpg", jpeg, "image/jpeg")
        ok, detail = self._post(
            "sendMediaGroup",
            {"media": json.dumps(media, ensure_ascii=False)},
            files=files,
        )
        self._record(ok, detail)
        return ok, detail

    def _record(self, ok: bool, detail: str) -> None:
        with self._lock:
            self.last_ok, self.last_error, self.last_at = ok, (None if ok else detail), time.time()

    def status(self) -> dict:
        with self._lock:
            return {"last_ok": self.last_ok, "last_error": self.last_error, "last_at": self.last_at}
