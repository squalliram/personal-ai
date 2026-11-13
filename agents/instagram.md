# Instagram agent

## Purpose

Manages Instagram content: reel scripts with scored hooks, captions, carousels, stories, DM replies, weekly planning and post audits. Drafts only; nothing is posted automatically.

## Where it runs

The agent is the [instagram-agent-skill](https://github.com/squalliram/instagram-agent-skill) pack of 13 Claude Code skills. It runs inside the Jarvis bridge ([squalliram/jarvis](https://github.com/squalliram/jarvis)), which loads the pack as a local plugin, or in Claude Code with the skill folders copied into `~/.claude/skills/`. The Command Centre lists the agent and its commands; it cannot start it.

| Command | What it does |
| --- | --- |
| `/ig-reel` | One idea into a Reel: three scored hooks, the script, on-screen text and a timed beat sheet |
| `/ig-viral` | Ranks what is working in the niche by multiple over each account's median and writes the swipe file |
| `/ig-caption` | The caption, linted, with the 125 characters the feed shows before the tap |
| `/ig-carousel` | Swipe posts: the cover, slide copy and the 1080x1350 files |
| `/ig-story` | The daily story sequence, sticker choices and the DM funnel |
| `/ig-profile` | Scores the profile out of 100 against a 12-part rubric, then rewrites in fix-first order |
| `/ig-plan` | The week: what to post, which format, when, and the accounts to engage with |
| `/ig-human` | The humanizer: strips AI tells and invisible characters, then scores the draft |
| `/ig-comment` | Comments on other people's posts, picked by what the post actually is |
| `/ig-reply` | Sorts the thread under a post and drafts replies in priority order |
| `/ig-dm` | Keyword delivery, first message, collab pitch and two follow-ups |
| `/ig-repurpose` | One video, podcast or newsletter into a week of standalone reels and carousels |
| `/ig-audit` | Post-mortem on published posts, ranked by outlier multiple and sends per reach |

In Jarvis the skills are plugin-qualified (`instagram-agent:ig-reel`); asking for a reel, caption or plan in plain speech is enough.

## Before first use

Fill in `~/.claude/instagram/voice.md` (a copy of the pack's `templates/voice.md`). Every skill reads it. Alternatively, give the agent three of your own reels and ask it to write `voice.md` from them.

The scoring scripts (`hookscore.py`, `beats.py`, `caption.py`, `humanize.py`, `detect.py`, `swipe.py`) run with `python3` from each skill folder. In Jarvis they need the shell, so start the bridge with `npm run bridge:writes`; in read-only mode the agent still drafts, without scores.

## Guardrails

- Drafts only. Never posts, comments, follows, likes or sends DMs on Instagram, by API, browser automation or any other tool. Every skill ends with copy-ready text for you to post.
- Never fabricates metrics, clients or outcomes; missing numbers come back as `{{your number}}` placeholders.
- `/ig-viral` reads public posts at human speed in your own browser. No scraping, no logging in as you.
- Shared state (`voice.md`, `swipe.md`, `plan.md`, `log.md`) lives in `~/.claude/instagram/` on your machine only.
