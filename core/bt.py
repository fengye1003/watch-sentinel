"""Windows Bluetooth 配对 / 连接状态检测（ctypes 直调 bthprops.cpl）。

不用 PowerShell：每次查询约 1-3 ms，可直接在检测循环里跑。
判据来自 BLUETOOTH_DEVICE_INFO：
  fRemembered  -> 已配对
  fConnected   -> 当前已连接（Radio 实时连接表）
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import re
import threading
import time
import unicodedata

_bth = ctypes.WinDLL("bthprops.cpl")


class BLUETOOTH_ADDRESS(ctypes.Structure):
    _fields_ = [("ullLong", ctypes.c_ulonglong)]


class SYSTEMTIME(ctypes.Structure):
    _fields_ = [
        ("wYear", wt.WORD), ("wMonth", wt.WORD), ("wDayOfWeek", wt.WORD),
        ("wDay", wt.WORD), ("wHour", wt.WORD), ("wMinute", wt.WORD),
        ("wSecond", wt.WORD), ("wMilliseconds", wt.WORD),
    ]


class BLUETOOTH_DEVICE_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("Address", BLUETOOTH_ADDRESS),
        ("ulClassofDevice", wt.ULONG),
        ("fConnected", wt.BOOL),
        ("fRemembered", wt.BOOL),
        ("fAuthenticated", wt.BOOL),
        ("stLastSeen", SYSTEMTIME),
        ("stLastUsed", SYSTEMTIME),
        ("szName", wt.WCHAR * 248),
    ]


class BLUETOOTH_FIND_RADIO_PARAMS(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD)]


class BLUETOOTH_DEVICE_SEARCH_PARAMS(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("fReturnAuthenticated", wt.BOOL),
        ("fReturnRemembered", wt.BOOL),
        ("fReturnUnknown", wt.BOOL),
        ("fReturnConnected", wt.BOOL),
        ("fIssueInquiry", wt.BOOL),
        ("cTimeoutMultiplier", ctypes.c_ubyte),
        ("hRadio", wt.HANDLE),
    ]


_bth.BluetoothFindFirstRadio.argtypes = [ctypes.POINTER(BLUETOOTH_FIND_RADIO_PARAMS), ctypes.POINTER(wt.HANDLE)]
_bth.BluetoothFindFirstRadio.restype = wt.HANDLE
_bth.BluetoothFindNextRadio.argtypes = [wt.HANDLE, ctypes.POINTER(wt.HANDLE)]
_bth.BluetoothFindNextRadio.restype = wt.BOOL
_bth.BluetoothFindRadioClose.argtypes = [wt.HANDLE]
_bth.BluetoothFindRadioClose.restype = wt.BOOL
_bth.BluetoothFindFirstDevice.argtypes = [
    ctypes.POINTER(BLUETOOTH_DEVICE_SEARCH_PARAMS), ctypes.POINTER(BLUETOOTH_DEVICE_INFO)
]
_bth.BluetoothFindFirstDevice.restype = wt.HANDLE
_bth.BluetoothFindNextDevice.argtypes = [wt.HANDLE, ctypes.POINTER(BLUETOOTH_DEVICE_INFO)]
_bth.BluetoothFindNextDevice.restype = wt.BOOL
_bth.BluetoothFindDeviceClose.argtypes = [wt.HANDLE]
_bth.BluetoothFindDeviceClose.restype = wt.BOOL
_bth.BluetoothGetDeviceInfo.argtypes = [wt.HANDLE, ctypes.POINTER(BLUETOOTH_DEVICE_INFO)]
_bth.BluetoothGetDeviceInfo.restype = wt.DWORD

NAME_SEPARATORS = re.compile(r"[\s.．。·・•\-_—–]+")


def normalize_name(name: str) -> str:
    """把「Pixel.hello」「Pixel·hello」「Pixel hello」归一成同一个 key。"""
    text = unicodedata.normalize("NFKC", name or "")
    return NAME_SEPARATORS.sub("", text).casefold()


def _enumerate() -> list[dict]:
    devices: list[dict] = []
    frp = BLUETOOTH_FIND_RADIO_PARAMS(ctypes.sizeof(BLUETOOTH_FIND_RADIO_PARAMS))
    hradio = wt.HANDLE()
    find_radio = _bth.BluetoothFindFirstRadio(ctypes.byref(frp), ctypes.byref(hradio))
    if not find_radio:
        code = ctypes.GetLastError()
        if code == 259:  # ERROR_NO_MORE_ITEMS -> 没有蓝牙适配器
            raise OSError("未找到可用的蓝牙适配器")
        raise OSError(f"BluetoothFindFirstRadio 失败 (code={code})")
    try:
        while True:
            params = BLUETOOTH_DEVICE_SEARCH_PARAMS()
            params.dwSize = ctypes.sizeof(BLUETOOTH_DEVICE_SEARCH_PARAMS)
            params.fReturnAuthenticated = True
            params.fReturnRemembered = True
            params.fReturnUnknown = True
            params.fReturnConnected = True
            params.fIssueInquiry = False
            params.cTimeoutMultiplier = 0
            params.hRadio = hradio

            info = BLUETOOTH_DEVICE_INFO()
            info.dwSize = ctypes.sizeof(BLUETOOTH_DEVICE_INFO)
            find_dev = _bth.BluetoothFindFirstDevice(ctypes.byref(params), ctypes.byref(info))
            if find_dev:
                try:
                    while True:
                        live = BLUETOOTH_DEVICE_INFO()
                        ctypes.memmove(ctypes.byref(live), ctypes.byref(info), ctypes.sizeof(info))
                        if _bth.BluetoothGetDeviceInfo(hradio, ctypes.byref(live)) == 0:
                            info = live
                        devices.append(
                            {
                                "name": info.szName,
                                "addr": f"{info.Address.ullLong:012X}",
                                "connected": bool(info.fConnected),
                                "paired": bool(info.fRemembered),
                                "authenticated": bool(info.fAuthenticated),
                            }
                        )
                        if not _bth.BluetoothFindNextDevice(find_dev, ctypes.byref(info)):
                            break
                finally:
                    _bth.BluetoothFindDeviceClose(find_dev)
            if not _bth.BluetoothFindNextRadio(find_radio, ctypes.byref(hradio)):
                break
    finally:
        _bth.BluetoothFindRadioClose(find_radio)
    return devices


class BluetoothMonitor:
    """带缓存的蓝牙状态查询器；refresh() 可被任意线程调用。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._devices: list[dict] = []
        self._error: str | None = None
        self._ts = 0.0

    def refresh(self) -> None:
        try:
            devs = _enumerate()
            err = None
        except Exception as exc:  # noqa: BLE001 - 设备状态不该杀死哨兵
            devs, err = [], f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._devices, self._error, self._ts = devs, err, time.time()

    def snapshot(self, target: str) -> dict:
        with self._lock:
            devices = [dict(d) for d in self._devices]
            err, ts = self._error, self._ts
        key = normalize_name(target)
        match = next((d for d in devices if normalize_name(d["name"]) == key), None)
        if match is None and key:
            match = next((d for d in devices if key in normalize_name(d["name"])), None)
        return {
            "devices": sorted(devices, key=lambda d: (not d["connected"], d["name"])),
            "target": target,
            "matched": match,
            "found": match is not None,
            "connected": bool(match and match["connected"]),
            "paired": bool(match and match["paired"]),
            "error": err,
            "checked_at": ts,
        }
