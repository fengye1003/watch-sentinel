"""桌面在场检测：屏幕亮度 + Windows 锁屏状态。

判据（用户 2026-11-10 追加）：
    屏幕亮度不为最低  且  Windows 未锁屏  ->  人还在电脑前，不报警

亮度：WMI root\\wmi:WmiMonitorBrightness（win32com，单次 ~8 ms；dxva2 在本机
      "Lenovo HDR Display" 上 GetMonitorBrightness 返回 ERROR_GEN_FAILURE，不可用）。
锁屏：WTSQuerySessionInformation(WTSSessionInfoEx) 的 SessionFlags
      （WTS_SESSIONSTATE_UNLOCK=0 / LOCK=1，实测未锁屏时为 0）
      + LogonUI.exe 是否存在（锁屏时会在用户会话里拉起）双保险。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WTS_CURRENT_SERVER_HANDLE = 0
WTS_CURRENT_SESSION = 0xFFFFFFFF
WTSSessionInfoEx = 25
WTS_SESSIONSTATE_UNLOCK = 0
WTS_SESSIONSTATE_LOCK = 1

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", wt.LONG),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


_wtsapi32.WTSQuerySessionInformationW.argtypes = [
    wt.HANDLE, wt.DWORD, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.DWORD)
]
_wtsapi32.WTSQuerySessionInformationW.restype = wt.BOOL
_wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]
_kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
_kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_kernel32.Process32FirstW.restype = wt.BOOL
_kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_kernel32.Process32NextW.restype = wt.BOOL
_kernel32.CloseHandle.argtypes = [wt.HANDLE]


def _wts_flags() -> int | None:
    buf = ctypes.c_void_p()
    size = wt.DWORD()
    ok = _wtsapi32.WTSQuerySessionInformationW(
        WTS_CURRENT_SERVER_HANDLE, WTS_CURRENT_SESSION, WTSSessionInfoEx,
        ctypes.byref(buf), ctypes.byref(size),
    )
    if not ok:
        return None
    try:
        raw = ctypes.string_at(buf, size.value)
        if len(raw) < 16:
            return None
        return int.from_bytes(raw[12:16], "little")
    finally:
        _wtsapi32.WTSFreeMemory(buf)


def _logonui_running() -> bool:
    """LogonUI.exe 在用户会话里 = 锁屏/登录界面。"""
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return False
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        if not _kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            return False
        while True:
            if entry.szExeFile.lower() == "logonui.exe":
                return True
            if not _kernel32.Process32NextW(snap, ctypes.byref(entry)):
                return False
    finally:
        _kernel32.CloseHandle(snap)


class PresenceMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = {
            "enabled": True,
            "locked": False,
            "lock_source": "unknown",
            "brightness": None,
            "brightness_min": None,
            "brightness_max": None,
            "brightness_pct": None,
            "error": None,
            "checked_at": 0.0,
        }
        self._wmi = None
        self._com_thread = None
        self._owner_tid = None
        self._io_lock = threading.RLock()
        self._ts = 0.0

    # ---- WMI（COM 线程内初始化） ----------------------------------
    def _ensure_com(self) -> None:
        threading_id = threading.get_ident()
        if self._com_thread == threading_id:
            return
        try:
            import pythoncom

            pythoncom.CoInitialize()
            self._com_thread = threading_id
        except Exception:  # noqa: BLE001
            pass

    def _brightness(self):
        """返回 (current, min, max, error)。WMI/COM 对象按线程隔离：
        引擎线程缓存复用；其它线程（如手动刷新）临时创建、不缓存。"""
        tid = threading.get_ident()
        try:
            import win32com.client

            self._ensure_com()
            if self._owner_tid is None:
                self._owner_tid = tid
            cache_ok = tid == self._owner_tid
            wmi = self._wmi if (cache_ok and self._wmi is not None) else None
            if wmi is None:
                wmi = win32com.client.GetObject(r"winmgmts:\\.\root\wmi")
                if cache_ok:
                    self._wmi = wmi
            rows = list(wmi.ExecQuery("SELECT CurrentBrightness, Level FROM WmiMonitorBrightness"))
        except Exception as exc:  # noqa: BLE001
            if tid == self._owner_tid:
                self._wmi = None
            return None, None, None, f"{type(exc).__name__}: {exc}"
        if not rows:
            return None, None, None, "WMI 未返回 WmiMonitorBrightness"
        row = rows[0]
        try:
            cur = int(row.CurrentBrightness)
            levels = [int(v) for v in (row.Level or [])]
        except Exception as exc:  # noqa: BLE001
            return None, None, None, f"{type(exc).__name__}: {exc}"
        lo = min(levels) if levels else 0
        hi = max(levels) if levels else 100
        return cur, lo, hi, None

    # ---- 刷新 -----------------------------------------------------
    def refresh(self) -> dict:
        with self._io_lock:
            cur, lo, hi, err = self._brightness()
            flags = _wts_flags()
            logonui = _logonui_running()
        locked = bool((flags == WTS_SESSIONSTATE_LOCK) or logonui)
        if logonui:
            source = "logonui"
        elif flags is not None:
            source = "wts"
        else:
            source = "unknown"
        pct = None
        if cur is not None and lo is not None and hi is not None and hi > lo:
            pct = round((cur - lo) * 100.0 / (hi - lo))
        state = {
            "locked": locked,
            "lock_source": source,
            "brightness": cur,
            "brightness_min": lo,
            "brightness_max": hi,
            "brightness_pct": pct,
            "error": err,
            "checked_at": time.time(),
            "wts_flags": flags,
            "logonui": logonui,
        }
        with self._lock:
            self._state.update(state)
            self._ts = time.time()
            return dict(self._state)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def stale(self, seconds: float) -> bool:
        with self._lock:
            return (time.time() - self._ts) > seconds
