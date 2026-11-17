# Plan: Instagram Reels from my own clips

Status: proposal for review. Nothing here is built yet, and this PR changes no code.

My choice: "Start planning reels from my own clips". On @instafoodielove, reels do far better than photos. The best reels got 27k, 12.9k, 9.4k and 9.4k views; photo posts rarely pass 150. This plan adds reels to `instagram_agent` alongside the carousels. It uses the same review, veto window, notifications and Meta's official publishing API, and nothing else. AGENTS.md still applies: no scraping, no unofficial login and no other Instagram writes.

## 1. Uploading clips plus a note

This mirrors the photo-upload flow from the separate PR: the same Command Centre page, permission, CSRF, audit log and storage layout. Only the file type and size limits differ.

- **Where:** the Instagram page gets **New reel**. I pick one clip (or up to 3 that get joined end to end, phase 2 only) and write a short note, for example "Dishoom Covent Garden, lamb raan, halal, worth the queue".
- **Limits checked on upload:** MP4 or MOV, 3 s to 90 s, 300 MB or less (Meta's cap). Anything else is refused with a clear reason. 90 s is my own suggested cap for this account, not a Meta limit.
- **Storage:** private, under `data/instagram/uploads/<draft_id>/`, which is gitignored (see the cron and clean-clone note in `docs/instagram-pipeline.md`). Not under `public_html`.
- **Transport:** the Command Centre API takes JSON only, capped at `MAX_BODY_BYTES = 4096` in `command_centre/app.py`. Video needs its own streamed upload route, sent in chunks (for example 8 MB each) so one slow phone upload doesn't hit a proxy or Passenger timeout. I don't know LiteSpeed's request body limit or Passenger's timeout on this host. Phase 0 measures them.
- **The note is data, not instructions**, the same as benchmark captions. The prompts say so, and the note can't change the schedule, permissions or the publish switch.

## 2. What the agents produce

A reel draft is a new `kind='reel'` row in `drafts`, so the existing queue, Approve/Reject and audit trail keep working. Create writes these fields, using the `ig-reel`, `ig-caption` and `ig-human` rules from the skill pack (`ig-reel` already scores hooks and builds timed beat sheets):

| Field | What it is | Checked by |
| --- | --- | --- |
| Hook | The first 1-2 s: at most 6 words of on-screen text, plus the caption's first line | `hookscore.py`-style rules; Review |
| On-screen text | 2-5 short timed captions (start s, end s, text) taken from the note | Length per beat; no claims missing from the note |
| Caption | Same voice and rules as carousels: no invented prices, offers, hours or venues (#47, #48) | Existing `normalize` and length checks |
| Hashtags | Up to 20, from `trending_hashtags`, weighted towards reels | Existing `clean_hashtags` |
| Cover | A frame time (`thumb_offset`, ms) or a JPEG cover | Section 3 |
| Length | A target length (aim for 7-30 s for food clips) and trim points, if any | Section 3 |

Meta's video limits make Create's job smaller than for carousels: no images to draw and no Cloudflare image neurons. Only a few text calls.

### Processing (ffmpeg), and what I'm not sure about

What the repo shows about the cPanel/CloudLinux host:

- Code runs on CloudLinux alt-python (`/opt/alt/python312`, picked by `scripts/find-python.sh`) inside a CageFS shell. The host can't `pip install`, so dependencies go in as hash-pinned wheels sent over SSH (`deploy_host.py`, `COMMAND_CENTRE_WHEELS_DIR`).
- Nothing in the repo uses or checks for `ffmpeg`/`ffprobe`. **I don't know if either exists on the host**, or if CageFS exposes them to my account. *Update (phase 0 probe, deploy run 44110566222):* `ffmpeg` 4.2.9 is at `/usr/bin/ffmpeg` and `ffprobe` is not found; 18 CPUs, no address-space/data/CPU rlimits, about 2 TB free.
- **Inference, not checked:** CloudLinux LVE limits (CPU, memory, process count) may make re-encoding a 1080p clip slow, or get it killed.

So processing is staged:

- **Phase 1, no ffmpeg:** post my clip as it is. A small pure-Python MP4/MOV box reader checks every limit in Meta's Reels spec (section 3) that can be read from the file's headers: container and edit lists, `moov` position, duration, file size, width, frame rate, video and audio codec, audio sample rate and channels, and average bitrates. Anything it can't read (closed GOP, progressive scan, chroma subsampling) is left to Meta's `status_code`; an `ERROR` there also marks the draft "needs re-export". If the clip fails, the draft is marked "needs re-export" instead of being sent to Meta. The cover is a `thumb_offset` frame that Create picks from the note's timing (default 0). Because nothing can be burned in, the on-screen text is shown on the draft as a script and the caption's first line carries the hook. Simplest, but the reel has no text overlay.
- **Phase 2, with ffmpeg:** if phase 0 finds ffmpeg, or a pinned static build can be shipped like the wheels (checksum-verified, about 80 MB), add:
  - trim to the agent's in and out points
  - join up to 3 clips
  - scale and pad to 1080×1920 (9:16)
  - burn in on-screen text with `drawtext` (needs a bundled font)
  - re-encode to H.264, AAC 48 kHz, `+faststart`
  - export the chosen cover frame as JPEG for `cover_url`

  Each job runs with `nice` under a time limit. If a job fails, the draft stops and asks me to retry the processing or Approve the original. It never falls back to the original by itself, because that would post footage the trim was meant to cut.
- **Optional later:** Workers AI Whisper (`@cf/openai/whisper`) could transcribe the clip's audio so the caption can quote what I say. Llama 4 Scout accepts images, so it could look at a few extracted frames (needs ffmpeg). Both fit in the free neurons, but neither is checked on this account yet.

## 3. Publishing a reel through Meta's official API

Same token, host and IG user as the carousels (system-user token on `graph.facebook.com`, IG user `17841408862128347`). Permissions: `instagram_basic` and `instagram_content_publish` (plus `pages_read_engagement`) for Facebook Login.

Steps, from Meta's docs:

1. **Pre-checks:** the clip passes the spec check below, and `GET /<IG_ID>/content_publishing_limit?fields=quota_usage,config` shows quota left.
2. **Create the container:** `POST /<IG_ID>/media` with `media_type=REELS`, `caption` (caption plus hashtags), `share_to_feed=true`, and either `cover_url` (a public JPEG) or `thumb_offset` (ms; ignored if `cover_url` is set). Then the video, one of:
   - **a) `video_url`:** a public HTTPS URL that Meta cURLs. Same pattern as the slides today: an unguessable folder under `INSTAGRAM_PUBLIC_DIR` (`public_html/ig-media`), checked to be reachable, removed afterwards. The file must stay until processing finishes, not just until the call returns.
   - **b) Resumable upload (recommended):** `upload_type=resumable` on the container, then `POST https://rupload.facebook.com/ig-api-upload/<version>/<container_id>` with `Authorization: OAuth <token>`, `offset: 0`, `file_size: <bytes>` and the file as the body. The video never goes into `public_html`. A JPEG `cover_url` still needs the public folder; `thumb_offset` doesn't. `GraphClient._url` only allows one origin, so this needs a second, rupload-only client with the same no-token-in-errors rules.
3. **Poll `GET /<container_id>?fields=status_code`** until `FINISHED` (other values: `IN_PROGRESS`, `ERROR`, `EXPIRED`, `PUBLISHED`). Meta recommends once a minute for at most 5 minutes. Today's carousel loop polls 10 times, every 3 s, so reels need their own loop: up to 5 polls a minute apart, then hand over to the next 15-minute cron tick instead of holding the lock. Containers expire after 24 h.
4. **Publish:** `POST /<IG_ID>/media_publish` with `creation_id`. **Never retried**, with the same `started` / "check Instagram" crash handling as `publish.py`.
5. **Confirm:** read back the `permalink` and `media_product_type` (published reels report `media_type=VIDEO`; `media_product_type=REELS` is what identifies a reel).

Meta's limits:

- **Reel video:** MOV or MP4, no edit lists, `moov` atom at the front. H.264 or HEVC, progressive, closed GOP, 4:2:0. 23-60 fps, at most 1920 px wide. 9:16 recommended. Video at most 25 Mbps VBR; audio AAC, at most 48 kHz, mono or stereo, 128 kbps. 3 s to 15 min, at most 300 MB.
- **Cover:** JPEG, at most 8 MB, sRGB. Cropped to the middle 9:16 for Reels and the middle 1:1 in the feed.
- **Reels can't go in carousels.** Music tagging only works with original audio, so **posting through the API can't add licensed Instagram music**. Copyrighted music already in my clip can get the reel muted or blocked. `copyright_check_status` on the container reports matches.
- **Rate limits:** 100 API-published posts per rolling 24 h, and 400 containers per 24 h. The carousel section of the same page says 50, so the docs disagree. At one post a day this doesn't matter, but the code should still check `content_publishing_limit` first.
- `share_to_feed=true` lets the reel show in both Feed and the Reels tab. Meta says neither value guarantees a place in the Reels tab.
- Optional, off by default: `collaborators` (up to 3 usernames), `location_id` (a Page with location data), `trial_params` (trial reels shown to non-followers first) and `audio_name`. `is_ai_generated` doesn't apply, because the footage is mine.

Docs (checked 2026-11-17):

- Content publishing guide: https://developers.facebook.com/docs/instagram-platform/content-publishing
- `POST /<IG_ID>/media` reference, with the Reels and cover specs and `share_to_feed`, `cover_url` and `thumb_offset`: https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media
- IG Container (`status_code`, `copyright_check_status`): https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-container
- Publishing limit: https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/content_publishing_limit
- Media insights: https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-media/insights

## 4. How reels fit the existing pipeline

- **Review scoring:** the same 0-100 Review and `PASS_SCORE = 70`, with up to 3 rounds. For reels the reviewer gets hook, on-screen text, caption, hashtags and note. It compares them with my top reels and benchmark `VIDEO` posts by views, not with carousels. A failing spec check is a hard fail, whatever the score.
- **Veto window:** unchanged. A reel posts at `max(scheduled_for, created_at + 1 hour)` if I approved it, or it's pending and scored 70+. Reject before then stops it. More than 3 h late means it's skipped. Open decision 2 covers whether my own footage should need **Approve** instead.
- **Notifications:** all through `notifications.notify("instagram", outcome, title, link)`, so they show in the Command Centre Inbox and on Telegram:
  - "Reel draft ready: <topic>, posts at HH:MM" when drafted
  - "Clip needs re-export: <reason>" when the spec check fails
  - "Reel is live: <topic>" with the permalink
  - a failure for every failed processing or publish attempt, linking to the draft
- **Schedule:** one post a day. `main` currently drafts 3 carousels a day (`DAILY_DRAFTS = 3`) at 3 slots, so this needs deciding (open decision 1). The proposal: when an uploaded clip's reel is ready, it takes that day's best slot from `publish_slots`, and the carousel for that slot is dropped, or moved if it was approved. Carousels fill days with no clip. Clips wait in a queue, oldest first, one a day.
- **Analytics:**
  - Add `media_product_type` to `MEDIA_FIELDS` so reels can be told apart from other videos.
  - Keep `views`, `reach`, `saved`, `shares`, `likes`, `comments` and `total_interactions` (all valid for reels).
  - For reels only, add `ig_reels_avg_watch_time`, `ig_reels_video_view_total_time` and `reels_skip_rate`, stored in new nullable `post_metrics` columns.
  - "Plays": the current insights page doesn't list `plays`; `views` ("times IG Media has been played") is the count to use.
  - Reels get their own score, (views, saves and shares per reach) plus watch time, so they don't distort the carousel `best_hours` and examples. An approved reel that does well becomes a weighted example for future hooks.

## 5. Phased rollout

| Phase | What ships | Done when |
| --- | --- | --- |
| 0. Probe (read-only) | `probe_host.py` reports `ffmpeg`/`ffprobe` paths and versions, free disk and quota, and the LVE limits it can read. I test one 50 MB upload to find LiteSpeed's body limit | Facts recorded in this doc |
| 1. Post clips as they are | Chunked upload route; `kind='reel'` drafts; Create, Tags and Review for reels; spec check; resumable publish with `thumb_offset`; notifications; reel analytics fields | One reel posted from a real clip after a manual Approve |
| 2. Processing | ffmpeg (host or pinned static build): trim, join, 9:16, burned-in text, H.264/AAC re-encode, JPEG `cover_url` | 3 processed reels post with no re-export |
| 3. Learn | Reel-specific scoring and examples; reel and carousel slot mix; optional Whisper transcript | A week of reels with metrics on the Instagram page |

Each phase is its own PR, reviewed by CI and CodeRabbit. Reels sit behind a new `INSTAGRAM_REELS=1` switch (off by default), on top of the existing `INSTAGRAM_AUTO_PUBLISH` kill switch.

## 6. Risks

- **Music copyright:** trending audio can't be added through the API, and copyrighted music in my clips can get the reel muted. Mitigation: check `copyright_check_status` before publishing, and turn on a "strip audio" option once ffmpeg is in.
- **Host limits:** big uploads and re-encoding may hit LiteSpeed body limits, Passenger timeouts, disk quota or LVE limits. Mitigation: chunked upload, processing in the cron job rather than the web request, and phase 0 measurements.
- **Wrong facts:** an agent could put the wrong venue, price or halal claim over my footage. Mitigation: the existing no-invention rules (#47, #48), with facts limited to my note.
- **Public exposure:** `video_url` would put my raw clip in `public_html` for minutes. Mitigation: resumable upload.
- **Double posting:** mitigation: same never-retry `media_publish` and `started` recovery as carousels.
- **Disk:** clips build up. Mitigation: delete uploads 7 days after publish or reject.
- **Token:** the system-user token is the only working one (see `docs/instagram-pipeline.md`). Uploading to rupload with it isn't checked yet and is tested in phase 1.

## 7. Decisions for me (the owner)

1. **Schedule:** a reel replaces that day's carousel (proposed), or reels post on top of carousels (2 posts that day)?
2. **Approval:** my own footage needs **Approve** before it posts (safer, proposed for phase 1), or the 1-hour veto with auto-post at 70+, the same as carousels?
3. **ffmpeg:** OK to ship a pinned static ffmpeg build to the host if it isn't there? Otherwise phase 2 is limited to what the host has.
4. **Clip rules:** maximum length (90 s proposed), raw or already-edited clips, and keep my original audio or strip it by default?
5. **Extras:** turn on `share_to_feed` (proposed yes), `collaborators` for venues, `location_id`, or trial reels?

## 8. Effort estimate (rough)

| Phase | Estimate |
| --- | --- |
| 0. Probe | 0.5 day |
| 1. Post clips as they are | 2-3 days (about one Devin session), mostly the upload route, spec check and resumable publish with tests |
| 2. ffmpeg processing | 2 days if ffmpeg exists or a static build works under CageFS; up to a week if LVE limits force a different approach |
| 3. Learn | 1-2 days |

The total is about 1-1.5 weeks of work, plus waiting on real clips and on reels' metrics.
