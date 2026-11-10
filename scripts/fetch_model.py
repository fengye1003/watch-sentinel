"""Download the YuNet face detector used by Watch Sentinel.

The model is NOT committed to this repository (license/binary hygiene);
run this once after cloning:

    python scripts/fetch_model.py

It lands in models/face_detection_yunet_2023mar.onnx and is about 230 KB.
Set HTTPS_PROXY if you are behind a proxy (e.g. http://127.0.0.1:7897).
"""

from __future__ import annotations

import os
import sys

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.path.join(ROOT, "models", "face_detection_yunet_2023mar.onnx")
URLS = [
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
]


def main() -> int:
    os.makedirs(os.path.dirname(DEST), exist_ok=True)
    if os.path.exists(DEST) and os.path.getsize(DEST) > 100_000:
        print(f"[ok] already present: {DEST} ({os.path.getsize(DEST)} bytes)")
        return 0

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("HTTP_PROXY")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    for url in URLS:
        try:
            print(f"[..] GET {url}")
            resp = requests.get(url, timeout=(10, 90), proxies=proxies)
            resp.raise_for_status()
            if len(resp.content) < 100_000:
                print(f"[!!] too small ({len(resp.content)} bytes), skipping")
                continue
            with open(DEST, "wb") as fh:
                fh.write(resp.content)
            print(f"[ok] saved {DEST} ({len(resp.content)} bytes)")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"[!!] {type(exc).__name__}: {exc}")
    print("[xx] download failed. Download it manually from OpenCV Zoo:")
    print("     https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet")
    return 1


if __name__ == "__main__":
    sys.exit(main())
