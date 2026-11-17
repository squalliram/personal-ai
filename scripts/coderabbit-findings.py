#!/usr/bin/env python3
"""Rebuild docs/coderabbit/findings.md: every CodeRabbit finding on the owner's PRs.

Reads pull requests, review comments, reviews and review threads with the `gh` CLI
(read-only). Run from the repository root after a PR's CodeRabbit review settles:

    python3 scripts/coderabbit-findings.py
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
BOT = "coderabbitai[bot]"
REPOS = ("squalliram/personal-ai", "squalliram/jarvis", "squalliram/instagram-agent-skill")
OUTPUT = Path("docs/coderabbit/findings.md")
SEVERITIES = ("Critical", "Major", "Minor", "Trivial", "Info")

PICTURE = re.compile(r"<picture>.*?</picture>", re.S)
HEADER = re.compile(r"\*\*\s*(Critical|Major|Minor|Trivial|Info)\*\*\s*·\s*\*\*(.+?)\*\*\s*·\s*\*\*(.+?)\*\*")
TITLE = re.compile(r"^\*\*(?!\s*(?:Critical|Major|Minor|Trivial|Info|Unverified)\b)(.+?)\*\*\s*$", re.M)
# Findings CodeRabbit folds into the review body: "Nitpick comments", "Outside diff range comments", ...
SECTION = re.compile(r"<summary>\s*([A-Z][A-Za-z ]+?comments?)(?::\s*\d+)?\s*</summary>")
FILE_GROUP = re.compile(r"<summary>([^<:]+?\.[A-Za-z0-9]+)(?:-[\d-]+)?: \d+</summary>")
NESTED = re.compile(r"^`(\d+(?:-\d+)?)`: (\*\*.*)$", re.M)
QUERY = """query($o:String!,$r:String!,$n:Int!,$c:String){repository(owner:$o,name:$r){pullRequest(number:$n){
reviewThreads(first:100,after:$c){pageInfo{hasNextPage endCursor}
nodes{isResolved isOutdated resolvedBy{login} comments(first:100){nodes{databaseId author{login} body}}}}}}}"""


def gh(*args: str):
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def rest(path: str) -> list[dict]:
    return [item for page in gh("api", "--paginate", "--slurp", path) for item in page]


def threads(repo: str, number: int) -> list[dict]:
    owner, name = repo.split("/")
    nodes, cursor = [], None
    while True:
        args = ["api", "graphql", "-f", f"query={QUERY}", "-F", f"o={owner}", "-F", f"r={name}", "-F", f"n={number}"]
        page = gh(*args, *(["-F", f"c={cursor}"] if cursor else []))["data"]["repository"]["pullRequest"]
        nodes += page["reviewThreads"]["nodes"]
        if not page["reviewThreads"]["pageInfo"]["hasNextPage"]:
            return nodes
        cursor = page["reviewThreads"]["pageInfo"]["endCursor"]


def parse(body: str) -> dict | None:
    """Severity, category, effort and title of one CodeRabbit finding, or None for a reply."""
    text = PICTURE.sub("", body)
    header = HEADER.search(text)
    if not header:
        return None
    title = TITLE.search(text, header.end())
    flags = [
        label
        for label, marker in (
            ("unverified", "Unverified finding"),
            ("static analysis", "Supported by static analysis"),
            ("learnings", "Based on learnings"),
        )
        if marker in text
    ]
    return {
        "severity": header.group(1),
        "category": header.group(2).strip(),
        "effort": header.group(3).strip(),
        "title": (title.group(1).strip() if title else "(untitled)").rstrip("."),
        "flags": flags,
    }


def nested(body: str) -> list[dict]:
    """Findings inside a review body, each tagged with its section and file."""
    text = PICTURE.sub("", body)
    found, section, path = [], None, None
    marks = [(m.start(), "section", m.group(1)) for m in SECTION.finditer(text)]
    marks += [(m.start(), "file", m.group(1)) for m in FILE_GROUP.finditer(text)]
    marks += [(m.start(), "finding", m) for m in NESTED.finditer(text)]
    marks.sort(key=lambda mark: mark[0])
    for index, (_, kind, value) in enumerate(marks):
        if kind == "section":
            section = value
        elif kind == "file":
            path = value.strip()
        elif section and (
            finding := parse(text[value.start() : marks[index + 1][0] if index + 1 < len(marks) else None])
        ):
            found.append({**finding, "kind": section, "path": path, "line": value.group(1)})
    return found


def outcome(thread: dict | None) -> str:
    if thread is None:
        return "open"
    if thread["isResolved"]:
        # CodeRabbit resolves its own thread only after it accepts the fix.
        return (
            "fixed (confirmed by CodeRabbit)"
            if (thread.get("resolvedBy") or {}).get("login") in ("coderabbitai", "coderabbitai[bot]")
            else "resolved"
        )
    return "outdated" if thread["isOutdated"] else "open"


def collect(repo: str) -> tuple[list[dict], list[dict]]:
    prs, findings = [], []
    for pr in sorted(rest(f"repos/{repo}/pulls?state=all&per_page=100"), key=lambda p: p["number"]):
        number = pr["number"]
        state = "merged" if pr.get("merged_at") else pr["state"]
        notes = [
            c["body"] for c in rest(f"repos/{repo}/issues/{number}/comments?per_page=100") if c["user"]["login"] == BOT
        ]
        inline = [c for c in rest(f"repos/{repo}/pulls/{number}/comments?per_page=100") if c["user"]["login"] == BOT]
        reviews = [r for r in rest(f"repos/{repo}/pulls/{number}/reviews?per_page=100") if r["user"]["login"] == BOT]
        by_root = (
            {t["comments"]["nodes"][0]["databaseId"]: t for t in threads(repo, number) if t["comments"]["nodes"]}
            if inline
            else {}
        )
        base = {"repo": repo, "number": number, "pr_title": pr["title"], "pr_url": pr["html_url"], "pr_state": state}
        count = 0
        for comment in inline:
            if comment.get("in_reply_to_id") or not (finding := parse(comment["body"])):
                continue
            line = comment.get("line") or comment.get("original_line")
            start = comment.get("start_line") or comment.get("original_start_line")
            span = f"{start}-{line}" if start and start != line else str(line or "")
            findings.append(
                {
                    **base,
                    **finding,
                    "kind": "inline",
                    "path": comment["path"],
                    "line": span,
                    "url": comment["html_url"],
                    "date": comment["created_at"],
                    "outcome": outcome(by_root.get(comment["id"])),
                }
            )
            count += 1
        for review in reviews:
            for finding in nested(review["body"]):
                findings.append(
                    {
                        **base,
                        **finding,
                        "url": review["html_url"],
                        "date": review["submitted_at"],
                        "outcome": "review body (untracked)",
                    }
                )
                count += 1
        prs.append(
            {
                **base,
                "author": pr["user"]["login"],
                "findings": count,
                "reviewed": bool(reviews or inline),
                "note": review_note(notes, bool(reviews or inline)),
            }
        )
    return prs, findings


def review_note(notes: list[str], reviewed: bool) -> str | None:
    """Why a PR has no CodeRabbit review, from its summary comment."""
    if reviewed:
        return None
    text = " ".join(notes)
    if "Review failed" in text:
        return "review failed"
    if "Review skipped" in text:
        return "review skipped"
    return "summary only, no review comments" if notes else "not reviewed"


def cell(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).replace("|", "\\|")


def render(prs: list[dict], findings: list[dict], now: datetime, repos: tuple[str, ...] = REPOS) -> str:
    lines = [
        "# CodeRabbit findings log",
        "",
        f"Generated by `scripts/coderabbit-findings.py` on {now.astimezone(LONDON):%Y-%m-%d %H:%M} (Europe/London). "
        "Do not edit by hand; rerun the script. See [README.md](README.md) for the configuration and the comparison with other reviewers.",
        "",
        "Outcome: `fixed (confirmed by CodeRabbit)` means CodeRabbit accepted the fix and resolved the thread itself; "
        "`resolved` means someone else resolved the thread; `outdated` means the commented lines changed but the thread stayed open; "
        "`review body (untracked)` findings sit in the review summary, where GitHub has no thread to resolve.",
        "",
        "## Summary",
        "",
        "| Repository | PRs | PRs reviewed | Findings | Critical | Major | Minor | Trivial | Fixed or resolved |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for repo in repos:
        mine = [f for f in findings if f["repo"] == repo]
        sev = Counter(f["severity"] for f in mine)
        done = sum(f["outcome"].startswith(("fixed", "resolved")) for f in mine)
        repo_prs = [p for p in prs if p["repo"] == repo]
        lines.append(
            f"| [{repo}](https://github.com/{repo}) | {len(repo_prs)} | {sum(p['reviewed'] for p in repo_prs)} | {len(mine)} | "
            + " | ".join(str(sev[s]) for s in SEVERITIES[:4])
            + f" | {done} |"
        )
    lines += ["", "### By category", "", "| Category | Findings |", "|---|---|"]
    lines += [f"| {cell(k)} | {v} |" for k, v in Counter(f["category"] for f in findings).most_common()]
    lines += ["", "### By outcome", "", "| Outcome | Findings |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in Counter(f["outcome"] for f in findings).most_common()]
    for repo in repos:
        lines += ["", f"## {repo}"]
        for pr in (p for p in prs if p["repo"] == repo):
            mine = [f for f in findings if f["repo"] == repo and f["number"] == pr["number"]]
            status = pr["note"] or f"{len(mine)} finding{'s' if len(mine) != 1 else ''}"
            lines += ["", f"### [#{pr['number']}]({pr['pr_url']}) {cell(pr['pr_title'])} ({pr['pr_state']}, {status})"]
            if not mine:
                continue
            lines += [
                "",
                "| Severity | Category | Effort | Finding | Where | Kind | Outcome |",
                "|---|---|---|---|---|---|---|",
            ]
            for f in sorted(mine, key=lambda f: (SEVERITIES.index(f["severity"]), f["date"])):
                where = f"`{cell(f['path'] or '')}:{f['line']}`" if f["path"] else ""
                title = cell(f["title"]) + (f" ({', '.join(f['flags'])})" if f["flags"] else "")
                lines.append(
                    f"| {f['severity']} | {cell(f['category'])} | {cell(f['effort'])} | [{title}]({f['url']}) | {where} | {cell(f['kind'])} | {f['outcome']} |"
                )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--repo", action="append", help="owner/name; repeatable (default: the owner's three repos)")
    args = parser.parse_args()
    prs, findings, repos = [], [], tuple(args.repo or REPOS)
    for repo in repos:
        repo_prs, repo_findings = collect(repo)
        prs += repo_prs
        findings += repo_findings
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(prs, findings, datetime.now(LONDON), repos), encoding="utf-8")
    print(f"{len(findings)} findings across {len(prs)} PRs -> {args.output}")


if __name__ == "__main__":
    main()
