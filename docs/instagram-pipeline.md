# Instagram pipeline for @instafoodielove

A daily pipeline on the hosting server that drafts food, coffee and restaurant carousels, shows them in the Command Centre where I can reject them, publishes the rest at the best time and learns from how they perform.

| Step | Agent | Status |
| --- | --- | --- |
| 1 | **Analytics**: own posts, daily insights, follower online hours | Done (read-only) |
| 2 | **Benchmarks**: accounts I follow, ranked by followers and engagement; their best recent posts and hashtags | Done (read-only) |
| 3 | **Create**: 3 carousels a day by 12:00, in my voice, using the `/ig-*` skill rules. Text and images from Cloudflare Workers AI (free) or OpenAI | Done |
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
4. **Review** scores each draft 0-100. Drafts under 70 go back to Create with the reviewer's notes, up to 3 rounds. If fewer than 3 pass, the best of the rest still go in the queue, labelled with their score.
5. The image model draws every slide at 1024×1280 (4:5 portrait). OpenAI's image model also draws the slide's short text onto it. Cloudflare's FLUX.2 klein garbles lettering, so with Cloudflare the slides are clean photos and the caption carries the words. A slide image that fails gets one more try; if it still fails, that draft is dropped and the reason is recorded. Cloudflare returns JPEGs and OpenAI returns PNGs; each is saved in the format it arrives in.
6. Each draft gets a suggested publish slot: the best hours from `best_hours`, from 12:00 onwards (18:00, 12:00 and 20:00 until there's data).

If a run makes fewer than 3 drafts, later ticks before 12:00 top the day up to 3, avoiding the slots already suggested; after 12:00 a day with drafts is left as it is. A day gives up after 3 failed or partial attempts, so a broken key can't run up costs. Runs are recorded in `daily_runs` and shown on the Instagram page.

Benchmark captions and hashtags are other people's text. The prompts tell the agents to treat them as data and never follow instructions in them. The output is validated before it is stored: category, slide count and length, caption length and real hashtags only.

### Models and cost

`model_client()` uses Cloudflare Workers AI when `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` are set, and OpenAI otherwise. `INSTAGRAM_AI_PROVIDER` overrides the choice.

- **Cloudflare (default, free):** `@cf/meta/llama-4-scout-17b-16e-instruct` writes and `@cf/black-forest-labs/flux-2-klein-4b` draws. The free plan allows 10,000 neurons a day, reset at 00:00 UTC. From Cloudflare's price list, a day of drafts should use a few thousand. In a side-by-side test of Create, Llama 4 Scout wrote more specific slides and captions than Llama 3.3 70B. `gpt-oss-120b` doesn't answer this API call format.
- **OpenAI (paid):** `gpt-5-mini` writes and `gpt-image-2.5-flare` draws. A day costs 3-9 text calls and about 15 images.

Models can be changed with `CLOUDFLARE_TEXT_MODEL` / `CLOUDFLARE_IMAGE_MODEL` or `OPENAI_TEXT_MODEL` / `OPENAI_IMAGE_MODEL`.

## Reviewing in the Command Centre

The **Instagram** page shows each draft's slides, caption, hashtags, review score and suggested publish time.

- **Reject** stops the draft from posting and saves the topic as one to avoid.
- **Approve** saves the draft as an approved example and posts it at its slot even if it scored under 70. While automatic posting is off, it shows **Copy caption + hashtags** and a download link per slide for posting by hand. The published post's performance sets how much it counts for in future drafts.

Both need a RECOMMEND session, CSRF and same-origin, and both are written to the audit log. A draft can only be decided once.

## Automatic publishing

`python3 -m instagram_agent daily` runs `publish.tick` after drafting, under the same lock. It is off (`publish_off`) unless `INSTAGRAM_AUTO_PUBLISH=1`, `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_USER_ID`, `INSTAGRAM_PUBLIC_DIR` and an `https://` `INSTAGRAM_PUBLIC_URL` are all set. The deploy writes these from the `INSTAGRAM_ACCESS_TOKEN` and `INSTAGRAM_USER_ID` secrets, with the public folder at `public_html/ig-media`; set the repository variable `INSTAGRAM_AUTO_PUBLISH` to `0` to stop posting. That value is sent on every deploy, so it also stops a token already on the server.

- **What posts:** approved drafts, and pending drafts scoring 70 or more. Rejected and weaker pending drafts never post.
- **When:** at `max(scheduled_for, created_at + 1 hour)`, so there is always at least an hour to reject a draft. A draft more than 3 hours past its time is skipped rather than posted late.
- **How:** Meta's [content publishing API](https://developers.facebook.com/docs/instagram-platform/content-publishing): a container per JPEG slide (`is_carousel_item=true`), a `CAROUSEL` container with the caption and hashtags, a `status_code` check, then `media_publish`. Instagram Login tokens (`IG...`) use `graph.instagram.com`, Facebook Login tokens `graph.facebook.com`.
- **Public slides:** Meta fetches each image from a URL, so the slides are copied to a random 32-character folder under `INSTAGRAM_PUBLIC_DIR`, checked to be reachable, and deleted when the call finishes.
- **Failures:** recorded in `publish_runs` and shown on the draft. A failure before `media_publish` is retried on the next tick, up to 3 times. `media_publish` itself is never retried, and a crash after it started marks the draft failed with "check Instagram", so a post can't go out twice.

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
