import io
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from command_centre_fixtures import Client, Clock, make_app, make_settings
from test_instagram_reels import REEL, Case, ReelAI
from test_instagram_uploads import Fixture, form, photo

from command_centre.security import Permission
from instagram_agent import drafts, music, publish, render, storage, uploads, video

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
CAPS = {"ffmpeg": "/usr/bin/ffmpeg", "zoompan": True, "drawtext": True, "libx264": True, "aac": True}
CAPS.update(loudnorm=True, pillow=True)
MP3 = b"ID3\x04\0\0\0\0\0\0" + b"\xff\xfb" + b"\0" * 400
BEATS = [{"start": 0, "end": 2, "text": "Best espresso in Soho?"}, {"start": 2, "end": 6, "text": "Bar Italia, 9/10"}]


def photo_reel(**overrides):
    return {"source": "photos", "photos": 3, "seconds": 9.0, "hook": "Best espresso in Soho?", "on_screen": BEATS,
            "cover_ms": 500, "render": "pending", **overrides}  # fmt: skip


PROBE = render.capabilities.__wrapped__


def has_ffmpeg() -> bool:
    caps = PROBE()
    return render.can_render("photos", caps) and render.can_render("clip", caps)


class CapabilityTests(unittest.TestCase):
    def test_lists_filters_and_encoders_from_ffmpeg_output(self):
        filters = " T.. zoompan  V->V  Apply Zoom & Pan effect.\n ... drawtext  V->V  Draw text\n"
        encoders = " V..... libx264  libx264 H.264\n A..... aac  AAC (Advanced Audio Coding)\n"
        outputs = {"-filters": filters, "-encoders": encoders}
        run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, outputs[argv[2]], "")  # noqa: E731
        with patch.object(render, "find_tool", return_value="/usr/bin/ffmpeg"), patch.object(subprocess, "run", run):
            caps = render.capabilities.__wrapped__()
        self.assertTrue(caps["zoompan"] and caps["drawtext"] and caps["libx264"] and caps["aac"])
        self.assertFalse(caps["loudnorm"])

    def test_render_needs_x264_aac_text_and_zoompan_for_photos(self):
        self.assertTrue(render.can_render("photos", CAPS))
        self.assertFalse(render.can_render("photos", {**CAPS, "zoompan": False}))
        self.assertTrue(render.can_render("clip", {**CAPS, "zoompan": False}))
        self.assertTrue(render.can_render("clip", {**CAPS, "drawtext": False}))  # Pillow draws the text
        self.assertFalse(render.can_render("clip", {**CAPS, "drawtext": False, "pillow": False}))
        self.assertFalse(render.can_render("clip", {**CAPS, "libx264": False}))
        self.assertFalse(render.can_render("clip", {**CAPS, "ffmpeg": None}))

    def test_host_probe_reports_reel_features(self):
        from instagram_agent import host_probe

        with patch.object(render, "capabilities", return_value={**CAPS, "drawtext": False}):
            line = host_probe.reel_features()
        self.assertEqual(line, "reel render: zoompan yes, drawtext no, loudnorm yes, libx264 yes, aac yes, pillow yes")
        with patch.object(render, "capabilities", return_value={**CAPS, "ffmpeg": None}):
            self.assertEqual(host_probe.reel_features(), "reel render: no ffmpeg")


class CommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.work = self.dir / ".render"
        self.work.mkdir()

    def test_photo_lengths_stay_between_7_and_30_seconds(self):
        self.assertEqual(render.photo_seconds(1), 7.0)
        self.assertEqual(render.photo_seconds(2), 3.5)
        self.assertEqual(render.photo_seconds(10) * 10, 30.0)

    def test_photo_reel_is_ken_burns_9_16_h264_aac_with_moov_first(self):
        argv, seconds = render.command(self.dir, photo_reel(), self.work, CAPS, None)
        self.assertEqual(seconds, 9.0)
        graph = argv[argv.index("-filter_complex") + 1]
        self.assertEqual(graph.count("zoompan="), 3)
        self.assertIn("concat=n=3:v=1:a=0", graph)
        self.assertIn("s=1080x1920", graph)
        self.assertEqual(graph.count("drawtext="), 3)  # the hook wraps to two lines
        self.assertIn("expansion=none", graph)
        for flag in ("libx264", "+faststart", "yuv420p", "-an"):
            self.assertIn(flag, argv)
        self.assertEqual((self.work / "text-0-0.txt").read_text(), "Best espresso in")

    def test_text_falls_back_to_pillow_pngs_without_drawtext(self):
        argv, _ = render.command(self.dir, photo_reel(), self.work, {**CAPS, "drawtext": False}, None)
        graph = argv[argv.index("-filter_complex") + 1]
        self.assertNotIn("drawtext", graph)
        self.assertIn("[3:v]overlay=0:0:enable='between(t,0,2)'", graph)
        self.assertTrue((self.work / "text-1.png").is_file())

    def test_clip_is_trimmed_with_beats_in_reel_time_and_music_mixed_under(self):
        reel = {**photo_reel(), "source": "clip", "source_file": "source.mp4", "seconds": 20.0}
        reel.update(trim={"in": 4.0, "out": 14.0}, music={"track_id": "a" * 16, "mix": "under"}, audio=True)
        reel["on_screen"] = [{"start": 4, "end": 6, "text": "Hook"}, {"start": 12, "end": 18, "text": "Late"}]
        argv, seconds = render.command(self.dir, reel, self.work, CAPS, self.dir / "track.mp3")
        self.assertEqual(seconds, 10.0)
        self.assertEqual(argv[argv.index("-ss") + 1 : argv.index("-ss") + 4], ["4.0", "-t", "10.0"])
        graph = argv[argv.index("-filter_complex") + 1]
        self.assertIn("between(t,0.0,2.0)", graph)
        self.assertIn("between(t,8.0,10.0)", graph)  # cut at the trim's end
        self.assertIn("amix=inputs=2", graph)
        self.assertIn("loudnorm", graph)
        self.assertIn("afade=t=out:st=8.5", graph)
        self.assertEqual(argv[argv.index("-stream_loop") + 1], "-1")
        self.assertIn("aac", argv)

    def test_music_can_replace_the_audio_and_clips_keep_their_own_otherwise(self):
        reel = {**photo_reel(), "source": "clip", "source_file": "source.mp4", "seconds": 10.0, "audio": True}
        graph = render.command(self.dir, reel, self.work, CAPS, None)[0]
        self.assertIn("[0:a]aresample", ";".join(graph))
        reel["music"] = {"track_id": "a" * 16, "mix": "replace"}
        argv, _ = render.command(self.dir, reel, self.work, CAPS, self.dir / "t.mp3")
        graph = argv[argv.index("-filter_complex") + 1]
        self.assertNotIn("[0:a]", graph)
        self.assertNotIn("amix", graph)

    def test_music_skips_loudnorm_when_ffmpeg_lacks_it(self):
        argv, _ = render.command(self.dir, photo_reel(), self.work, {**CAPS, "loudnorm": False}, self.dir / "t.mp3")
        graph = argv[argv.index("-filter_complex") + 1]
        self.assertNotIn("loudnorm", graph)
        self.assertIn("afade", graph)

    def test_unsafe_paths_and_missing_hook_beat(self):
        with self.assertRaises(render.RenderError):
            render.command(self.dir / "it's", photo_reel(), self.dir / "it's", CAPS, None)
        beats = render._beats({**photo_reel(), "on_screen": [{"start": 3, "end": 5, "text": "Later"}]})
        self.assertEqual(beats[0], {"start": 0.0, "end": 2.0, "text": "Best espresso in Soho?"})


@unittest.skipUnless(has_ffmpeg(), "needs ffmpeg with libx264, aac, zoompan and a text renderer")
class RealRenderTests(Case):
    def test_default_photo_music_is_baked_into_the_reusable_reel_file(self):
        audio = io.BytesIO()
        with wave.open(audio, "wb") as track:
            track.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
            track.writeframes(b"\0\0" * 44100)
        track_id = music.save(self.conn, self.media, "Test audio", "CC0", "", audio.getvalue(), NOW)
        jpeg = photo((600, 800))
        uploads.save(self.conn, self.media, "Coffee", [(jpeg, jpeg)], NOW, reel=True)
        upload = uploads.oldest(self.conn)
        draft = drafts.generate_from_upload(
            self.conn, ReelAI({**REEL, "music_track": track_id}), NOW, upload, self.media, as_reel=True
        )
        draft_id = drafts.store_upload_draft(self.conn, draft, upload, self.media, NOW)
        reel = render.render(self.conn, self.media, draft_id, draft["reel"], PROBE())
        self.assertEqual((reel["render"], reel["file"], reel["music"]["mix"]), ("done", "reel.mp4", "replace"))
        facts, problems = video.check(self.media / draft_id / "reel.mp4")
        self.assertEqual(problems, [])
        self.assertEqual(facts["audio_codec"], "aac")

    def test_renders_a_reel_that_meets_meta_spec(self):
        folder = self.media / "d1"
        folder.mkdir(parents=True)
        (folder / "slide-1.jpg").write_bytes(photo((600, 800)))
        caps = PROBE()
        for text_caps in (caps, {**caps, "drawtext": False}):
            reel = render.render(self.conn, self.media, "d1", photo_reel(photos=1, seconds=7.0), text_caps)
            self.assertEqual((reel["render"], reel["file"], reel["seconds"]), ("done", "reel.mp4", 7.0))
            facts, problems = video.check(folder / "reel.mp4")
            self.assertEqual(problems, [])
            self.assertEqual((facts["width"], facts["height"]), (1080, 1920))
            self.assertFalse((folder / ".render").exists())


class RenderDueTests(Case):
    def add_reel(self, reel):
        draft = {"kind": "reel", "category": "coffee", "topic": "Bar Italia", "angle": "", "caption": "Hi",
                 "slides": [], "hashtags": [], "reel": reel, "draft_id": "d" * 16,
                 "scheduled_for": "2026-11-12T18:00:00+00:00"}  # fmt: skip
        with self.conn:
            storage.insert_draft(self.conn, draft, "2026-11-12", NOW.isoformat())

    def reel(self):
        return json.loads(self.conn.execute("SELECT reel FROM drafts").fetchone()[0])

    def test_a_failing_render_retries_then_holds_the_reel(self):
        self.add_reel(photo_reel())
        told = []
        failing = patch.object(render, "render", side_effect=render.RenderError("ffmpeg failed (exit 1)"))
        with failing:
            for _ in range(render.ATTEMPTS):
                render.render_due(self.conn, self.media, lambda row, error, final: told.append((error, final)))
        self.assertEqual(self.reel()["render"], "failed")
        self.assertEqual(told[-1], ("ffmpeg failed (exit 1)", True))
        self.assertEqual([final for _, final in told], [False, False, True])
        [row] = self.conn.execute("SELECT * FROM drafts").fetchall()
        self.assertFalse(publish.rendered(row))

    def test_done_render_is_stored_and_postable_and_only_one_runs_at_once(self):
        self.add_reel(photo_reel())
        [row] = self.conn.execute("SELECT * FROM drafts").fetchall()
        self.assertFalse(publish.rendered(row))
        with patch.object(render, "render", side_effect=lambda c, m, d, reel: {**reel, "render": "done"}):
            self.assertEqual(render.render_due(self.conn, self.media, now=NOW), "rendered 1, failed 0")
        [row] = self.conn.execute("SELECT * FROM drafts").fetchall()
        self.assertTrue(publish.rendered(row))
        import fcntl

        with open(self.media / render.LOCK, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(render.render_due(self.conn, self.media), "render busy")

    def test_a_reel_made_after_its_window_posts_now_that_day_or_not_at_all(self):
        self.add_reel(photo_reel())
        told = []
        late = datetime(2026, 11, 12, 22, 0, tzinfo=timezone.utc)
        with patch.object(render, "render", side_effect=lambda c, m, d, reel: {**reel, "render": "done"}):
            render.render_due(self.conn, self.media, lambda row, error, final: told.append(error), now=late)
        self.assertEqual(self.conn.execute("SELECT scheduled_for FROM drafts").fetchone()[0], late.isoformat())
        self.assertEqual(told, [])
        with self.conn:
            self.conn.execute(
                "UPDATE drafts SET reel=?, scheduled_for=?", (json.dumps(photo_reel()), "2026-11-12T18:00:00+00:00")
            )
        with patch.object(render, "render", side_effect=lambda c, m, d, reel: {**reel, "render": "done"}):
            render.render_due(
                self.conn, self.media, lambda row, error, final: told.append(error), now=late + timedelta(hours=3)
            )
        self.assertIsNone(self.conn.execute("SELECT scheduled_for FROM drafts").fetchone()[0])
        self.assertEqual(told, ["it was made after its posting day ended"])

    def test_a_deleted_or_unlicensed_track_fails_the_render(self):
        (self.media / "x").mkdir(parents=True)
        with self.assertRaisesRegex(render.RenderError, "licence"):
            render.render(self.conn, self.media, "x", photo_reel(music={"track_id": "a" * 16}), CAPS)


class MusicTests(Case):
    def test_only_tracks_with_a_licence_are_saved_and_used(self):
        with self.assertRaises(music.MusicError) as caught:
            music.save(self.conn, self.media, "Song", "  ", "", MP3, NOW)
        self.assertEqual(caught.exception.code, "licence_required")
        with self.assertRaises(music.MusicError):
            music.save(self.conn, self.media, "Song", "CC0", "", b"not audio", NOW)
        track_id = music.save(self.conn, self.media, "Song", "Pixabay Content License", "pixabay.com", MP3, NOW)
        self.assertEqual([t["track_id"] for t in music.tracks(self.conn)], [track_id])
        self.assertEqual(music.usable_file(self.conn, self.media, track_id).name, "track.mp3")
        self.assertEqual(music.choice(track_id, "under", self.conn)["track_id"], track_id)
        self.assertEqual(music.choice("agents", "replace"), {"pick": "agents", "mix": "replace"})
        self.assertEqual(music.choice("", "under"), {"pick": "agents", "mix": "under"})
        self.assertEqual(music.choice(None, "replace"), {"pick": "agents", "mix": "replace"})
        self.assertEqual(music.choice("off", "under"), {"pick": "off"})
        for bad in (("f" * 16, "under"), (track_id, "loud"), ("../x", "under")):
            with self.assertRaises(music.MusicError):
                music.choice(*bad, conn=self.conn)
        self.assertTrue(music.delete(self.conn, self.media, track_id))
        self.assertIsNone(music.usable_file(self.conn, self.media, track_id))
        self.assertEqual(music.kind(b"RIFF\0\0\0\0WAVEfmt "), "wav")
        self.assertEqual(music.kind(b"\0\0\0\x20ftypM4A "), "m4a")


class ReelDraftRenderTests(Case):
    def test_fallback_keeps_24h_measurements_when_72h_has_no_measurements(self):
        tracks = [
            {
                "track_id": "a" * 16,
                "title": "Lo-fi",
                "licence": "CC0",
                "performance": {
                    "24h": {"mean_views": 1000, "mean_reach": 800},
                    "72h": {"mean_views": None, "mean_reach": None},
                },
            },
            {
                "track_id": "b" * 16,
                "title": "Acoustic",
                "licence": "CC0",
                "performance": {
                    "24h": {"mean_views": 100, "mean_reach": 80},
                },
            },
        ]
        draft = drafts._with_render({"reel": {}}, {}, {}, {"pick": "agents", "mix": "under"}, tracks)
        self.assertEqual(draft["reel"]["music"]["track_id"], "a" * 16)
        tracks[0]["performance"]["24h"] = {"mean_views": 0, "mean_reach": 0}
        tracks[0]["performance"]["72h"] = {"mean_views": 10000, "mean_reach": 8000}
        draft = drafts._with_render({"reel": {}}, {}, {}, {"pick": "agents", "mix": "under"}, tracks)
        self.assertEqual(draft["reel"]["music"]["track_id"], "b" * 16)

    def test_fallback_prefers_24h_and_uses_72h_only_when_24h_is_missing(self):
        tracks = [
            {
                "track_id": "a" * 16,
                "title": "Lo-fi",
                "licence": "CC0",
                "performance": {
                    "24h": {"mean_views": 100, "mean_reach": 80},
                    "72h": {"mean_views": 1000, "mean_reach": 800},
                },
            },
            {
                "track_id": "b" * 16,
                "title": "Acoustic",
                "licence": "CC0",
                "performance": {
                    "24h": {"mean_views": 200, "mean_reach": 160},
                },
            },
        ]
        for expected in ("b" * 16, "a" * 16):
            draft = drafts._with_render({"reel": {}}, {}, {}, {"pick": "agents", "mix": "under"}, tracks)
            self.assertEqual(draft["reel"]["music"]["track_id"], expected)
            tracks[0]["performance"].pop("24h", None)

    def test_missing_photo_choice_defaults_to_agents_and_replace(self):
        track_id = music.save(self.conn, self.media, "Warm coffee lo-fi", "CC0", "", MP3, NOW)
        jpeg = photo()
        uploads.save(self.conn, self.media, "Coffee", [(jpeg, jpeg)], NOW, reel=True)
        ai = ReelAI({**REEL, "music_track": track_id})
        with patch.object(ai, "chat_json", wraps=ai.chat_json) as chat:
            draft = drafts.generate_from_upload(self.conn, ai, NOW, uploads.oldest(self.conn), self.media, as_reel=True)
        self.assertEqual(
            draft["reel"]["music"],
            {"track_id": track_id, "mix": "replace", "title": "Warm coffee lo-fi", "licence": "CC0"},
        )
        payload = json.loads(chat.call_args_list[0].args[1])
        self.assertEqual(
            payload["music_tracks"], [{"track_id": track_id, "title": "Warm coffee lo-fi", "performance": {}}]
        )
        self.assertIn("top-performing food/coffee reel audio", ai.calls[0])
        self.assertIn("strongest past reel views/reach", ai.calls[0])

    def test_clips_default_to_agents_under_and_explicit_off_survives(self):
        track_id = music.save(self.conn, self.media, "Lo-fi", "CC0", "", MP3, NOW)
        clip_id = self.waiting_clip()
        for choice in (None, music.choice("off", "under")):
            with self.subTest(choice=choice):
                self.conn.execute("UPDATE clips SET music=?", (json.dumps(choice) if choice else None,))
                clip = self.conn.execute("SELECT * FROM clips WHERE clip_id=?", (clip_id,)).fetchone()
                ai = ReelAI({**REEL, "music_track": track_id})
                draft = drafts.generate_from_clip(self.conn, ai, NOW, clip, rendered=True, media_dir=self.media)
                if choice:
                    self.assertNotIn("music", draft["reel"])
                    self.assertNotIn("music_tracks", ai.calls[0])
                else:
                    self.assertEqual(draft["reel"]["music"]["mix"], "under")
                    self.assertEqual(draft["reel"]["music"]["track_id"], track_id)

    def test_photo_off_empty_library_and_unusable_tracks_do_not_add_music(self):
        jpeg = photo()
        upload_id = uploads.save(self.conn, self.media, "Coffee", [(jpeg, jpeg)], NOW, reel=True)
        track_id = None
        for scenario in ("empty", "off", "missing_file", "malformed_choice"):
            with self.subTest(scenario=scenario):
                if scenario == "off":
                    track_id = music.save(self.conn, self.media, "Lo-fi", "CC0", "", MP3, NOW)
                    self.conn.execute("UPDATE uploads SET music=?", (json.dumps(music.choice("off", "replace")),))
                elif scenario == "missing_file":
                    music.usable_file(self.conn, self.media, track_id).unlink()
                    self.conn.execute("UPDATE uploads SET music=NULL")
                elif scenario == "malformed_choice":
                    self.conn.execute("UPDATE uploads SET music='not json'")
                upload = self.conn.execute("SELECT * FROM uploads WHERE upload_id=?", (upload_id,)).fetchone()
                ai = ReelAI({**REEL, "music_track": track_id})
                draft = drafts.generate_from_upload(self.conn, ai, NOW, upload, self.media, as_reel=True)
                self.assertNotIn("music", draft["reel"])
                self.assertNotIn("music_tracks are", ai.calls[0])

    def test_performance_is_track_specific_and_separated_by_checkpoint(self):
        track_id = music.save(self.conn, self.media, "Lo-fi", "CC0", "", MP3, NOW)
        other_id = music.save(self.conn, self.media, "Acoustic", "CC0", "", MP3, NOW)
        for number, (track, views, reach, checkpoint, captured) in enumerate(
            [
                (track_id, 100, 80, "24h", "today"),
                (track_id, 300, None, "24h", "today"),
                (track_id, 900, 700, "72h", "today"),
                (other_id, 0, 0, "24h", "today"),
                (track_id, 9999, 9999, "24h", None),
            ]
        ):
            draft = drafts.normalise_reel_draft(REEL, 10)
            draft.update(draft_id=f"{number:016x}", reel={"music": {"track_id": track}})
            storage.insert_draft(self.conn, draft, "2026-11-12", NOW.isoformat())
            self.conn.execute(
                "INSERT INTO post_reports(media_id,checkpoint,draft_id,views,reach,captured_at) VALUES(?,?,?,?,?,?)",
                (str(number), checkpoint, draft["draft_id"], views, reach, captured),
            )
        by_id = {t["track_id"]: t for t in music.for_reel(self.conn, self.media)}
        self.assertEqual(
            by_id[track_id]["performance"]["24h"],
            {"reels": 2, "mean_views": 200, "views_samples": 2, "mean_reach": 80, "reach_samples": 1},
        )
        self.assertEqual(by_id[track_id]["performance"]["72h"]["mean_views"], 900)
        self.assertEqual(by_id[other_id]["performance"]["24h"]["mean_views"], 0)
        clip_id = self.waiting_clip()
        clip = self.conn.execute("SELECT * FROM clips WHERE clip_id=?", (clip_id,)).fetchone()
        ai = ReelAI({**REEL, "music_track": other_id})
        with patch.object(ai, "chat_json", wraps=ai.chat_json) as chat:
            draft = drafts.generate_from_clip(self.conn, ai, NOW, clip, rendered=True, media_dir=self.media)
        payload = json.loads(chat.call_args_list[0].args[1])
        self.assertEqual(
            {t["track_id"]: t for t in payload["music_tracks"]}[track_id]["performance"], by_id[track_id]["performance"]
        )
        self.assertEqual(draft["reel"]["music"]["track_id"], other_id)
        for invalid in (None, "f" * 16, [track_id]):
            draft = drafts.generate_from_clip(
                self.conn, ReelAI({**REEL, "music_track": invalid}), NOW, clip, rendered=True, media_dir=self.media
            )
            self.assertEqual(draft["reel"]["music"]["track_id"], track_id)

    def test_photos_marked_as_a_reel_become_a_rendered_reel_with_the_agents_music_pick(self):
        track_id = music.save(self.conn, self.media, "Lo-fi", "YouTube Audio Library: no attribution", "", MP3, NOW)
        jpeg = photo()
        upload_id = uploads.save(self.conn, self.media, "Bar Italia, 9/10", [(jpeg, jpeg)] * 3, NOW,
                                 reel=True, music={"pick": "agents", "mix": "under"})  # fmt: skip
        upload = uploads.oldest(self.conn)
        ai = ReelAI({**REEL, "music_track": track_id})
        draft = drafts.generate_from_upload(self.conn, ai, NOW, upload, self.media, as_reel=True)
        self.assertIn("9-second reel", ai.calls[0])
        self.assertIn("music_track", ai.calls[0])
        self.assertEqual(draft["kind"], "reel")
        reel = draft["reel"]
        self.assertEqual((reel["source"], reel["photos"], reel["render"], reel["seconds"]), ("photos", 3, "pending", 9))
        self.assertEqual(reel["music"]["track_id"], track_id)
        draft_id = drafts.store_upload_draft(self.conn, draft, upload, self.media, NOW)
        self.assertTrue((self.media / draft_id / "slide-3.jpg").is_file())
        self.assertFalse(uploads.is_waiting(self.conn, upload_id))

    def test_agents_cannot_pick_a_track_outside_the_library(self):
        jpeg = photo()
        uploads.save(
            self.conn, self.media, "x", [(jpeg, jpeg)], NOW, reel=True, music={"pick": "agents", "mix": "under"}
        )
        draft = drafts.generate_from_upload(
            self.conn,
            ReelAI({**REEL, "music_track": "f" * 16}),
            NOW,
            uploads.oldest(self.conn),
            self.media,
            as_reel=True,
        )
        self.assertNotIn("music", draft["reel"])
        odd = drafts.generate_from_upload(
            self.conn, ReelAI({**REEL, "music_track": ["x"]}), NOW, uploads.oldest(self.conn), self.media, as_reel=True
        )
        self.assertNotIn("music", odd["reel"])

    def test_a_rendered_clip_keeps_its_source_and_gets_the_agents_trim(self):
        clip_id = self.waiting_clip()
        clip = self.conn.execute("SELECT * FROM clips WHERE clip_id=?", (clip_id,)).fetchone()
        ai = ReelAI({**REEL, "in_seconds": 1, "out_seconds": 8})
        draft = drafts.generate_from_clip(self.conn, ai, NOW, clip, rendered=True)
        self.assertIn("burned into", ai.calls[0])
        self.assertEqual(draft["reel"]["trim"], {"in": 1.0, "out": 8.0})
        self.assertEqual(drafts._trim({"in_seconds": 5, "out_seconds": 6}, 10.0), {"in": 0.0, "out": 10.0})
        draft_id = drafts.store_clip_draft(self.conn, draft, clip, self.media, NOW + timedelta(minutes=1))
        reel = json.loads(self.conn.execute("SELECT reel FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()[0])
        self.assertEqual((reel["source_file"], reel["render"]), ("source.mp4", "pending"))
        self.assertNotIn("file", reel)
        self.assertTrue((self.media / draft_id / "source.mp4").is_file())


class MusicApiTests(Fixture):
    def test_form_defaults_and_off_are_stored_for_photos_and_clips(self):
        client = self.client()
        for choice in (None, "off"):
            with self.subTest(choice=choice):
                raw, content_type = form(files=[photo()])
                fields = [("reel", "1")] + ([("music", choice)] if choice else [])
                extra = "".join(
                    f'--XyZ\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in fields
                ).encode()
                response = self.post(client, "/api/v1/instagram/uploads", extra + raw, content_type)
                self.assertEqual(response.status, 200, response.body)
                with sqlite3.connect(self.db) as conn:
                    picked = json.loads(
                        conn.execute(
                            "SELECT music FROM uploads WHERE upload_id=?", (response.json()["upload_id"],)
                        ).fetchone()[0]
                    )
                self.assertEqual(picked, {"pick": "off"} if choice else {"pick": "agents", "mix": "replace"})
                body = {"note": "Coffee", "size": 10000, **({"music": choice} if choice else {})}
                response = client.request(
                    "POST", "/api/v1/instagram/clips", body=body, headers={"X-CSRF-Token": client.csrf}
                )
                self.assertEqual(response.status, 200, response.body)
                with sqlite3.connect(self.db) as conn:
                    picked = json.loads(
                        conn.execute(
                            "SELECT music FROM clips WHERE clip_id=?", (response.json()["clip_id"],)
                        ).fetchone()[0]
                    )
                self.assertEqual(picked, {"pick": "off"} if choice else {"pick": "agents", "mix": "under"})

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for form checks")
    def test_forms_preselect_agents_and_submit_explicit_off(self):
        script = (Path(__file__).resolve().parents[1] / "command_centre/static/app.js").read_text()
        function = script[script.index("  function musicChoice(") : script.index("  function uploadCard(")]
        harness = (
            """
const assert = require('node:assert/strict');
function el(tag, attrs, ...children) {
  const node = {tag, attrs, children};
  if (tag === 'select') node.value = (children.find(c => c && 'selected' in c.attrs) || children[0]).attrs.value;
  return node;
}
"""
            + function
            + """
for (const music of [[], [{track_id: '123', title: 'Lo-fi'}]]) {
  for (const clips of [false, true]) {
    const form = musicChoice({music}, 'test', clips);
    assert.deepEqual(form.values(), {music: 'agents', mix: clips ? 'under' : 'replace'});
    const select = form.node.children.find(c => c && c.tag === 'select');
    select.value = 'off';
    assert.equal(form.values().music, 'off');
  }
}
"""
        )
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def client(self):
        settings = make_settings(
            self.root, permission_ceiling=Permission.RECOMMEND, instagram_db=self.db, instagram_media=self.media
        )
        client = Client(make_app(settings, clock=Clock()))
        client.login()
        return client

    def track_form(self, licence="CC0", data=MP3, boundary="XyZ"):
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
            for k, v in (("title", "Lo-fi"), ("licence", licence), ("source", "pixabay.com"))
        ]
        head = f'--{boundary}\r\nContent-Disposition: form-data; name="track"; filename="t.mp3"\r\n\r\n'
        parts.append(head.encode() + data + b"\r\n")
        return b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"

    def post(self, client, path, raw, content_type):
        headers = {"X-CSRF-Token": client.csrf}
        return client.request("POST", path, raw=raw, content_type=content_type, headers=headers)

    def test_owner_adds_lists_uses_and_deletes_a_licensed_track(self):
        client = self.client()
        refused = self.post(client, "/api/v1/instagram/music", *self.track_form(licence=""))
        self.assertEqual((refused.status, refused.json()["error"]), (400, "licence_required"))
        added = self.post(client, "/api/v1/instagram/music", *self.track_form())
        self.assertEqual(added.status, 200, added.body)
        track_id = added.json()["track_id"]
        data = client.request("GET", "/api/v1/instagram/drafts").json()
        self.assertEqual([(t["track_id"], t["licence"]) for t in data["music"]], [(track_id, "CC0")])
        raw, content_type = form(files=[photo()])
        extra = "".join(
            f'--XyZ\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in
            (("reel", "1"), ("music", track_id), ("mix", "under"))
        ).encode()  # fmt: skip
        response = self.post(client, "/api/v1/instagram/uploads", extra + raw, content_type)
        self.assertEqual(response.status, 200, response.body)
        self.assertTrue(response.json()["reel"])
        row = sqlite3.connect(self.db).execute("SELECT reel, music FROM uploads").fetchone()
        self.assertEqual((row[0], json.loads(row[1])["track_id"]), (1, track_id))
        unknown = extra.replace(track_id.encode(), b"f" * 16) + raw
        self.assertEqual(self.post(client, "/api/v1/instagram/uploads", unknown, content_type).status, 400)
        gone = client.request(
            "POST", "/api/v1/instagram/music/delete", body={"track_id": track_id}, headers={"X-CSRF-Token": client.csrf}
        )
        self.assertEqual(gone.status, 200, gone.body)
        self.assertEqual(client.request("GET", "/api/v1/instagram/drafts").json()["music"], [])
