import io
import json
import struct
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from command_centre_fixtures import Client, Clock, make_app, make_settings
from test_instagram_publish import DUE, ENV, MADE, SLOT, FakeGraph

import notifications
from command_centre import sources
from command_centre.security import Permission
from instagram_agent import cli, clips, drafts, publish, storage, uploads, video
from instagram_agent.graph import GraphAPIError

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
NOTE = "Bar Italia Soho, espresso, 9/10"


def box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def full(kind: bytes, payload: bytes) -> bytes:
    return box(kind, b"\0\0\0\0" + payload)


def track(handler, codec, *, seconds, samples=0, width=1080, height=1920, rate=44100, channels=2, edits=1):
    matrix = struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    tkhd = full(b"tkhd", b"\0" * 20 + b"\0" * 16 + matrix + struct.pack(">II", width << 16, height << 16))
    elst = box(b"edts", full(b"elst", struct.pack(">I", edits) + b"\0" * 12 * edits))
    mdhd = full(b"mdhd", struct.pack(">IIII", 0, 0, 1000, int(seconds * 1000)) + b"\0\0\0\0")
    hdlr = full(b"hdlr", b"\0\0\0\0" + handler + b"\0" * 12)
    if handler == b"soun":
        entry = b"\0" * 6 + b"\0\1" + b"\0" * 8 + struct.pack(">HHHHI", channels, 16, 0, 0, rate << 16)
    else:
        entry = b"\0" * 70
    stsd = full(b"stsd", struct.pack(">I", 1) + box(codec, entry))
    stts = full(b"stts", struct.pack(">III", 1, samples, 1))
    minf = box(b"minf", box(b"stbl", stsd + stts))
    return box(b"trak", tkhd + elst + box(b"mdia", mdhd + hdlr + minf))


def mp4(*, seconds=10, fps=30, moov_first=True, brand=b"isom", audio=True, mdat=4096, **kw) -> bytes:
    tracks = track(b"vide", kw.pop("codec", b"avc1"), seconds=seconds, samples=int(seconds * fps), **kw)
    if audio:
        tracks += track(b"soun", b"mp4a", seconds=seconds, **{k: kw[k] for k in ("rate", "channels") if k in kw})
    moov = box(b"moov", tracks)
    data = box(b"mdat", b"\0" * mdat)
    return box(b"ftyp", brand + b"\0\0\0\0") + (moov + data if moov_first else data + moov)


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db, self.media = self.root / "instagram.sqlite3", self.root / "drafts"
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)

    def file(self, data: bytes, name="clip.mp4") -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def waiting_clip(self, data=None, when=NOW - timedelta(hours=1)) -> str:
        data = data or mp4()
        clip_id = clips.start(self.conn, self.media, NOTE, len(data), when)

        clips.append(self.conn, self.media, clip_id, 0, io.BytesIO(data), len(data))
        clips.finish(self.conn, self.media, clip_id, check=lambda p: video.check(p, ffprobe="/nonexistent/ffprobe"))
        return clip_id


class VideoCheckTests(Case):
    def headers(self, data, **kw):
        with patch.object(video, "find_tool", return_value=None):
            return video.check(self.file(data, **kw))

    def test_a_clip_inside_every_limit_passes_on_headers_alone(self):
        facts, problems = self.headers(mp4())
        self.assertEqual(problems, [])
        self.assertEqual(
            {k: facts[k] for k in ("seconds", "width", "height", "fps", "video_codec", "audio_codec", "checked_with")},
            {
                "seconds": 10,
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "video_codec": "h264",
                "audio_codec": "aac",
                "checked_with": "headers",
            },
        )

    def test_each_header_limit_is_reported(self):
        cases = {
            "moov": mp4(moov_first=False),
            "s long": mp4(seconds=95),
            "px wide": mp4(width=3840, height=2160),
            "H.264 or HEVC": mp4(codec=b"vp09"),
            "fps": mp4(fps=120),
            "48 kHz": mp4(rate=64000),
            "2 channels": mp4(channels=6),
            "edit lists": mp4(edits=2),
            "Mbps": mp4(seconds=3, mdat=12_000_000),
        }
        for words, data in cases.items():
            with self.subTest(words):
                _, problems = self.headers(data)
                self.assertTrue(any(words in p for p in problems), problems)

    def test_damaged_headers_are_a_problem_not_a_crash(self):
        for empty, trak in ((b"tkhd", box(b"tkhd")), (b"mdhd", box(b"mdia", box(b"mdhd")))):
            data = box(b"ftyp", b"isom\0\0\0\0") + box(b"moov", box(b"trak", trak))
            with self.subTest(empty):
                self.assertEqual(self.headers(data)[1], ["its headers are damaged; export it again"])

    def test_not_a_video_and_mov_brand(self):
        self.assertIn("it isn't an MP4 or MOV file", self.headers(b"\xff\xd8\xff" + b"\0" * 64)[1])
        self.assertEqual(self.headers(mp4(brand=b"qt  "), name="c.mov")[0]["ext"], "mov")
        self.assertEqual(self.headers(mp4(audio=False))[1], [])

    def test_ffprobe_adds_colour_and_scan_checks_when_present(self):
        tool = self.root / "ffprobe"
        reply = {"streams": [{"codec_type": "video", "pix_fmt": "yuv444p", "field_order": "tt"}]}
        tool.write_text(f"#!/bin/sh\necho '{json.dumps(reply)}'\n")
        tool.chmod(0o700)
        facts, problems = video.check(self.file(mp4()), ffprobe=str(tool))
        self.assertEqual(facts["checked_with"], "ffprobe")
        self.assertEqual(len(problems), 2)
        tool.write_text("#!/bin/sh\nexit 1\n")
        self.assertEqual(
            video.check(self.file(mp4()), ffprobe=str(tool))[1], ["ffprobe couldn't read it; export it again"]
        )


class ClipQueueTests(Case):
    def test_chunks_must_arrive_in_order_and_finish_checks_the_whole_clip(self):

        data = mp4()
        clip_id = clips.start(self.conn, self.media, NOTE, len(data), NOW)
        with self.assertRaises(clips.ClipError) as wrong:
            clips.append(self.conn, self.media, clip_id, 10, io.BytesIO(data[10:20]), 10)
        self.assertEqual(wrong.exception.code, "wrong_offset")
        with self.assertRaises(clips.ClipError):
            clips.finish(self.conn, self.media, clip_id)
        clips.append(self.conn, self.media, clip_id, 0, io.BytesIO(data[:100]), 100)
        clips.append(self.conn, self.media, clip_id, 100, io.BytesIO(data[100:]), len(data) - 100)
        with self.assertRaises(clips.ClipError) as over:
            clips.append(self.conn, self.media, clip_id, len(data), io.BytesIO(b"x"), 1)
        self.assertEqual(over.exception.code, "invalid_chunk")
        with patch.object(video, "find_tool", return_value=None):
            facts = clips.finish(self.conn, self.media, clip_id)
        self.assertEqual(facts["seconds"], 10)
        self.assertEqual(clips.oldest(self.conn)["clip_id"], clip_id)
        self.assertTrue((self.media / "clips" / clip_id / "clip.mp4").is_file())

    def test_a_replayed_chunk_is_accepted_only_if_identical(self):
        data = mp4()
        clip_id = clips.start(self.conn, self.media, NOTE, len(data), NOW)
        clips.append(self.conn, self.media, clip_id, 0, io.BytesIO(data[:100]), 100)
        self.assertEqual(clips.append(self.conn, self.media, clip_id, 0, io.BytesIO(data[:100]), 100), 100)
        with self.assertRaises(clips.ClipError) as changed:
            clips.append(self.conn, self.media, clip_id, 0, io.BytesIO(b"x" * 100), 100)
        self.assertEqual(changed.exception.code, "wrong_offset")

    def test_a_chunk_that_breaks_midway_leaves_nothing_behind(self):
        data = mp4()

        class Broken(io.BytesIO):
            def read(self, n=-1):
                if self.tell():
                    raise OSError("connection reset")
                return super().read(10)

        clip_id = clips.start(self.conn, self.media, NOTE, len(data), NOW)
        with self.assertRaises(OSError):
            clips.append(self.conn, self.media, clip_id, 0, Broken(data[:100]), 100)
        self.assertEqual((self.media / "clips" / clip_id / clips.PART).stat().st_size, 0)
        self.assertEqual(clips.append(self.conn, self.media, clip_id, 0, io.BytesIO(data[:100]), 100), 100)

    def test_rounded_times_stay_inside_the_clip(self):
        self.assertEqual(drafts._seconds(3.06, 3.06), 3.0)
        self.assertEqual(drafts._seconds(1.26, 3.06), 1.3)

    def test_finishing_twice_returns_the_same_facts_and_one_clip(self):
        data = mp4()
        clip_id = clips.start(self.conn, self.media, NOTE, len(data), NOW)
        clips.append(self.conn, self.media, clip_id, 0, io.BytesIO(data), len(data))
        with patch.object(video, "find_tool", return_value=None):
            first = clips.finish(self.conn, self.media, clip_id)
            self.assertEqual(clips.finish(self.conn, self.media, clip_id), first)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0], 1)
        self.conn.execute("UPDATE clips SET status='used'")
        self.assertEqual(clips.finish(self.conn, self.media, clip_id), first)  # drafted before the retry
        with self.assertRaises(clips.ClipError):
            clips.finish(self.conn, self.media, "0" * 16)

    def test_finish_recovers_a_rename_that_was_not_committed(self):
        data = mp4()
        clip_id = clips.start(self.conn, self.media, NOTE, len(data), NOW)
        clips.append(self.conn, self.media, clip_id, 0, io.BytesIO(data), len(data))
        where = self.media / "clips" / clip_id
        (where / clips.PART).rename(where / "clip.mp4")
        with patch.object(video, "find_tool", return_value=None):
            self.assertEqual(clips.finish(self.conn, self.media, clip_id)["seconds"], 10)
        self.assertEqual(clips.oldest(self.conn)["clip_id"], clip_id)

    def test_rename_failure_returns_stored_facts_only_for_a_waiting_clip(self):
        for status in ("waiting", "uploading", "used", "deleted"):
            for error in (FileNotFoundError, PermissionError):
                with self.subTest(status=status, error=error):
                    data = mp4()
                    clip_id = clips.start(self.conn, self.media, NOTE, len(data), NOW)
                    clips.append(self.conn, self.media, clip_id, 0, io.BytesIO(data), len(data))
                    stored = {"ext": "mp4", "seconds": 12}

                    def fail_rename(source, target, status=status, clip_id=clip_id, stored=stored, error=error):
                        if status == "deleted":
                            clips.delete(self.conn, self.media, clip_id)
                        else:
                            with self.conn:
                                self.conn.execute(
                                    "UPDATE clips SET status=?, facts=? WHERE clip_id=?",
                                    (status, json.dumps(stored), clip_id),
                                )
                        raise error("rename failed")

                    with patch.object(Path, "rename", fail_rename):

                        def check(p):
                            return {"ext": "mp4", "seconds": 10}, []

                        if status == "waiting":
                            self.assertEqual(clips.finish(self.conn, self.media, clip_id, check=check), stored)
                        else:
                            with self.assertRaises(clips.ClipError) as caught:
                                clips.finish(self.conn, self.media, clip_id, check=check)
                            self.assertEqual(caught.exception.code, "not_found")
                            self.assertIsInstance(caught.exception.__cause__, error)

    def test_a_failing_clip_is_dropped_with_its_problems(self):

        data = mp4(seconds=120)
        clip_id = clips.start(self.conn, self.media, NOTE, len(data), NOW)
        clips.append(self.conn, self.media, clip_id, 0, io.BytesIO(data), len(data))
        with patch.object(video, "find_tool", return_value=None), self.assertRaises(clips.ClipError) as caught:
            clips.finish(self.conn, self.media, clip_id)
        self.assertEqual(caught.exception.code, "clip_rejected")
        self.assertTrue(caught.exception.problems)
        self.assertFalse((self.media / "clips" / clip_id).exists())
        self.assertIsNone(clips.oldest(self.conn))

    def test_size_limit_stale_uploads_and_delete(self):
        with self.assertRaises(clips.ClipError) as big:
            clips.start(self.conn, self.media, NOTE, clips.MAX_CLIP_BYTES + 1, NOW)
        self.assertEqual(big.exception.code, "clip_too_large")
        old = clips.start(self.conn, self.media, NOTE, 10, NOW - timedelta(days=2))
        clips.start(self.conn, self.media, NOTE, 10, NOW)
        self.assertFalse((self.media / "clips" / old).exists())
        waiting = self.waiting_clip()
        self.assertTrue(clips.delete(self.conn, self.media, waiting))
        self.assertFalse(clips.delete(self.conn, self.media, waiting))


class ReelAI:
    draws_text = False

    def __init__(self, reel, score=85):
        self.reel, self.score, self.calls = reel, score, []

    def chat_json(self, system, user, *, images=()):
        self.calls.append(system)
        if system.startswith("You are the Create agent"):
            return self.reel
        if system.startswith("You are the Tags agent"):
            return {"hashtags": [["#coffee", "#soho"]]}
        return {"reviews": [{"score": self.score, "notes": "ok"}]}

    def image(self, prompt):
        raise AssertionError("a reel draws no images")


REEL = {
    "category": "coffee",
    "topic": "Espresso at Bar Italia",
    "angle": "late-night Soho classic",
    "hook": "Soho's best espresso at midnight?",
    "on_screen": [
        {"start": 2, "end": 5, "text": "9/10, no notes"},
        {"start": 0, "end": 2, "text": "Bar Italia"},
        {"start": 8, "end": 3, "text": "backwards"},
        {"start": "x", "end": 4, "text": "bad"},
    ],
    "cover_seconds": 99,
    "caption": "Midnight espresso in Soho. Save this for your next late night.",
}


class ReelDraftTests(Case):
    def test_normalise_keeps_valid_beats_inside_the_clip(self):
        draft = drafts.normalise_reel_draft(REEL, 10.0)
        self.assertEqual(draft["kind"], "reel")
        self.assertEqual([b["text"] for b in draft["reel"]["on_screen"]], ["Bar Italia", "9/10, no notes"])
        self.assertEqual(draft["reel"]["cover_ms"], 9900)
        self.assertEqual(draft["slides"][0]["text"], REEL["hook"])
        self.assertIsNone(drafts.normalise_reel_draft({**REEL, "hook": ""}, 10.0))
        self.assertIsNone(drafts.normalise_reel_draft({**REEL, "caption": "Hi #tag"}, 10.0))
        self.assertIsNone(drafts.normalise_reel_draft({**REEL, "on_screen": REEL["on_screen"][:1]}, 10.0))
        huge = {
            **REEL,
            "cover_seconds": 10**400,
            "on_screen": REEL["on_screen"][:2] + [{"start": -(10**400), "end": 2, "text": "x"}],
        }
        self.assertEqual(drafts.normalise_reel_draft(huge, 10.0)["reel"]["cover_ms"], 0)

    def test_daily_run_turns_the_oldest_clip_into_the_days_reel_instead_of_a_carousel(self):
        clip_id = self.waiting_clip()
        env = {
            "OPENAI_API_KEY": "sk-test",
            "COMMAND_CENTRE_NOTIFICATIONS_DB": str(self.root / "n.sqlite3"),
            "INSTAGRAM_VOICE_FILE": str(self.root / "voice.md"),
        }
        ai = ReelAI(REEL)
        with patch.object(cli, "model_client", return_value=ai):
            self.assertEqual(cli.daily(self.conn, env, self.media, now=NOW), "ok")
        [row] = self.conn.execute("SELECT * FROM drafts").fetchall()
        self.assertEqual(row["kind"], "reel")
        reel = json.loads(row["reel"])
        self.assertEqual(reel["file"], "reel.mp4")
        self.assertTrue((self.media / row["draft_id"] / "reel.mp4").is_file())
        self.assertFalse((self.media / "clips" / clip_id).exists())
        self.assertIn(drafts.CREATE_FROM_CLIP.format(seconds=10), ai.calls[0])
        self.assertEqual(self.conn.execute("SELECT status FROM clips").fetchone()[0], "used")
        with closing(notifications.connect(self.root / "n.sqlite3")) as notes:
            titles = [e["title"] for e in notifications.recent(notes)[0]]
        self.assertTrue(any("reel" in t.lower() for t in titles), titles)

    def test_a_waiting_clip_whose_file_was_lost_is_dropped_and_the_day_falls_back(self):
        clip_id = self.waiting_clip()
        (self.media / "clips" / clip_id / "clip.mp4").unlink()
        env = {
            "OPENAI_API_KEY": "sk-test",
            "COMMAND_CENTRE_NOTIFICATIONS_DB": str(self.root / "n.sqlite3"),
            "INSTAGRAM_VOICE_FILE": str(self.root / "voice.md"),
        }
        with (
            patch.object(cli, "model_client", return_value=ReelAI(REEL)),
            patch.object(drafts, "generate", return_value=[]) as fallback,
        ):
            cli.daily(self.conn, env, self.media, now=NOW)
        fallback.assert_called_once()
        self.assertIsNone(clips.oldest(self.conn))

    def test_photos_are_used_when_the_clip_was_deleted_during_drafting(self):
        self.waiting_clip()
        env = {"OPENAI_API_KEY": "sk-test", "COMMAND_CENTRE_NOTIFICATIONS_DB": str(self.root / "n.sqlite3")}
        uploads.save(self.conn, self.media, NOTE, [(b"\xff\xd8\xffslide", b"\xff\xd8\xffthumb")], NOW)
        with (
            patch.object(cli, "model_client", return_value=ReelAI(REEL)),
            patch.object(drafts, "store_clip_draft", return_value=None),
            patch.object(drafts, "generate_from_upload", return_value={"topic": "t"}) as from_photos,
            patch.object(drafts, "store_upload_draft", return_value="d" * 16),
        ):
            cli.daily(self.conn, env, self.media, now=NOW)
        from_photos.assert_called_once()

    def test_an_upload_whose_photos_were_lost_is_dropped(self):
        upload_id = uploads.save(self.conn, self.media, NOTE, [(b"\xff\xd8\xffslide", b"\xff\xd8\xffthumb")], NOW)
        (self.media / "uploads" / upload_id / "photo-1.jpg").unlink()
        draft = {**drafts.normalise_reel_draft(REEL, 10.0), "kind": "carousel", "reel": None}
        self.assertIsNone(drafts.store_upload_draft(self.conn, draft, uploads.oldest(self.conn), self.media, NOW))
        self.assertIsNone(uploads.oldest(self.conn))

    def test_a_clip_deleted_mid_run_makes_no_draft(self):
        clip_id = self.waiting_clip()
        clip = clips.oldest(self.conn)
        draft = drafts.normalise_reel_draft(REEL, 10.0)
        clips.delete(self.conn, self.media, clip_id)
        self.assertIsNone(drafts.store_clip_draft(self.conn, draft, clip, self.media, NOW))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 0)


class ReelGraph(FakeGraph):
    def __init__(self, *, quota=(0, 100), **kw):
        super().__init__(**kw)
        self.quota, self.gets = quota, []

    def get(self, path_or_url, params=None):
        self.gets.append(path_or_url)
        if path_or_url.endswith("content_publishing_limit"):
            return {"data": [{"quota_usage": self.quota[0], "config": {"quota_total": self.quota[1]}}]}
        return super().get(path_or_url, params)


class ReelPublishTests(Case):
    def setUp(self):
        super().setUp()
        self.public = self.root / "public"
        self.env = {
            **ENV,
            "INSTAGRAM_PUBLIC_DIR": str(self.public),
            "COMMAND_CENTRE_NOTIFICATIONS_DB": str(self.root / "n.sqlite3"),
        }
        self.checked = []
        draft = drafts.normalise_reel_draft({**REEL, "cover_seconds": 2.5}, 10.0)
        draft.update(draft_id="b" * 16, hashtags=["#coffee"], review_score=95, scheduled_for=SLOT)
        draft["reel"]["file"] = "reel.mp4"
        (self.media / draft["draft_id"]).mkdir(parents=True)
        (self.media / draft["draft_id"] / "reel.mp4").write_bytes(mp4())
        with self.conn:
            storage.insert_draft(self.conn, draft, MADE.date().isoformat(), MADE.isoformat())

    def tick(self, graph):
        check = lambda url, size: self.checked.append((url, size))  # noqa: E731
        return publish.tick(self.conn, self.env, self.media, DUE, graph, check=check, sleep=lambda s: None)

    def test_a_reel_needs_approve_even_with_a_high_score(self):
        graph = ReelGraph()
        self.assertEqual(self.tick(graph), "published 0, failed 0")
        self.assertEqual(graph.posts, [])

    def test_approved_reel_posts_through_the_reels_api_once(self):
        storage.decide_draft(self.conn, "b" * 16, "approved", MADE.isoformat())
        graph = ReelGraph(statuses=("IN_PROGRESS", "FINISHED"))
        self.assertEqual(self.tick(graph), "published 1, failed 0")
        (path, params, retry), (publish_path, _, publish_retry) = graph.posts
        self.assertEqual(params["media_type"], "REELS")
        self.assertEqual(params["share_to_feed"], "true")
        self.assertEqual(params["thumb_offset"], "2500")
        self.assertTrue(params["video_url"].endswith("/reel.mp4"))
        self.assertIn("#coffee", params["caption"])
        self.assertTrue(publish_path.endswith("media_publish"))
        self.assertFalse(publish_retry)
        self.assertEqual(self.checked[0][1], len(mp4()))
        self.assertEqual(list(self.public.iterdir()), [self.public / "index.html"])

    def test_meta_error_or_spent_quota_stops_before_publishing(self):
        storage.decide_draft(self.conn, "b" * 16, "approved", MADE.isoformat())
        graph = ReelGraph(statuses=("ERROR",))
        self.assertEqual(self.tick(graph), "published 0, failed 1")
        self.assertFalse(any(p.endswith("media_publish") for p, _, _ in graph.posts))
        spent = ReelGraph(quota=(100, 100))
        with patch.object(publish, "MAX_ATTEMPTS", 5):
            self.assertEqual(self.tick(spent), "published 0, failed 1")
        self.assertEqual(spent.posts, [])
        error = self.conn.execute("SELECT error FROM publish_runs ORDER BY id DESC").fetchone()[0]
        self.assertIn("publishing limit", error)

    def test_reel_insights_fall_back_to_the_common_metrics(self):
        class Insights:
            def media_insights(self, media_id, metrics="base"):
                if metrics != "base":
                    raise GraphAPIError("no")
                return {"views": 5}

        self.assertEqual(cli._insights(Insights(), {"id": "1", "media_product_type": "REELS"}), {"views": 5})


class ReelCommandCentreTests(Case):
    def client(self):
        settings = make_settings(
            self.root, permission_ceiling=Permission.RECOMMEND, instagram_db=self.db, instagram_media=self.media
        )
        client = Client(make_app(settings, clock=Clock()))
        client.login()
        plain = client.request

        def request(method, path, **kwargs):
            if client.csrf:
                kwargs.setdefault("headers", {"X-CSRF-Token": client.csrf})
            return plain(method, path, **kwargs)

        client.request = request
        return client

    def test_upload_in_chunks_then_listed_then_deleted(self):
        client, data = self.client(), mp4()
        start = client.request("POST", "/api/v1/instagram/clips", body={"note": NOTE, "size": len(data)})
        self.assertEqual(start.status, 200, start.json())
        clip_id = start.json()["clip_id"]
        bad = client.request("POST", "/api/v1/instagram/clips/chunk", raw=data[:5], query=f"clip={clip_id}&offset=0")
        self.assertEqual(bad.status, 415)
        chunk = client.request(
            "POST",
            "/api/v1/instagram/clips/chunk",
            raw=data,
            query=f"clip={clip_id}&offset=0",
            content_type="application/octet-stream",
        )
        self.assertEqual(chunk.json(), {"clip_id": clip_id, "received": len(data)})
        with patch.object(video, "find_tool", return_value=None):
            done = client.request("POST", "/api/v1/instagram/clips/finish", body={"clip_id": clip_id})
        self.assertEqual(done.json(), {"clip_id": clip_id, "seconds": 10})
        listed = client.request("GET", "/api/v1/instagram/drafts").json()
        self.assertEqual([c["clip_id"] for c in listed["clips"]], [clip_id])
        gone = client.request("POST", "/api/v1/instagram/clips/delete", body={"clip_id": clip_id})
        self.assertEqual(gone.status, 200)

    def test_rejected_clip_returns_every_problem(self):
        client, data = self.client(), mp4(seconds=2, width=2160, height=3840)
        clip_id = client.request("POST", "/api/v1/instagram/clips", body={"note": NOTE, "size": len(data)}).json()[
            "clip_id"
        ]
        client.request(
            "POST",
            "/api/v1/instagram/clips/chunk",
            raw=data,
            query=f"clip={clip_id}&offset=0",
            content_type="application/octet-stream",
        )
        with patch.object(video, "find_tool", return_value=None):
            done = client.request("POST", "/api/v1/instagram/clips/finish", body={"clip_id": clip_id})
        self.assertEqual(done.status, 422)
        self.assertEqual(len(done.json()["problems"]), 2)

    def test_needs_csrf_and_recommend(self):
        client = self.client()
        client.csrf = None
        self.assertEqual(client.request("POST", "/api/v1/instagram/clips", body={"note": NOTE, "size": 10}).status, 403)
        anonymous = Client(make_app(make_settings(self.root, instagram_db=self.db, instagram_media=self.media)))
        self.assertEqual(
            anonymous.request("POST", "/api/v1/instagram/clips", body={"note": NOTE, "size": 10}).status, 401
        )

    def test_reel_draft_shows_its_script_and_needs_approve(self):
        draft = drafts.normalise_reel_draft(REEL, 10.0)
        draft.update(draft_id="c" * 16, review_score=95, scheduled_for=SLOT, hashtags=[])
        with self.conn:
            storage.insert_draft(self.conn, draft, MADE.date().isoformat(), MADE.isoformat())
        settings = make_settings(self.root, instagram_db=self.db, instagram_media=self.media)
        [shown] = sources.instagram_drafts(settings)["drafts"]
        self.assertEqual(shown["kind"], "reel")
        self.assertFalse(shown["will_post"])
        self.assertEqual(shown["reel"]["hook"], REEL["hook"])
        self.assertEqual(shown["reel"]["cover_seconds"], 9.9)


if __name__ == "__main__":
    unittest.main()
