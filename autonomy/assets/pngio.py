"""Reading and writing the image bytes the image pipeline handles, with the standard library only.

The checks need three facts about a file without a picture library: what type it really is (the
first bytes, not the label), how big it is in pixels, and, for the fake provider, a few text notes
inside it. PNG and JPEG sizes are read from their headers; `make_png` writes a plain PNG (a vertical
gradient) so the fake provider can return real, decodable files.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PIXELS = 100_000_000
"""A size field larger than this is not a picture a brand would send; `make_png` refuses it."""


@dataclass(frozen=True)
class ImageInfo:
    """What the bytes say about themselves. `text` holds PNG tEXt and iTXt notes (empty for other types)."""

    mime_type: str
    width: int
    height: int
    text: dict[str, str] = field(default_factory=dict)


def sniff_mime(data: bytes) -> str | None:
    """The type the first bytes show: image/png, image/jpeg, image/gif, image/webp, or None."""
    if data.startswith(PNG_SIGNATURE):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def read_info(data: bytes) -> ImageInfo | None:
    """Width, height and notes of a PNG or JPEG; None when the bytes are neither or are damaged."""
    mime = sniff_mime(data)
    try:
        if mime == "image/png":
            return _read_png(data)
        if mime == "image/jpeg":
            return _read_jpeg(data)
    except (struct.error, ValueError, zlib.error):
        return None
    return None


def _read_png(data: bytes) -> ImageInfo:
    pos = len(PNG_SIGNATURE)
    width = height = 0
    text: dict[str, str] = {}
    seen_end = False
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        if len(body) != length or zlib.crc32(kind + body) != crc:
            raise ValueError("damaged PNG chunk")
        if kind == b"IHDR":
            width, height = struct.unpack(">II", body[:8])
        elif kind == b"tEXt":
            key, _, value = body.partition(b"\x00")
            text[key.decode("latin-1")] = value.decode("latin-1")
        elif kind == b"iTXt":
            note_key, note = _read_itxt(body)
            text[note_key] = note
        elif kind == b"IEND":
            seen_end = True
            break
        pos += 12 + length
    if not seen_end or width <= 0 or height <= 0:
        raise ValueError("incomplete PNG")
    return ImageInfo("image/png", width, height, text)


def _read_itxt(body: bytes) -> tuple[str, str]:
    """An iTXt chunk: keyword, compression flag and method, language, translated keyword, UTF-8 text."""
    key, _, rest = body.partition(b"\x00")
    if len(rest) < 2:
        raise ValueError("damaged iTXt chunk")
    compressed, rest = rest[0], rest[2:]
    _language, _, rest = rest.partition(b"\x00")
    _translated, _, value = rest.partition(b"\x00")
    if compressed:
        value = zlib.decompress(value)
    return key.decode("latin-1"), value.decode("utf-8")


def _read_jpeg(data: bytes) -> ImageInfo:
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            raise ValueError("bad JPEG marker")
        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        (length,) = struct.unpack(">H", data[pos + 2 : pos + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[pos + 5 : pos + 9])
            if width <= 0 or height <= 0:
                raise ValueError("bad JPEG size")
            return ImageInfo("image/jpeg", width, height)
        pos += 2 + length
    raise ValueError("no JPEG size found")


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def make_png(
    width: int,
    height: int,
    top: tuple[int, int, int],
    bottom: tuple[int, int, int] | None = None,
    *,
    text: dict[str, str] | None = None,
) -> bytes:
    """A valid RGB PNG fading from the `top` colour to the `bottom` one; `text` becomes tEXt notes.

    The same arguments always give the same bytes. Keys must be Latin-1, 1 to 79 characters. A value
    that Latin-1 can hold goes in a tEXt chunk; any other (say, a Devanagari item id) in a UTF-8 iTXt one.
    """
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        raise ValueError(f"image size {width}x{height} is out of range")
    end = bottom if bottom is not None else top
    rows: list[bytes] = []
    for y in range(height):
        t = y / (height - 1) if height > 1 else 0.0
        colour = bytes(round(a + (b - a) * t) for a, b in zip(top, end, strict=True))
        rows.append(b"\x00" + colour * width)
    parts = [PNG_SIGNATURE, _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))]
    for key in sorted(text or {}):
        value = (text or {})[key]
        if not 1 <= len(key) <= 79:
            raise ValueError(f"PNG note key {key!r} must be 1 to 79 characters")
        try:
            parts.append(_chunk(b"tEXt", key.encode("latin-1") + b"\x00" + value.encode("latin-1")))
        except UnicodeEncodeError:
            # keyword, no compression (flag 0, method 0), empty language and translated keyword, UTF-8 text
            itxt = key.encode("latin-1") + b"\x00\x00\x00\x00\x00" + value.encode("utf-8")
            parts.append(_chunk(b"iTXt", itxt))
    parts.append(_chunk(b"IDAT", zlib.compress(b"".join(rows), 9)))
    parts.append(_chunk(b"IEND", b""))
    return b"".join(parts)
