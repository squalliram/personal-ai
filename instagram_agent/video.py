"""Check an owner clip against Meta's Reels video spec before it is sent.

A small MP4/MOV box reader checks what the file's headers state; ffprobe (when the host has it) adds what only a
decoder can see. Anything neither can read is left to Meta's container `status_code`.
"""

from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path

from .host_probe import find_tool

MIN_SECONDS, MAX_SECONDS = 3, 90  # Meta allows 15 min; the owner chose 90 s
MAX_BYTES = 300 * 1024 * 1024
MAX_WIDTH = 1920
MIN_FPS, MAX_FPS = 23, 60
MAX_BITRATE = 25_000_000 + 128_000  # 25 Mbps video plus 128 kbps audio
MAX_SAMPLE_RATE = 48_000
VIDEO_CODECS = {b"avc1": "h264", b"avc3": "h264", b"hvc1": "hevc", b"hev1": "hevc"}
AUDIO_CODECS = {b"mp4a": "aac"}
BRANDS_MOV = {b"qt  "}
MAX_MOOV = 32 * 1024 * 1024
CONTAINERS = {b"trak", b"mdia", b"minf", b"stbl", b"edts"}


def _boxes(data: bytes, start: int = 0, end: int | None = None):
    """(type, payload, ) for each box in data[start:end]; stops at a malformed box."""
    end = len(data) if end is None else end
    at = start
    while at + 8 <= end:
        size, kind = struct.unpack(">I4s", data[at : at + 8])
        head = 8
        if size == 1:
            if at + 16 > end:
                return
            size, head = struct.unpack(">Q", data[at + 8 : at + 16])[0], 16
        elif size == 0:
            size = end - at
        if size < head or at + size > end:
            return
        yield kind, data[at + head : at + size]
        at += size


def _top_level(path: Path) -> tuple[list[tuple[bytes, int, int]], bytes | None]:
    """The file's top-level boxes as (type, offset, size), and the moov payload."""
    found, moov = [], None
    total = path.stat().st_size
    with path.open("rb") as stream:
        at = 0
        while at + 8 <= total and len(found) < 1000:
            stream.seek(at)
            head = stream.read(16)
            size, kind = struct.unpack(">I4s", head[:8])
            header = 8
            if size == 1 and len(head) == 16:
                size, header = struct.unpack(">Q", head[8:16])[0], 16
            elif size == 0:
                size = total - at
            if size < header or at + size > total:
                break
            found.append((kind, at, size))
            if kind == b"moov" and moov is None and size <= MAX_MOOV:
                stream.seek(at + header)
                moov = stream.read(size - header)
            at += size
    return found, moov


def _full(payload: bytes) -> tuple[int, bytes]:
    return payload[0], payload[4:]


def _track(trak: bytes) -> dict:
    track: dict = {}
    for kind, payload in _boxes(trak):
        if kind == b"tkhd":
            version, body = _full(payload)
            body = body[32:] if version == 1 else body[20:]  # times, track id, reserved, duration
            if len(body) >= 60:
                a, b = struct.unpack(">ii", body[16:24])
                w, h = struct.unpack(">II", body[52:60])
                w, h = w >> 16, h >> 16
                track["width"], track["height"] = (h, w) if a == 0 and abs(b) == 0x10000 else (w, h)
        elif kind == b"edts":
            for sub, elst in _boxes(payload):
                if sub == b"elst" and len(elst) >= 8:
                    track["edits"] = struct.unpack(">I", elst[4:8])[0]
        elif kind == b"mdia":
            for sub, body in _boxes(payload):
                if sub == b"hdlr" and len(body) >= 12:
                    track["handler"] = body[8:12]
                elif sub == b"mdhd":
                    version, rest = _full(body)
                    fmt = ">QQIQ" if version == 1 else ">IIII"
                    if len(rest) >= struct.calcsize(fmt):
                        _, _, scale, duration = struct.unpack(fmt, rest[: struct.calcsize(fmt)])
                        track["seconds"] = duration / scale if scale else 0
                elif sub == b"minf":
                    for stbl_kind, stbl in _boxes(body):
                        if stbl_kind == b"stbl":
                            _sample_table(stbl, track)
    return track


def _sample_table(stbl: bytes, track: dict) -> None:
    for kind, payload in _boxes(stbl):
        if kind == b"stsd" and len(payload) >= 16:
            entry = payload[8:]
            size, codec = struct.unpack(">I4s", entry[:8])
            track["codec"] = codec
            sample = entry[8:size]
            # Sound sample entry (QuickTime versions 0/1 and ISO): channels and 16.16 rate after 16 bytes.
            if len(sample) >= 28 and struct.unpack(">H", sample[8:10])[0] in (0, 1):
                track["channels"] = struct.unpack(">H", sample[16:18])[0]
                track["sample_rate"] = struct.unpack(">I", sample[24:28])[0] >> 16
        elif kind == b"stts" and len(payload) >= 8:
            count = struct.unpack(">I", payload[4:8])[0]
            entries = payload[8 : 8 + 8 * count]
            track["samples"] = sum(struct.unpack(">I", entries[n : n + 4])[0] for n in range(0, len(entries) - 7, 8))


def read(path: Path) -> tuple[dict, list[str]]:
    """What the headers say about the clip, and every limit it breaks."""
    size = path.stat().st_size
    boxes, moov = _top_level(path)
    kinds = [kind for kind, _, _ in boxes]
    facts: dict = {"bytes": size, "ext": "mp4"}
    problems: list[str] = []
    if size > MAX_BYTES:
        problems.append("the file is over 300 MB")
    if b"ftyp" not in kinds:
        return facts, problems + ["it isn't an MP4 or MOV file"]
    ftyp = boxes[kinds.index(b"ftyp")]
    with path.open("rb") as stream:
        stream.seek(ftyp[1] + 8)
        brand = stream.read(4)
    facts["ext"] = "mov" if brand in BRANDS_MOV else "mp4"
    if moov is None:
        return facts, problems + ["its index (moov) is missing or unreadable"]
    if b"mdat" in kinds and kinds.index(b"mdat") < kinds.index(b"moov"):
        problems.append("its index (moov) is at the end; export it again with 'optimise for web' or 'fast start'")
    tracks = [_track(payload) for kind, payload in _boxes(moov) if kind == b"trak"]
    video = next((t for t in tracks if t.get("handler") == b"vide"), None)
    audio = next((t for t in tracks if t.get("handler") == b"soun"), None)
    if video is None:
        return facts, problems + ["it has no video track"]
    seconds = video.get("seconds") or 0
    facts.update(
        seconds=round(seconds, 3),
        width=video.get("width"),
        height=video.get("height"),
        video_codec=VIDEO_CODECS.get(video.get("codec"), repr(video.get("codec"))),
        fps=round(video["samples"] / seconds, 2) if seconds and video.get("samples") else None,
        audio_codec=AUDIO_CODECS.get(audio.get("codec"), repr(audio.get("codec"))) if audio else None,
        edit_lists=max(t.get("edits", 0) for t in tracks),
    )
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        problems.append(f"it is {seconds:.1f} s long; reels here are {MIN_SECONDS}-{MAX_SECONDS} s")
    if (facts["width"] or 0) > MAX_WIDTH:
        problems.append(f"it is {facts['width']} px wide; the most is {MAX_WIDTH} (export at 1080p)")
    if video.get("codec") not in VIDEO_CODECS:
        problems.append("its video isn't H.264 or HEVC")
    if facts["fps"] is not None and not MIN_FPS <= facts["fps"] <= MAX_FPS + 0.5:
        problems.append(f"it is {facts['fps']} fps; Instagram needs {MIN_FPS}-{MAX_FPS}")
    if seconds and size * 8 / seconds > MAX_BITRATE:
        problems.append(f"its bitrate is about {size * 8 / seconds / 1e6:.0f} Mbps; the most is 25 (export at 1080p)")
    if facts["edit_lists"] > 1:
        problems.append("it has edit lists; export it again instead of posting a trimmed-in-place file")
    if audio:
        if audio.get("codec") not in AUDIO_CODECS:
            problems.append("its audio isn't AAC")
        if (audio.get("sample_rate") or 0) > MAX_SAMPLE_RATE:
            problems.append("its audio sample rate is over 48 kHz")
        if audio.get("channels") not in (None, 1, 2):
            problems.append("its audio has more than 2 channels")
    return facts, problems


def _ffprobe(path: Path, tool: str) -> list[str]:
    """What only a decoder sees: chroma subsampling, interlacing and the real frame rate."""
    try:
        result = subprocess.run(
            [
                tool,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,pix_fmt,field_order,avg_frame_rate",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        streams = json.loads(result.stdout or "{}").get("streams", [])
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []  # Meta's status_code still judges the clip
    if result.returncode != 0:
        return ["ffprobe couldn't read it; export it again"]
    problems = []
    for stream in streams:
        if not isinstance(stream, dict) or stream.get("codec_type") != "video":
            continue
        pix = str(stream.get("pix_fmt", ""))
        if pix and not pix.startswith(("yuv420", "yuvj420", "nv12")):
            problems.append(f"its colour format is {pix}; Instagram needs 4:2:0")
        if stream.get("field_order") in ("tt", "bb", "tb", "bt"):
            problems.append("it is interlaced; Instagram needs progressive video")
        break
    return problems


def check(path: Path, *, ffprobe: str | None = None) -> tuple[dict, list[str]]:
    """Facts and problems; `facts["checked_with"]` says whether ffprobe took part."""
    facts, problems = read(path)
    tool = ffprobe or find_tool("ffprobe")
    facts["checked_with"] = "ffprobe" if tool else "headers"
    if tool and not problems:
        problems += _ffprobe(path, tool)
    return facts, problems
