"""Tests for webapp.preview_extract — eDrawings static-preview extraction."""
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp"))

from preview_extract import extract_preview_png  # noqa: E402


def _png_bytes(w=200, h=150, color=(200, 40, 40)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _jpg_bytes(w=300, h=200, color=(40, 40, 200)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="JPEG")
    return buf.getvalue()


class TestExtractPreview:
    def test_raw_embedded_png(self):
        blob = b"\x00" * 500 + _png_bytes() + b"\x00" * 500
        out = extract_preview_png(blob)
        assert out is not None and out[:8] == b"\x89PNG\r\n\x1a\n"

    def test_raw_embedded_jpeg(self):
        blob = b"junkheader" + _jpg_bytes() + b"trailer"
        assert extract_preview_png(blob) is not None

    def test_largest_preview_wins(self):
        from PIL import Image

        small = _png_bytes(100, 100)
        big = _png_bytes(640, 480, color=(10, 200, 10))
        out = extract_preview_png(b"x" + small + b"y" + big + b"z")
        with Image.open(io.BytesIO(out)) as im:
            assert (im.width, im.height) == (640, 480)

    def test_zip_container(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("meta/info.txt", "not an image")
            zf.writestr("previews/page0.png", _png_bytes(400, 300))
        assert extract_preview_png(buf.getvalue()) is not None

    def test_tiny_icons_rejected(self):
        blob = _png_bytes(16, 16)  # below the min-pixels threshold
        assert extract_preview_png(blob) is None

    def test_no_image_returns_none(self):
        assert extract_preview_png(b"\x00\x01\x02" * 1000) is None

    def test_corrupt_png_skipped(self):
        good = _png_bytes(200, 200)
        corrupt = b"\x89PNG\r\n\x1a\n" + b"\xff" * 40 + b"IEND\xaeB`\x82"
        out = extract_preview_png(corrupt + good)
        assert out is not None
