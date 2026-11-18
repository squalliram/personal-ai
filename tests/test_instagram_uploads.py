import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from command_centre_fixtures import Client, Clock, make_app, make_settings
from test_instagram_drafts import MORNING, FakeAI, raw_draft

import notifications
from command_centre.security import Permission
from instagram_agent import cli, drafts, storage, uploads
from instagram_agent.openai_client import OpenAIError

try:
    from PIL import Image
except ImportError:
    Image = None

NOTE = "Dishoom Kings Cross, lamb raan, 8/10"
GPS_TAG, ORIENTATION_TAG = 0x8825, 0x0112


def photo(size=(800, 1000), fmt="JPEG", *, gps=False, orientation=None, mode="RGB", comment=None):
    image = Image.new(mode, size, (200, 80, 40, 255) if mode == "RGBA" else (200, 80, 40))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"
    if gps:
        exif[GPS_TAG] = {1: "N", 2: (51.0, 31.0, 48.0)}
    if orientation:
        exif[ORIENTATION_TAG] = orientation
    out = io.BytesIO()
    extra = {"comment": comment} if comment else {}
    image.save(out, fmt, exif=exif.tobytes(), **extra) if fmt == "JPEG" else image.save(out, fmt)
    return out.getvalue()


def form(note=NOTE, files=(), boundary="XyZ"):
    parts = (
        []
        if note is None
        else [f'--{boundary}\r\nContent-Disposition: form-data; name="note"\r\n\r\n{note}\r\n'.encode()]
    )
    for n, data in enumerate(files, 1):
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="photos"; filename="p{n}.jpg"\r\n'
            "Content-Type: image/jpeg\r\n\r\n"
        )
        parts.append(head.encode() + data + b"\r\n")
    return b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


class SniffTests(unittest.TestCase):
    def test_type_comes_from_magic_bytes(self):
        self.assertEqual(uploads.kind(b"\xff\xd8\xff\xe0rest"), "jpeg")
        self.assertEqual(uploads.kind(b"\x89PNG\r\n\x1a\nrest"), "png")
        self.assertEqual(uploads.kind(b"\x00\x00\x00\x18ftypheic...."), "heif")
        for data in (b"GIF89a", b"<svg>", b"%PDF-1.7", b""):
            self.assertIsNone(uploads.kind(data))

    def test_notes_are_short_single_lines(self):
        self.assertEqual(uploads.clean_note("  Dishoom,\n lamb raan  "), "Dishoom, lamb raan")
        for bad in ("", "   ", "x" * 301, "a\x00b", None, 5):
            with self.assertRaises(uploads.PhotoError):
                uploads.clean_note(bad)

    def test_limits_apply_before_decoding(self):
        for photos, code in (([], "photo_count"), ([b"\xff\xd8\xff"] * 11, "photo_count")):
            with self.assertRaises(uploads.PhotoError) as caught:
                uploads.prepare(photos)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(uploads.PhotoError) as caught:
            uploads.prepare([b"\xff\xd8\xff" + b"0" * uploads.MAX_PHOTO_BYTES])
        self.assertEqual(caught.exception.code, "photo_too_large")
        with self.assertRaises(uploads.PhotoError) as caught:
            uploads.prepare([b"GIF89a" + b"0" * 64])
        self.assertEqual(caught.exception.code, "unsupported_photo")


@unittest.skipUnless(Image, "Pillow is not installed")
class PrepareTests(unittest.TestCase):
    def decoded(self, data):
        return Image.open(io.BytesIO(data))

    def test_portrait_photos_become_clean_4_5_jpegs(self):
        [(slide, thumb)] = uploads.prepare([photo((900, 1600), gps=True, comment=b"home address")])
        image = self.decoded(slide)
        self.assertEqual((image.format, image.size), ("JPEG", uploads.PORTRAIT))
        self.assertEqual(dict(image.getexif()), {})
        self.assertNotIn(b"Exif", slide)
        self.assertNotIn(b"PhoneMaker", slide + thumb)
        self.assertNotIn(b"home address", slide + thumb)
        self.assertNotIn("comment", image.info)
        self.assertNotIn("icc_profile", image.info)
        self.assertEqual(self.decoded(thumb).size, (540, 675))

    def test_the_first_photo_sets_one_shape_for_the_carousel(self):
        prepared = uploads.prepare([photo((1600, 900)), photo((900, 1600), fmt="PNG")])
        self.assertEqual([self.decoded(s).size for s, _ in prepared], [uploads.SQUARE, uploads.SQUARE])

    def test_orientation_is_applied_then_dropped(self):
        [(slide, _)] = uploads.prepare([photo((1600, 900), orientation=6)])  # stored sideways, shown portrait
        image = self.decoded(slide)
        self.assertEqual(image.size, uploads.PORTRAIT)
        self.assertNotIn(ORIENTATION_TAG, image.getexif())

    def test_transparent_png_is_flattened(self):
        [(slide, _)] = uploads.prepare([photo((1000, 1000), fmt="PNG", mode="RGBA")])
        self.assertEqual(self.decoded(slide).mode, "RGB")

    def test_disguised_or_broken_files_are_refused(self):
        cases = {
            "unreadable_photo": [b"\xff\xd8\xff" + b"not really a jpeg" * 10, photo(fmt="PNG")[:60]],
            "heic_unsupported": [b"\x00\x00\x00\x18ftypheic" + b"0" * 64],
            "unsupported_photo": [b"<?php echo 1; ?>"],
        }
        for code, items in cases.items():
            for data in items:
                with self.subTest(code=code), self.assertRaises(uploads.PhotoError) as caught:
                    uploads.prepare([data])
                self.assertEqual(caught.exception.code, code)

    def test_png_magic_with_jpeg_inside_is_refused(self):
        with self.assertRaises(uploads.PhotoError):
            uploads.prepare([b"\x89PNG\r\n\x1a\n" + photo()])

    def test_huge_dimensions_are_refused_before_decoding(self):
        with patch.object(uploads, "MAX_PIXELS", 100):
            with self.assertRaises(uploads.PhotoError) as caught:
                uploads.prepare([photo((20, 20))])
        self.assertEqual(caught.exception.code, "photo_too_large")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db, self.media = self.root / "instagram.sqlite3", self.root / "drafts"
        self.conn = storage.connect(self.db)
        self.addCleanup(self.conn.close)

    def add_upload(self, note=NOTE, photos=2, when="2026-11-11T09:00:00+00:00"):
        jpeg = b"\xff\xd8\xff" + note.encode()
        now = datetime.fromisoformat(when)
        return uploads.save(self.conn, self.media, note, [(jpeg + b"slide", jpeg + b"thumb")] * photos, now)


@unittest.skipUnless(Image, "Pillow is not installed")
class UploadApiTests(Fixture):
    def client(self, permission=Permission.RECOMMEND):
        settings = make_settings(
            self.root, permission_ceiling=permission, instagram_db=self.db, instagram_media=self.media
        )
        client = Client(make_app(settings, clock=Clock()))
        client.login()
        return client

    def upload(self, client, files=None, note=NOTE, csrf=True, **kwargs):
        raw, content_type = form(note, [photo()] if files is None else files)
        headers = {"X-CSRF-Token": client.csrf} if csrf else {}
        return client.request(
            "POST", "/api/v1/instagram/uploads", raw=raw, content_type=content_type, headers=headers, **kwargs
        )

    def test_upload_is_cleaned_queued_and_shown(self):
        client = self.client()
        response = self.upload(client, [photo(gps=True), photo()])
        self.assertEqual(response.status, 200, response.body)
        upload_id = response.json()["upload_id"]
        folder = self.media / "uploads" / upload_id
        self.assertEqual(
            sorted(p.name for p in folder.iterdir()), [f"{k}-{n}.jpg" for k in ("photo", "thumb") for n in (1, 2)]
        )
        self.assertTrue(all(b"Exif" not in p.read_bytes() for p in folder.iterdir()))
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        data = client.request("GET", "/api/v1/instagram/drafts").json()
        self.assertTrue(data["uploads_on"])
        self.assertEqual(
            data["uploads"],
            [
                {
                    "upload_id": upload_id,
                    "created_at": data["uploads"][0]["created_at"],
                    "note": NOTE,
                    "photos": 2,
                    "reel": False,
                }
            ],
        )
        thumb = client.request("GET", "/api/v1/instagram/upload-photo", query=f"upload={upload_id}&n=1")
        self.assertEqual((thumb.status, thumb.body[:3]), (200, b"\xff\xd8\xff"))
        missing = client.request("GET", "/api/v1/instagram/upload-photo", query=f"upload={upload_id}&n=3")
        self.assertEqual(missing.status, 404)
        for query in (f"upload=../{upload_id}&n=1", f"upload={upload_id}&n=0", "upload=x&n=1"):
            self.assertEqual(client.request("GET", "/api/v1/instagram/upload-photo", query=query).status, 400)

    def test_the_first_upload_creates_the_media_folders(self):
        self.media = self.root / "instagram" / "drafts"
        response = self.upload(self.client())
        self.assertEqual(response.status, 200, response.body)
        self.assertTrue((self.media / "uploads" / response.json()["upload_id"] / "photo-1.jpg").is_file())

    def test_upload_needs_recommend_and_csrf(self):
        self.assertEqual(self.upload(self.client(Permission.READ)).status, 403)
        self.assertEqual(self.upload(self.client(), csrf=False).status, 403)
        anonymous = Client(make_app(make_settings(self.root, instagram_db=self.db, instagram_media=self.media)))
        raw, content_type = form(NOTE, [photo()])
        self.assertEqual(
            anonymous.request("POST", "/api/v1/instagram/uploads", raw=raw, content_type=content_type).status, 401
        )
        self.assertIsNone(uploads.oldest(self.conn))

    def test_bad_uploads_are_refused(self):
        client = self.client()
        cases = [
            (dict(files=[]), 400, "photo_count"),
            (dict(files=[photo()] * 11), 400, "photo_count"),
            (dict(note=""), 400, "invalid_note"),
            (dict(note=None), 400, "invalid_note"),
            (dict(files=[b"GIF89a" + b"0" * 32]), 400, "unsupported_photo"),
            (dict(files=[b"\xff\xd8\xff" + b"0" * 32]), 400, "unreadable_photo"),
        ]
        for kwargs, status, code in cases:
            with self.subTest(code=code, kwargs=list(kwargs)):
                response = self.upload(client, **kwargs)
                self.assertEqual((response.status, response.json()["error"]), (status, code))
        json_body = client.request(
            "POST", "/api/v1/instagram/uploads", body={"note": NOTE}, headers={"X-CSRF-Token": client.csrf}
        )
        self.assertEqual(json_body.status, 415)
        raw, content_type = form(NOTE, [photo()])
        extra = raw.replace(b"--XyZ--", b'--XyZ\r\nContent-Disposition: form-data; name="other"\r\n\r\nx\r\n--XyZ--')
        response = client.request(
            "POST",
            "/api/v1/instagram/uploads",
            raw=extra,
            content_type=content_type,
            headers={"X-CSRF-Token": client.csrf},
        )
        self.assertEqual(response.status, 400)
        with patch.object(uploads, "MAX_UPLOAD_BYTES", 100):
            self.assertEqual(self.upload(client).status, 413)
        self.assertIsNone(uploads.oldest(self.conn))

    def test_delete_removes_a_waiting_upload_only(self):
        client = self.client()
        waiting, used = self.add_upload(), self.add_upload(when="2026-11-11T10:00:00+00:00")
        with self.conn:
            self.conn.execute(
                "UPDATE uploads SET status='used', draft_id='0123456789abcdef' WHERE upload_id=?", (used,)
            )

        def delete(upload_id, csrf=True):
            headers = {"X-CSRF-Token": client.csrf} if csrf else {}
            return client.request(
                "POST", "/api/v1/instagram/uploads/delete", body={"upload_id": upload_id}, headers=headers
            )

        self.assertEqual(delete(waiting, csrf=False).status, 403)
        self.assertEqual(delete(waiting).status, 200)
        self.assertFalse((self.media / "uploads" / waiting).exists())
        self.assertEqual(delete(waiting).status, 404)
        self.assertEqual(delete(used).status, 404)
        self.assertEqual(delete("../../etc").status, 400)
        read_only = self.client(Permission.READ)
        response = read_only.request(
            "POST",
            "/api/v1/instagram/uploads/delete",
            body={"upload_id": used},
            headers={"X-CSRF-Token": read_only.csrf},
        )
        self.assertEqual(response.status, 403)

    def test_without_pillow_uploads_are_off(self):
        client = self.client()
        with patch.object(uploads, "available", return_value=False):
            self.assertEqual(self.upload(client).status, 503)
            self.assertFalse(client.request("GET", "/api/v1/instagram/drafts").json()["uploads_on"])


class PhotoAI(FakeAI):
    def __init__(self, photo_drafts, scores, creates=()):
        super().__init__(list(creates), scores)
        self.photo_drafts, self.seen = list(photo_drafts), []

    def chat_json(self, system, user, *, images=()):
        if system.startswith("You are the Create agent") and "owner took the attached photos" in system:
            self.calls.append((system, json.loads(user)))
            self.seen.append(len(images))
            reply = self.photo_drafts.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        return super().chat_json(system, user)


def photo_draft(**overrides):
    return {
        "category": "restaurant",
        "topic": "Lamb raan at Dishoom Kings Cross",
        "caption": "Dishoom Kings Cross, lamb raan, 8/10. Would you order it? Tell me below.",
        "photos": ["Lamb raan on a plate", "The table"],
        **overrides,
    }


class DailyFromUploadTests(Fixture):
    def setUp(self):
        super().setUp()
        self.notes = self.root / "notifications.sqlite3"
        self.env = {
            "OPENAI_API_KEY": "sk-test-secret",
            "INSTAGRAM_VOICE_FILE": str(self.root / "voice.md"),
            "COMMAND_CENTRE_NOTIFICATIONS_DB": str(self.notes),
        }
        (self.root / "voice.md").write_text("Warm, short, first person.")

    def tick(self, ai, now=MORNING):
        with patch.object(cli, "model_client", return_value=ai):
            return cli.daily(self.conn, self.env, self.media, now=now)

    def titles(self):
        conn = notifications.connect(self.notes)
        try:
            return [e["title"] for e in reversed(notifications.recent(conn)[0])]
        finally:
            conn.close()

    def test_the_oldest_upload_becomes_the_days_draft(self):
        newer = self.add_upload("Padella, pici cacio e pepe", when="2026-11-11T12:00:00+00:00")
        oldest = self.add_upload()
        ai = PhotoAI([photo_draft()], [[85]])
        self.assertEqual(self.tick(ai), "ok")
        [row] = self.conn.execute("SELECT * FROM drafts").fetchall()
        self.assertEqual(
            (row["topic"], row["review_score"], row["status"]), ("Lamb raan at Dishoom Kings Cross", 85, "pending")
        )
        self.assertEqual(
            json.loads(row["slides"]),
            [{"text": "Lamb raan on a plate", "file": "slide-1.jpg"}, {"text": "The table", "file": "slide-2.jpg"}],
        )
        self.assertEqual(
            (self.media / row["draft_id"] / "slide-1.jpg").read_bytes(), b"\xff\xd8\xff" + NOTE.encode() + b"slide"
        )
        self.assertEqual(row["scheduled_for"], "2026-11-12T18:00+00:00")
        self.assertFalse((self.media / "uploads" / oldest).exists())
        used = self.conn.execute("SELECT status, draft_id FROM uploads WHERE upload_id=?", (oldest,)).fetchone()
        self.assertEqual(tuple(used), ("used", row["draft_id"]))
        self.assertEqual(uploads.oldest(self.conn)["upload_id"], newer)
        self.assertEqual(ai.seen, [2])
        self.assertEqual(ai.images, 0)
        create, payload = ai.calls[0]
        self.assertIn("only evidence for the venue", create)
        self.assertIn("Warm, short, first person.", create)
        self.assertEqual(payload["owner_note"], NOTE)
        review_system, review_payload = ai.calls[-1]
        self.assertTrue(review_system.startswith("You are the Review agent"))
        self.assertEqual(review_payload["owner_note"], NOTE)
        self.assertEqual(self.titles(), [f'Your photos ("{NOTE}") are now an Instagram draft, posting is manual'])
        self.assertEqual(self.tick(PhotoAI([], [])), "skipped")

    def test_no_upload_falls_back_to_the_ai_draft(self):
        ai = PhotoAI([], [[90]], creates=[[raw_draft("a")]])
        self.assertEqual(self.tick(ai), "ok")
        self.assertEqual(ai.seen, [])
        self.assertEqual(self.conn.execute("SELECT topic FROM drafts").fetchone()[0], "a")
        self.assertEqual(self.titles(), ["1 Instagram draft ready to review, posting is manual"])

    def test_a_model_without_vision_still_drafts_from_the_note(self):
        self.add_upload()
        ai = PhotoAI([OpenAIError("images not supported"), photo_draft()], [[80]])
        self.assertEqual(self.tick(ai), "ok")
        self.assertEqual(ai.seen, [2, 0])

    def test_weak_drafts_are_retried_and_the_best_kept(self):
        self.add_upload()
        ai = PhotoAI([photo_draft(topic="first"), {"category": "nope"}, photo_draft(topic="third")], [[40], [60]])
        self.assertEqual(self.tick(ai), "ok")
        row = self.conn.execute("SELECT topic, review_score FROM drafts").fetchone()
        self.assertEqual(tuple(row), ("third", 60))
        creates = [payload for system, payload in ai.calls if "owner took the attached photos" in system]
        self.assertEqual(creates[1]["feedback_on_weak_drafts"], ["first: note 40"])

    def test_failed_upload_run_keeps_the_upload_for_a_retry(self):
        upload_id = self.add_upload()
        ai = PhotoAI([{"category": "nope"}] * 3, [])
        self.assertEqual(self.tick(ai), "failed")
        self.assertTrue(uploads.is_waiting(self.conn, upload_id))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 0)
        self.assertTrue((self.media / "uploads" / upload_id / "photo-1.jpg").is_file())

    def test_an_unreadable_voice_file_is_a_failed_run_not_a_crash(self):
        upload_id = self.add_upload()
        with patch.object(cli, "_read", side_effect=PermissionError("denied")):
            self.assertEqual(self.tick(PhotoAI([photo_draft()], [[85]])), "failed")
        run = self.conn.execute("SELECT outcome, error FROM daily_runs").fetchone()
        self.assertEqual(tuple(run), ("failed", "PermissionError"))
        self.assertTrue(uploads.is_waiting(self.conn, upload_id))

    def test_an_upload_deleted_mid_run_falls_back_to_ai(self):
        upload_id = self.add_upload()
        real = drafts.generate_from_upload

        def delete_first(conn, *args, **kwargs):
            draft = real(conn, *args, **kwargs)
            uploads.delete(conn, self.media, upload_id)
            return draft

        ai = PhotoAI([photo_draft()], [[85], [90]], creates=[[raw_draft("a")]])
        with patch.object(drafts, "generate_from_upload", delete_first):
            self.assertEqual(self.tick(ai), "ok")
        self.assertEqual([r[0] for r in self.conn.execute("SELECT topic FROM drafts")], ["a"])
        self.assertEqual(len(list(self.media.iterdir())), 2)  # uploads/ and the AI draft

    def test_photo_drafts_need_valid_fields(self):
        self.assertIsNone(drafts.normalise_photo_draft(photo_draft(category="travel"), 2))
        self.assertIsNone(drafts.normalise_photo_draft(photo_draft(caption="Lovely #food"), 2))
        draft = drafts.normalise_photo_draft(photo_draft(photos=["one"]), 3)
        self.assertEqual([s["text"] for s in draft["slides"]], ["one", "Photo 2", "Photo 3"])


if __name__ == "__main__":
    unittest.main()
