"""Thumbnail generation using only the standard library.

Handles 8-bit RGB / RGBA, non-interlaced PNG - the format gpt-image and most
image tools emit. Thumbnails are cached under data/thumbs/ mirroring the
original path. Any unsupported format returns None so callers can fall back
to serving the original file.

A 1024x1024 source becomes a ~256x256 PNG: roughly 40-80 KB instead of 1.5 MB,
which is what makes a gallery with tens of thousands of images usable.
"""
from __future__ import annotations

import struct
import threading
import zlib
from pathlib import Path

MAX_EDGE = 256
_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _decode(data: bytes):
    """Return (width, height, bytes_per_pixel, rows) or None."""
    if not data.startswith(_SIGNATURE):
        return None
    pos, idat, ihdr = 8, [], None
    size = len(data)
    while pos + 8 <= size:
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        if pos + 12 + length > size:
            return None
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            if len(chunk) < 13:
                return None
            ihdr = struct.unpack(">IIBBBBB", chunk[:13])
        elif ctype == b"IDAT":
            idat.append(chunk)
        elif ctype == b"IEND":
            break
    if not ihdr or not idat:
        return None
    width, height, depth, color, _comp, _filt, interlace = ihdr
    if depth != 8 or color not in (2, 6) or interlace != 0:
        return None
    if not width or not height or width * height > 64_000_000:
        return None
    bpp = 3 if color == 2 else 4
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error:
        return None
    stride = width * bpp
    if len(raw) < (stride + 1) * height:
        return None

    rows, prev, off = [], bytearray(stride), 0
    for _ in range(height):
        ft = raw[off]
        off += 1
        line = bytearray(raw[off:off + stride])
        off += stride
        if ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                c = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(a, prev[i], c)) & 255
        elif ft != 0:
            return None
        rows.append(line)
        prev = line
    return width, height, bpp, rows


def _chunk(ctype: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + ctype + payload
            + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF))


def _encode(width: int, height: int, bpp: int, rows: list) -> bytes:
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8,
                       2 if bpp == 3 else 6, 0, 0, 0)
    return (_SIGNATURE + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, 6))
            + _chunk(b"IEND", b""))


def make_thumb(data: bytes, edge: int = MAX_EDGE) -> bytes | None:
    """Downscale a PNG with nearest-neighbour sampling. None if unsupported."""
    decoded = _decode(data)
    if decoded is None:
        return None
    width, height, bpp, rows = decoded
    if max(width, height) <= edge:
        return data
    tw = max(1, round(width * edge / max(width, height)))
    th = max(1, round(height * edge / max(width, height)))
    out = []
    for ty in range(th):
        src = rows[min(height - 1, ty * height // th)]
        line = bytearray()
        for tx in range(tw):
            sx = min(width - 1, tx * width // tw) * bpp
            line.extend(src[sx:sx + bpp])
        out.append(line)
    return _encode(tw, th, bpp, out)


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
        except (OSError, ValueError, struct.error, zlib.error):
            return None
        if thumb is None:
            return None
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        tmp.write_bytes(thumb)
        tmp.replace(dst)
        return dst
