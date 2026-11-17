"""The Pocket Option SSID, kept with Windows DPAPI (only this Windows user can read it). In the normal folder only a DEMO
session is accepted: an SSID that says isDemo 0, or that does not say it at all, is refused. The REAL folder
(PocketTylReal, 4 Nov 2026) takes only a session that says isDemo 0."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re


def protect(data: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise ValueError("El guardado protegido requiere Windows.")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    api = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CryptProtectData.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob),
                                    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    api.CryptProtectData.restype = wintypes.BOOL
    api.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                                      ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    api.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        ok = api.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        ok = api.CryptProtectData(ctypes.byref(source), "Pocket Tyl SSID", None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise ValueError("Windows no pudo proteger o recuperar el SSID.")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def demo_check(ssid: str) -> str | None:
    """None when the SSID is a demo session; otherwise the reason it is refused."""
    ssid = (ssid or "").strip()
    if len(ssid) < 20:
        return "Pega el SSID completo de tu cuenta DEMO de Pocket Option."
    flag = re.search(r'"?isDemo"?\s*:\s*(\d)', ssid)
    if flag is None:
        return "No se puede confirmar que sea una cuenta DEMO (falta isDemo): por seguridad no se guarda."
    if flag.group(1) != "1":
        return "Ese SSID es de una cuenta REAL. Por ahora solo se aceptan cuentas DEMO."
    return None


def real_check(ssid: str) -> str | None:
    """None when the SSID is a REAL-account session (user, 4 Nov 2026: the GPT bot on his real account, in its own folder
    PocketTylReal, started by him); otherwise the reason it is refused."""
    ssid = (ssid or "").strip()
    if len(ssid) < 20:
        return "No llegó la sesión completa de la cuenta REAL."
    flag = re.search(r'"?isDemo"?\s*:\s*(\d)', ssid)
    if flag is None:
        return "No se puede confirmar que sea la cuenta REAL (falta isDemo): por seguridad no se guarda."
    if flag.group(1) != "0":
        return "Esa sesión es de la cuenta DEMO; para la cuenta real abre la sala REAL."
    return None


def save_ssid(root: Path, ssid: str, real: bool = False, extra: dict | None = None) -> None:
    reason = (real_check if real else demo_check)(ssid)
    if reason:
        raise ValueError(reason)
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "ssid.bin.tmp"
    tmp.write_bytes(protect(ssid.strip().encode("utf-8")))
    tmp.replace(root / "ssid.bin")
    flag = root / "ssid.json"
    flag.write_text(json.dumps(dict(demo=not real, **(extra or {}))), encoding="utf-8")


def ssid_info(root: Path) -> dict:
    """What was saved next to the SSID: demo flag, and since 4 Nov 2026 the socket URL and browser user agent it came from."""
    try:
        return json.loads((root / "ssid.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def has_ssid(root: Path) -> bool:
    return (root / "ssid.bin").is_file()


def forget_ssid(root: Path) -> None:
    for name in ("ssid.bin", "ssid.json"):
        try:
            (root / name).unlink()
        except FileNotFoundError:
            pass
