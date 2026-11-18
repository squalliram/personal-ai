"""The owner's own photos: checked, cleaned and re-encoded by the Command Centre, then drafted by the daily run.

Only the Command Centre decodes images (Pillow lives in its vendor/ directory); the daily run just copies the
cleaned JPEGs into a draft folder, so it needs no image library.
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
from datetime import datetime
from pathlib import Path

MAX_PHOTOS = 10
MAX_PHOTO_BYTES = 12 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_NOTE = 300
MAX_PIXELS = 50_000_000
PORTRAIT, SQUARE = (1080, 1350), (1080, 1080)
THUMB_WIDTH = 540
UPLOAD_ID = re.compile(r"[0-9a-f]{16}")
HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1", b"avif"}
FOLDER = "uploads"
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
TABLE = """
CREATE TABLE IF NOT EXISTS uploads (
    upload_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    note TEXT NOT NULL,
    photos INTEGER NOT NULL CHECK (photos BETWEEN 1 AND 10),
    status TEXT NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'used')),
    draft_id TEXT,
    used_at TEXT,
    reel INTEGER NOT NULL DEFAULT 0,
    music TEXT
)
"""


class PhotoError(ValueError):
    """A photo or note the owner should fix; `code` is the API error code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def available() -> bool:
    try:
        import PIL.Image  # noqa: F401
        import PIL.ImageOps  # noqa: F401
    except ImportError:
        return False
    return True


def kind(data: bytes) -> str | None:
    """The file type from its magic bytes, never from its name or declared type."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[4:8] == b"ftyp" and data[8:12] in HEIF_BRANDS:
        return "heif"
    return None


def clean_note(value) -> str:
    note = " ".join(value.split()) if isinstance(value, str) else ""
    if not note or len(note) > MAX_NOTE or CONTROL.search(note):
        raise PhotoError("invalid_note")
    return note


def _decode(data: bytes):
    """A decoded RGB image in sRGB, upright, with every bit of metadata left behind."""
    from PIL import Image, ImageCms, ImageOps

    found = kind(data)
    if found == "heif":
        raise PhotoError("heic_unsupported")
    if found is None:
        raise PhotoError("unsupported_photo")
    try:
        image = Image.open(io.BytesIO(data), formats=["JPEG" if found == "jpeg" else "PNG"])
        if image.width * image.height > MAX_PIXELS:
            raise PhotoError("photo_too_large")
        if found == "jpeg":
            image.draft("RGB", (PORTRAIT[0] * 2, PORTRAIT[1] * 2))
        image.load()
        image = ImageOps.exif_transpose(image)
        icc = image.info.get("icc_profile")
        if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            image = Image.new("RGB", rgba.size, (255, 255, 255))
            image.paste(rgba, mask=rgba.getchannel("A"))
        else:
            image = image.convert("RGB")
    except PhotoError:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise PhotoError("unreadable_photo") from None
    if icc:
        try:
            source = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            image = ImageCms.profileToProfile(image, source, ImageCms.createProfile("sRGB"), outputMode="RGB")
        except (ImageCms.PyCMSError, OSError, ValueError, TypeError):
            pass  # Keep the colours as stored rather than refuse the photo.
    return image


def _jpeg(image, quality: int) -> bytes:
    image = image.copy()
    image.info = {}  # Pillow would otherwise copy a source JPEG's comment (and other info) into the new file
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue()


def prepare(photos: list[bytes]) -> list[tuple[bytes, bytes]]:
    """(slide, thumbnail) JPEGs per photo: 4:5 if the first photo is portrait, else 1:1, centre-cropped."""
    if not 1 <= len(photos) <= MAX_PHOTOS:
        raise PhotoError("photo_count")
    if any(len(data) > MAX_PHOTO_BYTES for data in photos):
        raise PhotoError("photo_too_large")
    if any(kind(data) not in ("jpeg", "png", "heif") for data in photos):
        raise PhotoError("unsupported_photo")
    from PIL import Image, ImageOps

    size, prepared = None, []
    for data in photos:
        image = _decode(data)
        size = size or (PORTRAIT if image.height > image.width else SQUARE)
        slide = ImageOps.fit(image, size, Image.Resampling.LANCZOS)
        thumb = slide.resize((THUMB_WIDTH, THUMB_WIDTH * size[1] // size[0]), Image.Resampling.LANCZOS)
        prepared.append((_jpeg(slide, 90), _jpeg(thumb, 82)))
    return prepared


def _write(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o600), "wb") as stream:
        stream.write(data)


def folder(media_dir: Path, upload_id: str) -> Path:
    if not UPLOAD_ID.fullmatch(upload_id):
        raise ValueError("invalid upload id")
    return media_dir / FOLDER / upload_id


def private_root(media_dir: Path, name: str) -> Path:
    """media_dir/name, made private and refused if either is a symlink."""
    root = media_dir / name
    media_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in (media_dir, root):
        path.mkdir(mode=0o700, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise OSError("Upload directory is not a private directory")
    return root


def save(
    conn: sqlite3.Connection,
    media_dir: Path,
    note: str,
    prepared: list[tuple[bytes, bytes]],
    now: datetime,
    *,
    reel: bool = False,
    music: dict | None = None,
):
    """Store the cleaned photos, then the waiting row (`reel`: make them a reel, with `music` if chosen)."""
    root = private_root(media_dir, FOLDER)
    upload_id = secrets.token_hex(8)
    staging, final = root / f".{upload_id}", root / upload_id
    staging.mkdir(mode=0o700)
    try:
        for number, (slide, thumb) in enumerate(prepared, 1):
            _write(staging / f"photo-{number}.jpg", slide)
            _write(staging / f"thumb-{number}.jpg", thumb)
        staging.rename(final)
        with conn:
            conn.execute(TABLE)
            add_columns(conn)
            conn.execute(
                "INSERT INTO uploads(upload_id,created_at,note,photos,reel,music) VALUES(?,?,?,?,?,?)",
                (
                    upload_id,
                    now.isoformat(timespec="seconds"),
                    note,
                    len(prepared),
                    int(reel),
                    json.dumps(music) if reel and music else None,
                ),
            )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(final, ignore_errors=True)
        raise
    return upload_id


def delete(conn: sqlite3.Connection, media_dir: Path, upload_id: str) -> bool:
    """Remove a waiting upload and its photos. False if it is unknown or already drafted."""
    with conn:
        changed = conn.execute("DELETE FROM uploads WHERE upload_id=? AND status='waiting'", (upload_id,)).rowcount
    if changed:
        shutil.rmtree(folder(media_dir, upload_id), ignore_errors=True)
    return bool(changed)


def add_columns(conn: sqlite3.Connection, table: str = "uploads") -> None:
    """Add the reel and music choices to a table made before reels could be rendered."""
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if "music" not in have:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN music TEXT")
    if table == "uploads" and "reel" not in have:
        conn.execute("ALTER TABLE uploads ADD COLUMN reel INTEGER NOT NULL DEFAULT 0")


def oldest(conn: sqlite3.Connection):
    with conn:
        add_columns(conn)
    return conn.execute(
        "SELECT upload_id, created_at, note, photos, reel, music FROM uploads WHERE status='waiting' "
        "ORDER BY created_at, upload_id"
    ).fetchone()


def is_waiting(conn: sqlite3.Connection, upload_id: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM uploads WHERE upload_id=? AND status='waiting'", (upload_id,)).fetchone()
        is not None
    )


def read_photo(media_dir: Path, upload_id: str, name: str) -> bytes:
    """One stored JPEG, refusing symlinks and anything that isn't a JPEG file."""
    path = folder(media_dir, upload_id) / name
    if path.parent.is_symlink():
        raise OSError("upload folder is a symlink")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("not a file")
        data = stream.read(MAX_PHOTO_BYTES + 1)
    if kind(data) != "jpeg" or len(data) > MAX_PHOTO_BYTES:
        raise OSError("not a stored JPEG")
    return data
