# Instagram pipeline for @instafoodielove

A daily pipeline on the hosting server that drafts food, coffee and restaurant carousels, waits for my approval in the Command Centre, publishes approved posts at the best time and learns from how they perform.

| Step | Agent | Status |
| --- | --- | --- |
| 1 | **Analytics**: own posts, daily insights, follower online hours | This module (read-only) |
| 2 | **Benchmarks**: accounts I follow, ranked by followers and engagement; their best recent posts and hashtags | This module (read-only) |
| 3 | **Memory**: approved and rejected drafts, with approved posts weighted by how they performed | Storage and scoring in this module; approve/reject buttons come with the queue |
| 4 | Create: 3 carousels a day by 12:00, OpenAI text and images, `/ig-*` skill rules | Next |
| 5 | Tags: up to 20 per post, from `trending_hashtags` | Next |
| 6 | Review: scoring scripts plus comparison with `top_benchmark_posts` | Next |
| 7 | Publish: only after **Approve** in the Command Centre, at a slot from `best_hours` | Later, needs an EXECUTE route |
| 8 | DMs: drafted replies, sent when I press send | Later, needs Meta app review |

Nothing in this module writes to Instagram. It only calls `GET` endpoints of the Instagram API with Facebook Login.

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
