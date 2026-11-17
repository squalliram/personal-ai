# Instagram pipeline for @instafoodielove

A daily pipeline on the hosting server that drafts food, coffee and restaurant carousels, shows them in the Command Centre where I can reject them, publishes the rest at the best time and learns from how they perform.

| Step | Agent | Status |
| --- | --- | --- |
| 1 | **Analytics**: own posts, daily insights, follower online hours | Done (read-only) |
| 2 | **Benchmarks**: accounts I follow, ranked by followers and engagement; their best recent posts and hashtags | Done (read-only) |
| 3 | **Create**: 1 carousel a day by 12:00, in my voice, using the `/ig-*` skill rules. Text and images from Cloudflare Workers AI (free) or OpenAI | Done |
| 4 | **Tags**: up to 20 per post, mostly from `trending_hashtags` | Done |
| 5 | **Review**: scores each draft against `top_benchmark_posts`; under 70 goes back to Create with notes, up to 3 rounds | Done |
| 6 | **Queue**: Instagram page in the Command Centre with **Approve** and **Reject**; decisions feed the memory | Done |
| 7 | **Publish**: drafts that passed review (or that I approved) post at their slot unless I reject them first. Off until the Meta token and user ID are set | Built, waiting for the token |
| 8 | **DMs**: drafted replies, sent when I press send | Later, needs Meta app review |

Step 7 is the only Instagram write. Steps 1-2 only call `GET` endpoints, and all three are skipped while there's no Meta token. The drafts then use the voice profile, past decisions and general food/coffee/restaurant knowledge.

## Requirements (optional, for steps 1-2)

- @instafoodielove is a Business or Creator account linked to a Facebook Page.
- A Meta app with the permissions that Meta's Business Discovery and Insights docs list (including `instagram_basic` and `instagram_manage_insights`), and a long-lived user access token.
- `INSTAGRAM_ACCESS_TOKEN` and `INSTAGRAM_USER_ID` in `.env` (see `.env.example`). `META_GRAPH_VERSION` defaults to `v27.0`.

## Commands

```bash
# Add benchmark accounts from Instagram's export:
# Accounts Center → Your information and permissions → Download your information →
# Followers and following, JSON → connections/followers_and_following/following.json
python3 -m instagram_agent import-following following.json

# Fetch own posts, insights, follower hours and up to 50 benchmark accounts
python3 -m instagram_agent sync

# Rankings, top posts, trending hashtags, best London hours and learned examples as JSON
python3 -m instagram_agent report
```

Data goes to `data/instagram.sqlite3` (ignored by Git).

## Daily drafts

```bash
python3 -m instagram_agent daily          # one tick; safe to run as often as you like
./scripts/install-instagram-daily.sh      # one marked cron entry, every 15 minutes (the deploy runs this)
```

On the server, the deploy does this for you after every merge to `main` (`Configure Instagram daily drafts` in `deploy-dashboard.yml`). It writes the `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` GitHub Actions secrets into the server's `.env` with `python3 -m instagram_agent.env_install` (keys go over stdin, the file stays `0600`, other lines are kept), then installs the cron entry.

From 09:00 London time, the first tick of the day:

1. Syncs analytics and benchmarks (when the Meta token is set). If Instagram is down, it drafts from the data it already has.
2. **Create** writes carousels in the voice from `INSTAGRAM_VOICE_FILE` (default `agents/instagram-voice.md`), following the `ig-carousel`, `ig-caption` and `ig-human` rules from `INSTAGRAM_SKILLS_DIR` when the skill pack is installed. Topics are limited to food, coffee and restaurants. Approved examples that performed well count for more; rejected topics and the last 14 days' topics are avoided.
3. **Tags** adds up to 20 hashtags per draft.
4. **Review** scores each draft 0-100. Drafts under 70 go back to Create with the reviewer's notes, up to 3 rounds. If none passes, the best of the rest still goes in the queue, labelled with its score.
5. The image model draws every slide at 1024×1280 (4:5 portrait). OpenAI's image model also draws the slide's short text onto it. Cloudflare's FLUX.2 klein garbles lettering, so with Cloudflare the slides are clean photos and the caption carries the words. A slide image that fails gets one more try; if it still fails, that draft is dropped and the reason is recorded. Cloudflare returns JPEGs and OpenAI returns PNGs; each is saved in the format it arrives in.
6. The draft gets a publish slot: the best audience hour from `best_hours`, from 12:00 onwards (18:00 until there's data). If that hour has passed today, the slot is the same hour tomorrow.

If photos of your own are waiting (see [Your own photos](#your-own-photos)), the oldest set is used instead of steps 2-5.

If a run makes no draft, later ticks before 12:00 try again; after 12:00 a day with a draft is left as it is. A day gives up after 3 failed attempts, so a broken key can't run up costs. Runs are recorded in `daily_runs` and shown on the Instagram page.

Benchmark captions and hashtags are other people's text. The prompts tell the agents to treat them as data and never follow instructions in them. The output is validated before it is stored: category, slide count and length, caption length and real hashtags only.

### Models and cost

`model_client()` uses Cloudflare Workers AI when `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` are set, and OpenAI otherwise. `INSTAGRAM_AI_PROVIDER` overrides the choice.

- **Cloudflare (default, free):** `@cf/meta/llama-4-scout-17b-16e-instruct` writes and `@cf/black-forest-labs/flux-2-klein-4b` draws. The free plan allows 10,000 neurons a day, reset at 00:00 UTC. From Cloudflare's price list, a day of drafts should use a few thousand. In a side-by-side test of Create, Llama 4 Scout wrote more specific slides and captions than Llama 3.3 70B. `gpt-oss-120b` doesn't answer this API call format.
- **OpenAI (paid):** `gpt-5-mini` writes and `gpt-image-2.5-flare` draws. A day costs 3-9 text calls and 5-8 images.

Models can be changed with `CLOUDFLARE_TEXT_MODEL` / `CLOUDFLARE_IMAGE_MODEL` or `OPENAI_TEXT_MODEL` / `OPENAI_IMAGE_MODEL`.

## Your own photos

**Add your photos** on the Command Centre's Instagram page takes 1-10 photos and a short note, such as "Dishoom Kings Cross, lamb raan, 8/10", and works in a phone browser. It needs a RECOMMEND session, CSRF and same-origin, and every upload, refusal and deletion is written to the audit log.

- **Checks:** the type comes from each file's first bytes (JPEG or PNG), not its name or declared type. Limits: 10 photos, 12 MB each, 50 MB per upload, 50 megapixels, a 300-character note. The browser converts HEIC (iPhone) photos and shrinks large ones to JPEG before sending when it can; a HEIC file that still arrives is refused with a message to export it as JPEG.
- **Cleaning:** Pillow (hash-pinned in `deploy/command-centre/requirements.txt`, installed into each release's `vendor/`) decodes each photo, applies its EXIF rotation, converts it to sRGB and saves a new 1080×1350 (4:5) JPEG, or 1080×1080 when the first photo is landscape, centre-cropped. No EXIF, GPS, ICC profile or comment is copied. The cleaned files are kept privately under `uploads/<id>/` in the media folder; the originals are never stored. Without Pillow the form is turned off.
- **Queue:** waiting uploads are listed oldest first under **Your photos waiting**, with thumbnails, and can be deleted until a daily run uses them.
- **Drafting:** the first tick of a day with a waiting upload sends the note and up to 4 of the photos (540 px thumbnails, low detail) to **Create**, which writes the caption and a short description per photo in the voice file's style, then **Tags** and **Review** as usual. The note is the only evidence for the venue, dish, rating, price and anything else factual: Create is told not to add anything the note doesn't say, and Review scores a draft that does under 50. A model that can't take images is asked again with the note only. The photos become the slides, in upload order, of a normal draft with the usual slot, veto hour and auto-publish rule, and the upload is marked used and its folder deleted. A notification says the photos are now a draft. If every round fails, the upload stays waiting for the next run; with no upload the day gets an AI draft.
- **Posting:** a single photo posts as one image, several as a carousel.

## Your own clips (reels)

**Add a reel clip** takes one MP4 or MOV clip (3-90 s, raw or edited, up to 300 MB) and a note. The oldest waiting clip becomes that day's draft instead of a carousel (`docs/instagram-reels-plan.md`, phase 1).

- **Upload:** the browser sends `POST /api/v1/instagram/clips` (note, size), then 8 MB `application/octet-stream` chunks to `/clips/chunk?clip=<id>&offset=<n>`, then `/clips/finish`. All need RECOMMEND and the CSRF token. The clip is stored privately under `clips/<id>/`; unfinished uploads are cleared after a day.
- **Checks (`instagram_agent/video.py`):** a pure-Python MP4/MOV box reader checks what the headers state: container, `moov` before `mdat`, duration, size, display width ≤ 1920, H.264/HEVC, 23-60 fps, about 25 Mbps at most, AAC audio ≤ 48 kHz with 1-2 channels, and multi-entry edit lists. When the host has `ffprobe` (`host_probe.find_tool`), it adds chroma subsampling and interlacing. A clip that fails is dropped and the page lists every reason to fix before exporting again. Whatever neither can see is left to Meta's container `status_code`.
- **Drafting:** Create gets the note and the clip's length (not the video) and writes a hook, 2-5 timed on-screen text beats, a cover moment and a caption; Tags and Review run as for photos. Nothing is burned in: the on-screen text is shown as a script on the draft and the clip posts as filmed, with its own audio.
- **Posting:** a reel posts only once you **Approve** it. The publisher checks `content_publishing_limit`, copies the clip to the public folder, checks it with a `HEAD` request, creates a `media_type=REELS` container with `video_url`, `share_to_feed=true` and `thumb_offset`, checks `status_code` once a minute for up to 5 minutes, then calls `media_publish` once (never retried).
- **Analytics:** synced posts store `media_product_type`; reels also store average and total watch time (`avg_watch_ms`, `total_watch_ms`).
- **Host check:** each dashboard deploy prints `video host:` lines (ffmpeg/ffprobe path and version, CPUs, rlimits, cgroup limits, free disk) in the "Instagram" step's log. The first run (deploy run 44110566222, 17 Nov 2026) found `ffmpeg` 4.2.9 at `/usr/bin/ffmpeg`, **no `ffprobe`**, 18 CPUs, no address-space/data/CPU rlimits and about 2 TB free. So phase 1 on the host checks clips with the Python header reader only; chroma and interlacing are left to Meta's container `status_code`.

## Reviewing in the Command Centre

The **Instagram** page shows each draft's slides, caption, hashtags, review score and suggested publish time.

- **Reject** stops the draft from posting and saves the topic as one to avoid.
- **Approve** saves the draft as an approved example and posts it at its slot even if it scored under 70. While automatic posting is off, it shows **Copy caption + hashtags** and a download link per slide for posting by hand. The published post's performance sets how much it counts for in future drafts.

Both need a RECOMMEND session, CSRF and same-origin, and both are written to the audit log. A draft can only be decided once.

## Automatic publishing

`python3 -m instagram_agent daily` runs `publish.tick` after drafting, under the same lock. It is off (`publish_off`) unless `INSTAGRAM_AUTO_PUBLISH=1`, `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_USER_ID`, `INSTAGRAM_PUBLIC_DIR` and an `https://` `INSTAGRAM_PUBLIC_URL` are all set. The deploy writes these from the `INSTAGRAM_ACCESS_TOKEN` and `INSTAGRAM_USER_ID` secrets, with the public folder at `public_html/ig-media`; set the repository variable `INSTAGRAM_AUTO_PUBLISH` to `0` to stop posting. That value is sent on every deploy, so it also stops a token already on the server. Changing it takes effect only when a deploy of the server's current commit succeeds, and a `daily` run already in progress keeps the value it started with. To apply it straight away, re-run the **Deploy protected energy dashboard** run for the latest `main` commit (Actions → the run → **Re-run all jobs**, possible for 30 days), and check its log shows `instagram env: updated` or `instagram env: unchanged` (either means setup ran), not `skipping Instagram setup`. Until then, Reject any undecided draft that is due; an approved draft can't be stopped from the page.

- **What posts:** approved drafts, and pending drafts scoring 70 or more. Rejected and weaker pending drafts never post.
- **When:** at `max(scheduled_for, created_at + 1 hour)`, so there is always at least an hour to reject a draft. A draft more than 3 hours past its time is skipped rather than posted late.
- **How:** Meta's [content publishing API](https://developers.facebook.com/docs/instagram-platform/content-publishing): a container per JPEG slide (`is_carousel_item=true`), a `CAROUSEL` container with the caption and hashtags (a one-photo draft gets a single image container with the caption instead), a `status_code` check, then `media_publish`. Instagram Login tokens (`IG...`) use `graph.instagram.com`, Facebook Login tokens `graph.facebook.com`.
- **Public slides:** Meta fetches each image from a URL, so the slides are copied to a random 32-character folder under `INSTAGRAM_PUBLIC_DIR`, checked to be reachable, and deleted when the call finishes.
- **Failures:** recorded in `publish_runs` and shown on the draft. A failure before `media_publish` is retried on the next tick, up to 3 times. `media_publish` itself is never retried, and a crash after it started marks the draft failed with "check Instagram", so a post can't go out twice.

## Notifications

`daily` and `publish.tick` report through the shared `notifications` package: "1 Instagram draft ready to review, posts at HH:MM" once a day (or 'Your photos ("<note>") are now an Instagram draft, posts at HH:MM' when it came from an upload), "Instagram post is live: <topic>" with the post's permalink, and a failure for every failed post attempt and every failed, partial, not-configured or given-up drafting run (none for `skipped`). Each one is stored in `data/notifications.sqlite3` and listed newest first on the Command Centre **Inbox** page. With the `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` secrets set, the deploy writes them to the server's `.env` and each notification is also sent to that chat; a failed send is retried on later notifications, up to 3 times. Links point at `COMMAND_CENTRE_URL` (repository variable, default `https://iramkhan.com/command-centre/`). A notification problem never stops drafting or posting.

## How the numbers are worked out

All of these are suggestions, not measurements:

- **Benchmark ranking**: followers first, then median likes + comments per post. Instagram doesn't expose other accounts' reach.
- **Top posts**: likes + comments divided by that account's own median, the "outlier multiple" the skill pack uses.
- **Trending hashtags**: each tag scores the outlier multiples of the posts that use it. Tags used by more than one account rank first.
- **Own post score**: (likes + 2 × comments + 3 × (saves + shares)) ÷ reach.
- **Best hours**: Meta's `online_followers` per hour, blended with my own reach by posting hour once five or more posts have insights.

## Limits

- Business Discovery only returns Business and Creator accounts. Personal and age-gated accounts are marked `unavailable`.
- `online_followers` needs 100+ followers and covers the last 30 days. Meta's docs don't say which time zone its hour keys use. `best_hours` treats them as UTC until a live response confirms it.
- Insights can lag by up to 48 hours. A post whose insights call fails is skipped for that day and counted in `metric_errors`.
