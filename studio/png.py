"""Minimal PNG codec + pixel operations, standard library only.

Supports 8-bit RGB / RGBA, non-interlaced PNG (everything gpt-image emits).
This is the single source of truth for decoding and encoding; thumbs.py and
sprites.py both build on it. Keeping frame canvases small (64-256 px) makes
per-pixel Python loops fast enough for real sprite-sheet production.
"""
from __future__ import annotations

import math
import struct
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"


class Image:
    __slots__ = ("width", "height", "bpp", "rows")

    def __init__(self, width: int, height: int, bpp: int, rows=None):
        self.width, self.height, self.bpp = width, height, bpp
        self.rows = rows if rows is not None else [
            bytearray(width * bpp) for _ in range(height)]

    def pixel(self, x: int, y: int) -> tuple:
        i = x * self.bpp
        return tuple(self.rows[y][i:i + self.bpp])


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode(data: bytes) -> Image | None:
    if not data.startswith(SIGNATURE):
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
    return Image(width, height, bpp, rows)


def _chunk(ctype: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + ctype + payload
            + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF))


def encode(img: Image) -> bytes:
    bpp = img.bpp
    raw = b"".join(b"\x00" + bytes(r) for r in img.rows)
    ihdr = struct.pack(">IIBBBBB", img.width, img.height, 8,
                       2 if bpp == 3 else 6, 0, 0, 0)
    return (SIGNATURE + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, 6))
            + _chunk(b"IEND", b""))


def make(width: int, height: int, bpp: int = 4, fill=(0, 0, 0, 0)) -> Image:
    img = Image(width, height, bpp)
    if any(fill):
        px = bytes(fill[:bpp])
        for row in img.rows:
            row[:] = px * width
    return img


def to_rgba(img: Image) -> Image:
    if img.bpp == 4:
        return img
    out = Image(img.width, img.height, 4)
    for y, row in enumerate(img.rows):
        dst = out.rows[y]
        for x in range(img.width):
            s = x * 3
            d = x * 4
            dst[d:d + 3] = row[s:s + 3]
            dst[d + 3] = 255
    return out


def resize(img: Image, width: int, height: int) -> Image:
    """Nearest-neighbour resample (keeps hard edges, ideal for pixel sprites)."""
    out = Image(width, height, img.bpp)
    for y in range(height):
        sy = min(img.height - 1, y * img.height // height)
        src, dst = img.rows[sy], out.rows[y]
        for x in range(width):
            sx = min(img.width - 1, x * img.width // width) * img.bpp
            dst[x * img.bpp:(x + 1) * img.bpp] = src[sx:sx + img.bpp]
    return out


def corner_color(img: Image) -> tuple:
    """Average of the four corners - a good guess at a flat AI background."""
    pts = [(0, 0), (img.width - 1, 0), (0, img.height - 1),
           (img.width - 1, img.height - 1)]
    acc = [0, 0, 0]
    for x, y in pts:
        p = img.pixel(x, y)
        for i in range(3):
            acc[i] += p[i]
    return tuple(v // len(pts) for v in acc)


def chroma_key(img: Image, color=None, tolerance: int = 38) -> Image:
    """Turn a near-uniform background transparent. Returns an RGBA image."""
    src = to_rgba(img)
    if color is None:
        color = corner_color(src)
    tol2 = tolerance * tolerance * 3
    out = Image(src.width, src.height, 4)
    for y in range(src.height):
        s, d = src.rows[y], out.rows[y]
        for x in range(src.width):
            i = x * 4
            dr, dg, db = s[i] - color[0], s[i + 1] - color[1], s[i + 2] - color[2]
            if dr * dr + dg * dg + db * db <= tol2:
                d[i:i + 4] = b"\x00\x00\x00\x00"
            else:
                d[i:i + 4] = s[i:i + 4]
                d[i + 3] = 255
    return out


def crop(img: Image, x: int, y: int, width: int, height: int) -> Image:
    out = make(width, height, img.bpp)
    for row in range(height):
        sy = y + row
        if 0 <= sy < img.height:
            x0, x1 = max(0, x), min(img.width, x + width)
            if x0 < x1:
                s = x0 * img.bpp
                e = x1 * img.bpp
                out.rows[row][(x0 - x) * img.bpp:(x1 - x) * img.bpp] = img.rows[sy][s:e]
    return out


def split_grid(img: Image, cols: int, rows: int) -> list:
    cw, ch = img.width // cols, img.height // rows
    out = []
    for r in range(rows):
        for c in range(cols):
            out.append(crop(img, c * cw, r * ch, cw, ch))
    return out


def sheet(frames: list, cols: int, cell_w: int, cell_h: int) -> Image:
    rows = max(1, math.ceil(len(frames) / cols))
    out = make(cols * cell_w, rows * cell_h, 4)
    for i, frame in enumerate(frames):
        c, r = i % cols, i // cols
        f = frame if (frame.width == cell_w and frame.height == cell_h) \
            else resize(frame, cell_w, cell_h)
        # The sheet is always RGBA. Copying an RGB row into a 4-byte-per-pixel
        # slot would shift every channel and leave alpha=0 (invisible sheet).
        f = to_rgba(f)
        for y in range(cell_h):
            out.rows[r * cell_h + y][c * cell_w * 4:(c + 1) * cell_w * 4] = f.rows[y][:]
    return out


def transform(img: Image, width: int, height: int, *, dx=0, dy=0,
              sx=1.0, sy=1.0, flip_h=False, wave=0.0) -> Image:
    """Render one animation frame by inverse-mapping the source sprite.

    dx/dy: offset, sx/sy: squash & stretch, flip_h: mirror, wave: a gentle
    horizontal sway that peaks at the head and vanishes at the feet.
    """
    src = to_rgba(img)
    out = make(width, height, 4)
    cx, cy = src.width / 2.0, src.height / 2.0
    for oy in range(height):
        sway = wave * math.sin(math.pi * (oy / max(1, height)))
        iy = int(round((oy - dy - height / 2.0) / sy + cy))
        if not (0 <= iy < src.height):
            continue
        srow, drow = src.rows[iy], out.rows[oy]
        for ox in range(width):
            ix = int(round((ox - dx - sway - width / 2.0) / sx + cx))
            if flip_h:
                ix = src.width - 1 - ix
            if not (0 <= ix < src.width):
                continue
            s = ix * 4
            if srow[s + 3]:
                d = ox * 4
                drow[d:d + 4] = srow[s:s + 4]
    return out
