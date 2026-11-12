# Instagram pipeline for @instafoodielove

A daily pipeline on the hosting server that drafts food, coffee and restaurant carousels, waits for my approval in the Command Centre, publishes approved posts at the best time and learns from how they perform.

| Step | Agent | Status |
| --- | --- | --- |
| 1 | **Analytics**: own posts, daily insights, follower online hours | Done (read-only) |
| 2 | **Benchmarks**: accounts I follow, ranked by followers and engagement; their best recent posts and hashtags | Done (read-only) |
| 3 | **Create**: 3 carousels a day by 12:00, OpenAI text and images, in my voice, using the `/ig-*` skill rules | Done |
| 4 | **Tags**: up to 20 per post, mostly from `trending_hashtags` | Done |
| 5 | **Review**: scores each draft against `top_benchmark_posts`; under 70 goes back to Create with notes, up to 3 rounds | Done |
| 6 | **Queue**: Instagram page in the Command Centre with **Approve** and **Reject**; decisions feed the memory | Done |
| 7 | **Publish**: approved posts go out at the slot from `best_hours` | Next, needs an EXECUTE route |
| 8 | **DMs**: drafted replies, sent when I press send | Later, needs Meta app review |

Nothing here writes to Instagram yet. It only calls `GET` endpoints of the Instagram API with Facebook Login. Approving a draft records the decision; publishing arrives in the next step.

## Requirements

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
./scripts/install-instagram-daily.sh      # once on the server: one marked cron entry, every 15 minutes
```

From 09:00 London time, the first tick of the day:

1. Syncs analytics and benchmarks (when the Meta token is set). If Instagram is down, it drafts from the data it already has.
2. **Create** writes carousels in the voice from `INSTAGRAM_VOICE_FILE` (default `data/instagram/voice.md`), following the `ig-carousel`, `ig-caption` and `ig-human` rules from `INSTAGRAM_SKILLS_DIR` when the skill pack is installed. Topics are limited to food, coffee and restaurants. Approved examples that performed well count for more; rejected topics and the last 14 days' topics are avoided.
3. **Tags** adds up to 20 hashtags per draft.
4. **Review** scores each draft 0-100. Drafts under 70 go back to Create with the reviewer's notes, up to 3 rounds. If fewer than 3 pass, the best of the rest still go in the queue, labelled with their score.
5. OpenAI draws every slide (4:5 portrait), with the slide text drawn into the image. A draft whose images fail is dropped.
6. Each draft gets a suggested publish slot: the best hours from `best_hours`, from 12:00 onwards (18:00, 12:00 and 20:00 until there's data).

A day that already has drafts is skipped. A day gives up after 3 failed attempts, so a broken key can't run up costs. Runs are recorded in `daily_runs` and shown on the Instagram page.

Benchmark captions and hashtags are other people's text. The prompts tell the agents to treat them as data and never follow instructions in them. The output is validated before it is stored: category, slide count and length, caption length and real hashtags only.

### Cost

Each day makes 3-9 text calls and one image per slide, about 15 images for 3 five-slide carousels. Both are billed to the OpenAI account. The models are set with `OPENAI_TEXT_MODEL` (default `gpt-5-mini`) and `OPENAI_IMAGE_MODEL` (default `gpt-image-2.5-flare`). The defaults come from OpenAI's API reference.

## Reviewing in the Command Centre

The **Instagram** page shows each draft's slides, caption, hashtags, review score and suggested publish time.

- **Approve** saves the draft as an approved example. When the post is published, its performance sets how much it counts for in future drafts.
- **Reject** saves the topic as one to avoid.

Both need a RECOMMEND session, CSRF and same-origin, and both are written to the audit log. A draft can only be decided once.

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
