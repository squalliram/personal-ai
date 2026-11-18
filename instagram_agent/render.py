"""Turn a reel draft into the MP4 Meta gets: the owner's photos (Ken Burns) or their trimmed clip, on-screen text
burned in, and optional licensed music, all with the host's ffmpeg.

1080x1920 H.264/AAC 48 kHz with `moov` first. Text is drawn by ffmpeg's `drawtext` when it has libfreetype, else
rendered to PNG with Pillow and overlaid. One render runs at a time, niced and under a time limit; a failed render
holds the reel (it never posts the original instead) and is retried on the next daily ticks.
"""

from __future__ import annotations

import fcntl
import functools
import json
import os
import re
import shutil
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import music, publish, video
from .host_probe import find_tool

WIDTH, HEIGHT, FPS = 1080, 1920, 30
PHOTO_SECONDS, MIN_TOTAL, MAX_TOTAL = 3.0, 7.0, 30.0
ZOOM = 0.08
TIMEOUT = 600
NICE = 10
THREADS = "4"
ATTEMPTS = 3
FONT = Path(__file__).with_name("assets") / "Poppins-Bold.ttf"
SAFE_PATH = re.compile(r"[A-Za-z0-9_./-]+")
LOCK = ".render.lock"
OUT = "reel.mp4"
TEXT = re.compile(r"[^\x20-\x7e\u00a0-\u024f\u2010-\u2027\u20ac\u2122]")  # what the bundled font draws
MUSIC_UNDER = 0.3
LOUDNESS = "loudnorm=I=-14:TP=-1.5:LRA=11"


class RenderError(RuntimeError):
    """A render that didn't produce a postable reel; the message is safe to show the owner."""


@functools.lru_cache(maxsize=1)
def capabilities() -> dict:
    """What this host's ffmpeg (and Python) can do for reels."""
    found = {"ffmpeg": find_tool("ffmpeg"), "zoompan": False, "drawtext": False, "libx264": False, "aac": False}
    found.update(loudnorm=False, pillow=_pillow())
    if found["ffmpeg"]:
        filters, encoders = _list(found["ffmpeg"], "-filters"), _list(found["ffmpeg"], "-encoders")
        for name in ("zoompan", "drawtext", "loudnorm"):
            found[name] = name in filters
        found["libx264"], found["aac"] = "libx264" in encoders, "aac" in encoders
    return found


def _pillow() -> bool:
    try:
        import PIL.ImageDraw  # noqa: F401
        import PIL.ImageFont  # noqa: F401
    except ImportError:
        return False
    return True


def _list(ffmpeg: str, flag: str) -> set[str]:
    try:
        result = subprocess.run([ffmpeg, "-hide_banner", flag], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    names = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"[A-Z.|]{2,6}", parts[0]):
            names.add(parts[1])
    return names


def can_render(source: str, caps: dict | None = None) -> bool:
    """`source` is 'photos' or 'clip'. Needs ffmpeg with libx264 and aac, a way to draw text and, for photos, zoompan."""
    caps = caps or capabilities()
    ready = bool(caps["ffmpeg"] and caps["libx264"] and caps["aac"] and (caps["drawtext"] or caps["pillow"]))
    return ready and (source != "photos" or caps["zoompan"])


def photo_seconds(count: int) -> float:
    """About 3 s a photo, stretched so even one photo makes a 7 s reel; 10 photos make 30 s."""
    return round(max(PHOTO_SECONDS, MIN_TOTAL / count), 2) if count else 0.0


def _safe(path: Path) -> str:
    text = str(path)
    if not SAFE_PATH.fullmatch(text):
        raise RenderError("a render file path has characters ffmpeg's filters can't take")
    return text


def clean_text(text: str) -> str:
    return " ".join(TEXT.sub("", text).split())


def lines(text: str, hook: bool) -> list[str]:
    return textwrap.wrap(clean_text(text), 18 if hook else 22)[:3]


def _layout(beat: dict, index: int) -> tuple[list[str], int, int]:
    """Lines, font size and top y for one beat: the hook high up, later beats in the middle (clear of the UI)."""
    hook = index == 0
    size = 76 if hook else 62
    return lines(beat["text"], hook), size, int(HEIGHT * (0.24 if hook else 0.56))


def _drawtext(beats: list[dict], work: Path) -> list[str]:
    filters = []
    for index, beat in enumerate(beats):
        rows, size, top = _layout(beat, index)
        for row, line in enumerate(rows):
            file = work / f"text-{index}-{row}.txt"
            file.write_text(line, encoding="utf-8")
            filters.append(
                f"drawtext=fontfile={_safe(FONT)}:textfile={_safe(file)}:expansion=none:fontsize={size}:"
                f"fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=18:x=(w-text_w)/2:"
                f"y={top + row * int(size * 1.45)}:enable='between(t,{beat['start']},{beat['end']})'"
            )
    return filters


def _pngs(beats: list[dict], work: Path) -> list[Path]:
    """One transparent full-frame PNG per beat, laid out like the drawtext version."""
    from PIL import Image, ImageDraw, ImageFont

    files = []
    for index, beat in enumerate(beats):
        rows, size, top = _layout(beat, index)
        font = ImageFont.truetype(str(FONT), size)
        image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        for row, line in enumerate(rows):
            left, upper, right, lower = draw.textbbox((0, 0), line, font=font)
            x, y = (WIDTH - (right - left)) // 2, top + row * int(size * 1.45)
            draw.rectangle((x - 18, y + upper - 18, x + right - left + 18, y + lower + 18), fill=(0, 0, 0, 140))
            draw.text((x - left, y), line, font=font, fill=(255, 255, 255, 255))
        files.append(work / f"text-{index}.png")
        image.save(files[-1])
    return files


def _frame(label_in: str, label_out: str) -> str:
    """Fit into 9:16 over a blurred, filled copy of itself."""
    return (
        f"[{label_in}]split[{label_out}a][{label_out}b];"
        f"[{label_out}a]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},"
        f"boxblur=24:3[{label_out}bg];"
        f"[{label_out}b]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease[{label_out}fg];"
        f"[{label_out}bg][{label_out}fg]overlay=(W-w)/2:(H-h)/2"
    )


def _ken_burns(index: int, seconds: float) -> str:
    """Slow zoom in on even photos and out on odd ones, drifting a little sideways, without zoompan's jitter."""
    frames = max(int(round(seconds * FPS)), 1)
    step = ZOOM / frames
    zoom = f"min(1+{step:.6f}*on,{1 + ZOOM})" if index % 2 == 0 else f"max({1 + ZOOM}-{step:.6f}*on,1)"
    drift = "+" if index % 4 < 2 else "-"
    return (
        f",scale={WIDTH * 2}:{HEIGHT * 2},zoompan=z='{zoom}':d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS}:"
        f"x='iw/2-(iw/zoom/2){drift}on*0.6':y='ih/2-(ih/zoom/2)',setsar=1"
    )


def _audio(
    source_audio: str | None, track: int | None, mix: str, seconds: float, loudnorm: bool = True
) -> tuple[str, str | None]:
    """The audio filter chain and its output label, or ('', None) for silence."""
    fade = max(seconds - 1.5, 0)
    tail = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    level = f"{LOUDNESS}," if loudnorm else ""
    if track is not None:
        song = f"[{track}:a]atrim=0:{seconds},asetpts=N/SR/TB,{tail},afade=t=in:d=0.5,afade=t=out:st={fade}:d=1.5"
        if source_audio and mix == "under":
            return (
                f"[{source_audio}]{tail}[orig];{song},volume={MUSIC_UNDER}[song];"
                f"[orig][song]amix=inputs=2:duration=first:dropout_transition=0,{level}{tail}[aout]"
            ), "aout"
        return f"{song},{level}{tail}[aout]", "aout"
    if source_audio:
        return f"[{source_audio}]{tail}[aout]", "aout"
    return "", None


def command(draft_dir: Path, reel: dict, work: Path, caps: dict, track: Path | None) -> tuple[list[str], float]:
    """The ffmpeg argv for this reel, and its length in seconds."""
    _safe(work), _safe(draft_dir), _safe(FONT)
    beats = _beats(reel)
    inputs: list[str] = []
    graph: list[str] = []
    if reel.get("source") == "photos":
        count = int(reel["photos"])
        each = photo_seconds(count)
        for n in range(count):
            inputs += ["-i", str(draft_dir / f"slide-{n + 1}.jpg")]
            graph.append(_frame(f"{n}:v", f"p{n}") + _ken_burns(n, each) + f"[v{n}]")
        graph.append("".join(f"[v{n}]" for n in range(count)) + f"concat=n={count}:v=1:a=0[base]")
        seconds, source_audio = round(each * count, 2), None
    else:
        trim = reel.get("trim") or {"in": 0, "out": reel["seconds"]}
        seconds = round(trim["out"] - trim["in"], 2)
        inputs += ["-ss", str(trim["in"]), "-t", str(seconds), "-i", str(draft_dir / reel["source_file"])]
        graph.append(_frame("0:v", "c") + f",fps={FPS},setsar=1[base]")
        count, source_audio = 1, "0:a" if reel.get("audio", True) else None
    label = "base"
    if caps["drawtext"]:
        graph.append(f"[{label}]" + ",".join(_drawtext(beats, work) or ["null"]) + "[texted]")
        label = "texted"
    else:
        for index, png in enumerate(_pngs(beats, work)):
            inputs += ["-i", str(png)]
            beat = beats[index]
            graph.append(
                f"[{label}][{count + index}:v]overlay=0:0:enable='between(t,{beat['start']},{beat['end']})'[t{index}]"
            )
            label = f"t{index}"
        count += len(beats)
    graph.append(f"[{label}]format=yuv420p[vout]")
    track_index = None
    if track is not None:
        inputs += ["-stream_loop", "-1", "-i", str(track)]
        track_index = count
    mix = (reel.get("music") or {}).get("mix", "under")
    chain, audio = _audio(source_audio, track_index, mix, seconds, bool(caps.get("loudnorm")))
    if chain:
        graph.append(chain)
    argv = [caps["ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *inputs]
    argv += ["-filter_complex", ";".join(graph), "-map", "[vout]"]
    argv += ["-map", f"[{audio}]", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"] if audio else ["-an"]
    argv += ["-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-crf", "21", "-maxrate", "10M"]
    argv += ["-bufsize", "20M", "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2), "-flags", "+cgop"]
    argv += ["-threads", THREADS, "-t", str(seconds), "-movflags", "+faststart", str(work / OUT)]
    return argv, seconds


def _beats(reel: dict) -> list[dict]:
    """On-screen beats in the rendered reel's own time: shifted by the trim and cut to its length."""
    start = (reel.get("trim") or {}).get("in", 0) if reel.get("source") != "photos" else 0
    end = reel["seconds"] if reel.get("source") == "photos" else (reel.get("trim") or {}).get("out", reel["seconds"])
    beats = []
    for beat in reel.get("on_screen") or []:
        a, b = max(beat["start"], start) - start, min(beat["end"], end) - start
        if b > a and clean_text(beat["text"]):
            beats.append({"start": round(a, 2), "end": round(b, 2), "text": beat["text"]})
    if reel.get("hook") and not any(b["start"] < 0.5 for b in beats):
        first = beats[0]["start"] if beats else 2.0
        beats.insert(0, {"start": 0.0, "end": round(min(first, 2.0), 2) or 2.0, "text": reel["hook"]})
    return beats


def _run(argv: list[str]) -> None:
    try:
        result = subprocess.run(
            ["nice", "-n", str(NICE), *argv] if shutil.which("nice") else argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RenderError(f"ffmpeg took over {TIMEOUT // 60} minutes") from None
    except OSError as exc:
        raise RenderError(f"ffmpeg couldn't start ({type(exc).__name__})") from None
    if result.returncode != 0:
        raise RenderError(f"ffmpeg failed (exit {result.returncode})")


def render(conn, media_dir: Path, draft_id: str, reel: dict, caps: dict | None = None) -> dict:
    """Render one draft's reel to <draft>/reel.mp4 and return the updated reel facts."""
    caps = caps or capabilities()
    if not can_render(reel.get("source", "clip"), caps):
        raise RenderError("this server's ffmpeg can't render reels")
    draft_dir = media_dir / draft_id
    if draft_dir.is_symlink() or not draft_dir.is_dir():
        raise RenderError("the draft's files are missing")
    track = None
    if reel.get("music"):
        track = music.usable_file(conn, media_dir, reel["music"].get("track_id", ""))
        if track is None:
            raise RenderError("the chosen music track was deleted or has no recorded licence")
    work = draft_dir / ".render"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(mode=0o700)
    try:
        argv, seconds = command(draft_dir, reel, work, caps, track)
        _run(argv)
        facts, problems = video.check(work / OUT)
        if problems:
            raise RenderError("the rendered reel broke Instagram's rules: " + "; ".join(problems))
        os.replace(work / OUT, draft_dir / OUT)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    start = (reel.get("trim") or {}).get("in", 0) if reel.get("source") != "photos" else 0
    cover = min(max(int(reel.get("cover_ms", 0)) - int(start * 1000), 0), max(int(seconds * 1000) - 100, 0))
    return {**reel, "file": OUT, "render": "done", "seconds": seconds, "cover_ms": cover, "bytes": facts["bytes"]}


def slot_after_render(row, now: datetime) -> str | None:
    """A reel rendered after its posting window posts now if it is still its London day, else not at all."""
    if not row["scheduled_for"]:
        return row["scheduled_for"]
    when = publish.posts_at(row["scheduled_for"], row["created_at"])
    if when + publish.STALE_AFTER >= now:
        return row["scheduled_for"]
    london = ZoneInfo("Europe/London")
    if publish._instant(row["scheduled_for"]).astimezone(london).date() == now.astimezone(london).date():
        return now.isoformat(timespec="seconds")
    return None


def render_due(
    conn, media_dir: Path, notify_failure=lambda row, error, final: None, now: datetime | None = None
) -> str:
    """Render every reel draft waiting for it, one at a time; skipped while another render runs."""
    media_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(media_dir / LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "render busy"
        rows = conn.execute(
            "SELECT draft_id, topic, reel, scheduled_for, created_at FROM drafts "
            "WHERE kind='reel' AND status IN ('pending','approved') "
            "ORDER BY scheduled_for"
        ).fetchall()
        done = failed = 0
        for row in rows:
            reel = json.loads(row["reel"] or "{}")
            if reel.get("render") != "pending":
                continue
            try:
                reel = render(conn, media_dir, row["draft_id"], reel)
                done += 1
                slot = slot_after_render(row, now or datetime.now(timezone.utc))  # the clock after a long render
                if slot is None:
                    notify_failure(row, "it was made after its posting day ended", True)
                with conn:
                    conn.execute("UPDATE drafts SET scheduled_for=? WHERE draft_id=?", (slot, row["draft_id"]))
            except (RenderError, OSError, KeyError, TypeError, ValueError) as exc:
                error = str(exc) if isinstance(exc, RenderError) else type(exc).__name__
                tries = int(reel.get("render_attempts", 0)) + 1
                final = tries >= ATTEMPTS
                reel = {**reel, "render_attempts": tries, "render_error": error[:200]}
                if final:
                    reel["render"] = "failed"
                failed += 1
                notify_failure(row, error, final)
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                latest = conn.execute("SELECT reel FROM drafts WHERE draft_id=?", (row["draft_id"],)).fetchone()
                try:
                    stored = json.loads(latest[0] or "{}") if latest else {}
                except (TypeError, ValueError):
                    stored = {}
                for key in ("hold", "hold_version"):  # set by the owner while this rendered
                    if isinstance(stored, dict) and key in stored:
                        reel[key] = stored[key]
                conn.execute(
                    "UPDATE drafts SET reel=? WHERE draft_id=? AND kind='reel'",
                    (json.dumps(reel, ensure_ascii=False), row["draft_id"]),
                )
    return f"rendered {done}, failed {failed}"
