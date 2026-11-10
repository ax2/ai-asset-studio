"""Encrypted at-rest storage for the API key.

The credential file lives OUTSIDE the repository (under %APPDATA% on Windows,
or ~/.config on other platforms) so it can never be committed by accident.
On Windows the key is sealed with DPAPI (CryptProtectData), which binds the
ciphertext to the current Windows account - copying the file to another
machine or another user yields nothing. On other platforms we fall back to a
0600 file and mark the cipher honestly as "plain".

Only a masked hint is kept in cleartext; the key itself is never logged,
never returned by any API, and never written into the project directory.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from datetime import datetime
from pathlib import Path

_ENTROPY = b"ai-asset-studio/credentials/v1"
_CACHE: dict = {"loaded": False, "key": "", "doc": None}

# ---------------------------------------------------------------------------
# Windows DPAPI through ctypes (standard library only)
# ---------------------------------------------------------------------------
_HAS_DPAPI = False
if sys.platform == "win32":
    try:
        import ctypes
        from ctypes import wintypes

        class _BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_byte))]

        _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _BLOBP = ctypes.POINTER(_BLOB)

        _crypt32.CryptProtectData.argtypes = [
            _BLOBP, wintypes.LPCWSTR, _BLOBP, ctypes.c_void_p,
            ctypes.c_void_p, wintypes.DWORD, _BLOBP]
        _crypt32.CryptProtectData.restype = wintypes.BOOL
        _crypt32.CryptUnprotectData.argtypes = [
            _BLOBP, ctypes.POINTER(wintypes.LPCWSTR), _BLOBP, ctypes.c_void_p,
            ctypes.c_void_p, wintypes.DWORD, _BLOBP]
        _crypt32.CryptUnprotectData.restype = wintypes.BOOL
        _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        _kernel32.LocalFree.restype = wintypes.HLOCAL
        _HAS_DPAPI = True
    except Exception:  # pragma: no cover - non-standard Windows runtimes
        _HAS_DPAPI = False


def _make_blob(data: bytes):
    buf = (ctypes.c_byte * max(1, len(data))).from_buffer_copy(data)
    blob = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    return blob, buf  # keep buf alive for the duration of the call


def _protect(data: bytes) -> bytes:
    src, src_buf = _make_blob(data)
    ent, ent_buf = _make_blob(_ENTROPY)
    out = _BLOB()
    ok = _crypt32.CryptProtectData(ctypes.byref(src), None, ctypes.byref(ent),
                                   None, None, 0x01, ctypes.byref(out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(out.pbData, wintypes.HLOCAL))


def _unprotect(data: bytes) -> bytes:
    src, src_buf = _make_blob(data)
    ent, ent_buf = _make_blob(_ENTROPY)
    out = _BLOB()
    ok = _crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent),
                                     None, None, 0x01, ctypes.byref(out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(out.pbData, wintypes.HLOCAL))


# ---------------------------------------------------------------------------
# File handling
# ---------------------------------------------------------------------------
def path() -> Path:
    base = os.environ.get("APPDATA")
    root = Path(base) / "ai-asset-studio" if base else Path.home() / ".config" / "ai-asset-studio"
    return root / "credentials.bin"


def _hint(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return key[:3] + "…" + key[-4:]


def _info_unlocked() -> dict:
    doc = _CACHE.get("doc") or {}
    return {
        "persisted": bool(_CACHE.get("key")),
        "hint": doc.get("hint", ""),
        "path": str(path()),
        "cipher": "dpapi" if _HAS_DPAPI else "plain",
        "saved_at": doc.get("saved_at", ""),
    }


def info() -> dict:
    load()
    return _info_unlocked()


def load() -> str:
    """Return the persisted key, or '' if missing / undecryptable."""
    if _CACHE["loaded"]:
        return _CACHE["key"]
    _CACHE["loaded"] = True
    p = path()
    if not p.is_file():
        return ""
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        payload = base64.b64decode(doc["data"])
        if doc.get("cipher") == "dpapi":
            if not _HAS_DPAPI:
                return ""
            raw = _unprotect(payload)
        else:
            raw = payload
        key = raw.decode("utf-8").strip()
        _CACHE["key"] = key
        _CACHE["doc"] = doc
        return key
    except Exception:
        # Corrupt file, different Windows account, or changed entropy: fail
        # closed and let the user paste a key again. Never crash startup.
        return ""


def save(key: str) -> dict:
    key = (key or "").strip()
    if not key:
        raise ValueError("API key 为空，未保存。")
    raw = key.encode("utf-8")
    if _HAS_DPAPI:
        payload, cipher = _protect(raw), "dpapi"
    else:
        payload, cipher = raw, "plain"
    doc = {
        "v": 1,
        "cipher": cipher,
        "data": base64.b64encode(payload).decode("ascii"),
        "hint": _hint(key),
        "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    _CACHE.update({"loaded": True, "key": key, "doc": doc})
    return _info_unlocked()


def clear() -> dict:
    p = path()
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass
    _CACHE.update({"loaded": True, "key": "", "doc": None})
    return _info_unlocked()
