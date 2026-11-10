"""守望 Sentinel 的 HTTP 服务（Starlette + uvicorn）。

API 开放在 tailnet（默认 0.0.0.0:8099），面板同源。
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.auth import COOKIE_NAME, AuthManager  # noqa: E402
from core.config import BASE_DIR, DATA_DIR, ConfigStore  # noqa: E402
from core.engine import SentinelEngine  # noqa: E402

WEB_DIR = os.path.join(BASE_DIR, "web")
cfg_store = ConfigStore()
engine = SentinelEngine(cfg_store)
auth = AuthManager(cfg_store.get, DATA_DIR)

PUBLIC_PATHS = {"/login", "/favicon.ico", "/api/health", "/api/auth/login", "/api/auth/logout"}


@asynccontextmanager
async def lifespan(app: Starlette):
    engine.start()
    yield
    engine.stop()


class AuthGuard(BaseHTTPMiddleware):
    """TOTP 会话守卫：未登录时 API 返回 401、页面跳 /login。"""

    async def dispatch(self, request, call_next):
        cfg = cfg_store.get()["auth"]
        if not cfg["enabled"]:
            return await call_next(request)
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)
        if auth.check_cookie(request.cookies.get(COOKIE_NAME)):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse(
                {"ok": False, "error": "unauthorized", "need": "totp", "login": "/login"},
                status_code=401,
            )
        return RedirectResponse("/login", status_code=303)


class TokenGuard(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        token = cfg_store.get()["api"]["token"]
        if token and request.url.path.startswith("/api/"):
            given = request.headers.get("x-sentinel-token") or request.query_params.get("token", "")
            if given != token:
                return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def index(request):
    return FileResponse(
        os.path.join(WEB_DIR, "index.html"),
        headers={"Cache-Control": "no-store"},
    )


async def api_state(request):
    cfg = cfg_store.get()
    state = engine.status()
    state["config"] = cfg
    state["events"] = engine.events.recent(40)
    state["now"] = time.time()
    state["version"] = 1
    state["auth"] = {
        "enabled": bool(cfg["auth"]["enabled"]),
        "session_days": int(cfg["auth"]["session_days"]),
    }
    return JSONResponse(state)


async def api_config_get(request):
    return JSONResponse({"ok": True, "config": cfg_store.get()})


async def api_config_patch(request):
    try:
        patch = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "JSON 解析失败"}, status_code=400)
    if not isinstance(patch, dict):
        return JSONResponse({"ok": False, "error": "需要 JSON 对象"}, status_code=400)
    old = cfg_store.get()
    new = cfg_store.patch(patch)
    if new["camera"]["index"] != old["camera"]["index"] or new["camera"]["backend"] != old["camera"]["backend"]:
        engine.camera.reset()
    if new["armed"] != old["armed"]:
        engine.events.add(
            "system",
            "哨兵已启用" if new["armed"] else "哨兵已停用",
            "通过面板切换",
        )
    return JSONResponse({"ok": True, "config": new})


async def api_arm(request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    armed = bool(body.get("armed", True))
    new = cfg_store.patch({"armed": armed})
    engine.events.add("system", "哨兵已启用" if armed else "哨兵已停用", "通过面板切换")
    return JSONResponse({"ok": True, "armed": new["armed"], "config": new})


async def api_bt_refresh(request):
    await run_in_threadpool(engine.bt.refresh)
    return JSONResponse({"ok": True, "bt": engine.bt.snapshot(cfg_store.get()["bt"]["target_name"])})


async def api_presence_refresh(request):
    state = await run_in_threadpool(engine.presence.refresh)
    return JSONResponse({"ok": True, "presence": state})


async def api_drill(request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    seconds = float(body.get("seconds", 10))
    result = await run_in_threadpool(engine.drill, seconds)
    return JSONResponse(result)


async def api_telegram_test(request):
    ok, detail = await run_in_threadpool(engine.test_telegram)
    return JSONResponse({"ok": ok, "detail": detail})


async def api_telegram_simulate(request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    reason = "change" if body.get("reason") == "change" else "human"
    ok, detail = await run_in_threadpool(engine.simulate_alert, reason)
    return JSONResponse({"ok": ok, "detail": detail})


async def api_camera_probe(request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    index = int(body.get("index", 0))

    def probe():
        import cv2

        for backend, code in (("dshow", cv2.CAP_DSHOW), ("msmf", cv2.CAP_MSMF)):
            cap = cv2.VideoCapture(index, code)
            if not cap.isOpened():
                cap.release()
                continue
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            ok, frame = cap.read()
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            cap.release()
            if ok and frame is not None:
                return {"ok": True, "backend": backend, "width": w, "height": h}
        return {"ok": False, "error": "该序号打不开"}

    result = await run_in_threadpool(probe)
    return JSONResponse(result)


async def api_snapshot(request):
    engine.touch_camera(30)
    frame = await run_in_threadpool(engine.camera.get_frame, 4.0)
    if frame is None:
        return Response(b"", status_code=503, media_type="image/jpeg")
    metrics = engine.status().get("metrics") or {}
    from core.detector import Analyzer

    annotated = Analyzer.annotate(frame, metrics, cfg_store.get())
    jpeg = Analyzer.encode_jpeg(annotated, 78)
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


async def api_snapshot_file(request):
    path = engine.events.snapshot_path(request.path_params.get("name", ""))
    if not path:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return FileResponse(path, media_type="image/jpeg")


async def api_events(request):
    limit = int(request.query_params.get("limit", 50))
    return JSONResponse({"ok": True, "events": engine.events.recent(min(max(limit, 1), 500))})


async def api_events_clear(request):
    engine.events.clear()
    return JSONResponse({"ok": True})


async def api_health(request):
    return JSONResponse({"ok": True, "time": time.time()})


async def login_page(request):
    if not cfg_store.get()["auth"]["enabled"]:
        return RedirectResponse("/", status_code=303)
    if auth.check_cookie(request.cookies.get(COOKIE_NAME)):
        return RedirectResponse("/", status_code=303)
    return FileResponse(os.path.join(WEB_DIR, "login.html"), headers={"Cache-Control": "no-store"})


async def api_login(request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    ip = request.client.host if request.client else "unknown"
    wait = auth.retry_after(ip)
    if wait:
        return JSONResponse({"ok": False, "error": "rate_limited", "retry_after": wait}, status_code=429)
    try:
        ok = auth.verify_code(str(body.get("code", "")))
        err = None
    except Exception as exc:  # noqa: BLE001
        ok, err = False, str(exc)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=500)
    if not ok:
        auth.record_failure(ip)
        return JSONResponse({"ok": False, "error": "code_invalid"}, status_code=401)
    days = int(cfg_store.get()["auth"]["session_days"])
    resp = JSONResponse({"ok": True, "days": days})
    resp.set_cookie(
        COOKIE_NAME, auth.issue_cookie(), max_age=days * 86400,
        httponly=True, samesite="lax", path="/",
    )
    engine.events.add("system", "面板登录", f"来自 {ip}")
    return resp


async def api_logout(request):
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


routes = [
    Route("/login", login_page),
    Route("/api/auth/login", api_login, methods=["POST"]),
    Route("/api/auth/logout", api_logout, methods=["POST"]),
    Route("/", index),
    Route("/api/state", api_state),
    Route("/api/config", api_config_get, methods=["GET"]),
    Route("/api/config", api_config_patch, methods=["PATCH"]),
    Route("/api/arm", api_arm, methods=["POST"]),
    Route("/api/bt/refresh", api_bt_refresh, methods=["POST"]),
    Route("/api/presence/refresh", api_presence_refresh, methods=["POST"]),
    Route("/api/telegram/test", api_telegram_test, methods=["POST"]),
    Route("/api/telegram/simulate", api_telegram_simulate, methods=["POST"]),
    Route("/api/camera/probe", api_camera_probe, methods=["POST"]),
    Route("/api/drill", api_drill, methods=["POST"]),
    Route("/api/snapshot.jpg", api_snapshot),
    Route("/api/snapshots/{name}", api_snapshot_file),
    Route("/api/events", api_events),
    Route("/api/events/clear", api_events_clear, methods=["POST"]),
    Route("/api/health", api_health),
    Mount("/static", app=StaticFiles(directory=WEB_DIR), name="static"),
]

app = Starlette(
    routes=routes,
    middleware=[Middleware(AuthGuard), Middleware(TokenGuard)],
    lifespan=lifespan,
)


if __name__ == "__main__":
    import uvicorn

    conf = cfg_store.get()
    host = conf["api"]["host"]
    port = int(conf["api"]["port"])
    print(f"[sentinel] serving on http://{host}:{port}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)
