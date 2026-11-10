"""Thumbnail generation; the PNG codec lives in studio/png.py.

A 1024x1024 source becomes a ~256x256 PNG (roughly 130 KB instead of 1.5 MB),
which is what makes a gallery with tens of thousands of images usable.
"""
from __future__ import annotations

import threading
from pathlib import Path

from . import png

MAX_EDGE = 256
_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def make_thumb(data: bytes, edge: int = MAX_EDGE) -> bytes | None:
    """Downscale a PNG with nearest-neighbour sampling. None if unsupported."""
    img = png.decode(data)
    if img is None:
        return None
    if max(img.width, img.height) <= edge:
        return data
    scale = edge / max(img.width, img.height)
    tw = max(1, round(img.width * scale))
    th = max(1, round(img.height * scale))
    return png.encode(png.resize(img, tw, th))


def cache_path(cfg: dict, rel: str) -> Path:
    return cfg["local"] / "thumbs" / rel


def ensure(cfg: dict, rel: str) -> Path | None:
    """Return a cached thumbnail, creating it once. None = serve original."""
    src = cfg["local"] / rel
    if not src.is_file() or src.suffix.lower() != ".png":
        return None
    dst = cache_path(cfg, rel)
    if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    key = str(dst)
    with _GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    with lock:
        if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
            return dst
        try:
            thumb = make_thumb(src.read_bytes())
        except Exception:
            return None
        if thumb is None:
            return None
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        tmp.write_bytes(thumb)
        tmp.replace(dst)
        return dst
