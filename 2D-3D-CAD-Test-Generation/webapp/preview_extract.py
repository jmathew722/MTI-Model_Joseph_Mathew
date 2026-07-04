"""Static-preview extraction from eDrawings files (.edrw / .eprt / .easm).

eDrawings is a proprietary SolidWorks format with no open parser and no
headless converter in this environment, so a TRUE interactive viewer is not
feasible server-side. What eDrawings files reliably contain is one or more
embedded raster previews. This module digs them out three ways, most to least
structured:

1. ZIP container: newer eDrawings files are zip archives — image members are
   read directly.
2. OLE compound file: older files use structured storage; image streams are
   found by magic-byte scan of each stream (no olefile dependency — the raw
   scan below already covers the whole file, which includes every stream).
3. Raw scan: locate every embedded PNG (\\x89PNG..IEND) and JPEG
   (\\xFFD8..\\xFFD9) in the byte stream and keep the largest decodable one.

The result is explicitly a STATIC PREVIEW — the caller must label it as such
in the UI, never present it as the interactive model.
"""
from __future__ import annotations

import io
import zipfile

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PNG_END = b"IEND\xaeB`\x82"
_JPG_MAGIC = b"\xff\xd8\xff"
_JPG_END = b"\xff\xd9"

# Ignore icons/thumbnails smaller than this many pixels.
_MIN_PIXELS = 96 * 96


def _decodable(data: bytes) -> tuple[int, bytes] | None:
    """(pixel_area, png_bytes) if PIL can decode ``data``, else None."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            im.load()
            area = im.width * im.height
            if area < _MIN_PIXELS:
                return None
            out = io.BytesIO()
            im.convert("RGB").save(out, format="PNG")
            return area, out.getvalue()
    except Exception:
        return None


def _scan_embedded_images(blob: bytes) -> list[bytes]:
    """Every PNG/JPEG byte range embedded anywhere in ``blob``."""
    found: list[bytes] = []
    # PNGs
    start = 0
    while True:
        i = blob.find(_PNG_MAGIC, start)
        if i < 0:
            break
        j = blob.find(_PNG_END, i)
        if j < 0:
            break
        found.append(blob[i:j + len(_PNG_END)])
        start = j + len(_PNG_END)
    # JPEGs
    start = 0
    while True:
        i = blob.find(_JPG_MAGIC, start)
        if i < 0:
            break
        j = blob.find(_JPG_END, i + 3)
        if j < 0:
            break
        found.append(blob[i:j + len(_JPG_END)])
        start = j + len(_JPG_END)
    return found


def extract_preview_png(blob: bytes) -> bytes | None:
    """Best (largest decodable) embedded preview of an eDrawings file as PNG,
    or None when the file embeds no usable raster preview."""
    candidates: list[bytes] = []

    # 1. ZIP container members.
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for info in zf.infolist():
                if info.file_size and info.file_size < 64 * 1024 * 1024:
                    data = zf.read(info)
                    if data[:8] == _PNG_MAGIC or data[:3] == _JPG_MAGIC:
                        candidates.append(data)
                    else:
                        candidates.extend(_scan_embedded_images(data))
    except (zipfile.BadZipFile, OSError, RuntimeError):
        pass

    # 2/3. Raw scan of the whole byte stream (covers OLE streams too).
    candidates.extend(_scan_embedded_images(blob))

    best: tuple[int, bytes] | None = None
    for data in candidates:
        dec = _decodable(data)
        if dec is not None and (best is None or dec[0] > best[0]):
            best = dec
    return best[1] if best else None
