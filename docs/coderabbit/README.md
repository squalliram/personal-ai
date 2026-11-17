# CodeRabbit on the owner's repositories

[`findings.md`](findings.md) lists every CodeRabbit finding on every pull request in [squalliram/personal-ai](https://github.com/squalliram/personal-ai), [squalliram/jarvis](https://github.com/squalliram/jarvis) and [squalliram/instagram-agent-skill](https://github.com/squalliram/instagram-agent-skill). For each finding it records the severity, category, effort, file and line, and outcome. `scripts/coderabbit-findings.py` generates it, read-only, through the `gh` CLI.

## Keeping the log current

The [CodeRabbit findings log](../../.github/workflows/coderabbit-findings.yml) workflow rebuilds `findings.md` every day at 06:17 UTC and on demand (Actions → Run workflow). It commits to `main` only when a finding changed. Commits made with the workflow's `GITHUB_TOKEN` do not start Test, so they never trigger a deploy. To refresh by hand:

```bash
python3 scripts/coderabbit-findings.py              # all three repositories
python3 scripts/coderabbit-findings.py --repo squalliram/jarvis --output /tmp/jarvis.md   # one repository
```

The script needs `gh` with read access. It never posts, resolves or edits anything.

## Configuration

Only `personal-ai` has a [`.coderabbit.yaml`](../../.coderabbit.yaml). `jarvis` and `instagram-agent-skill` use CodeRabbit's organization defaults.

```yaml
# yaml-language-server: $schema=https://coderabbit.ai/integrations/schema.v2.json
language: en-US

reviews:
  profile: chill                  # fewer nitpicks than "assertive"
  request_changes_workflow: false # comments only, never "Request changes"
  high_level_summary: true
  poem: false
  review_status: true
  auto_review:
    enabled: true
    drafts: false
  path_filters:                   # generated data is never reviewed
    - "!data/**"
    - "!reports/**"
    - "!**/*.sqlite3"
  tools:
    ruff:                         # Ruff is configured in pyproject.toml
      enabled: true
    pylint:
      enabled: false

chat:
  auto_reply: true
```

## What CodeRabbit does that the alternatives don't

The sources are each vendor's documentation, read on 2026-11-17 and linked below, and the 200 findings in `findings.md`. Vendor features change often, so check the linked pages before relying on a row.

| | CodeRabbit | GitHub Copilot code review | Claude Code Review | OpenAI Codex code review |
|---|---|---|---|---|
| Git platforms | GitHub, GitHub Enterprise Server, GitLab (cloud and self-managed), Azure DevOps, Bitbucket Cloud and Data Center | GitHub | GitHub (Claude Code can also run in your own GitHub Actions or GitLab CI) | GitHub |
| Deterministic analyzers in the review | Bundled linters and security scanners run on the PR and feed the review: Ruff here, plus ast-grep, Semgrep, OpenGrep, TruffleHog, Betterleaks, OSV-Scanner, Checkov, Trivy, Presidio and more | Separate product: GitHub Code Quality adds CodeQL rules and coverage alongside the review | Model agents only; no bundled linters documented | Model only; no bundled linters documented |
| Configuration | One schema-validated `.coderabbit.yaml`: review profile, path filters, per-tool switches, path instructions, chat | `.github/copilot-instructions.md`, `*.instructions.md`, `AGENTS.md`, skills (read from the PR's head branch) | `REVIEW.md`, read from the base branch and never from the PR's own changes | Review guidelines in `AGENTS.md` |
| Memory across PRs | "Learnings" taught in `@coderabbitai` chat, reused on later PRs (8 findings here cite them) | Copilot Memory, custom instructions | `REVIEW.md` / `CLAUDE.md` | `AGENTS.md` |
| What it reports | Every severity from Critical to Trivial, with a category and an effort estimate on each finding | Comments plus an approval assessment | Findings ranked by severity after a verification pass | On GitHub, only P0 and P1 issues |
| Hand-off to coding agents | Every finding has a "Prompt for AI Agents", plus one "Prompt to fix all issues" and a "Fix comments" checkbox | Copilot cloud agent can apply fixes | Claude Code (not compared) | Codex (not compared) |
| Fix verification | Replies to "fixed in ..." by checking the new commit, then resolves the thread itself (6 confirmed here) | Not documented | Not documented | Not documented |
| Merge gates | Built-in pre-merge checks (e.g. docstring coverage) and custom natural-language checks (Team plan and above) | Copilot approvals can count toward required reviews when enabled | A check run on the PR; never approves | Not documented |
| Before the PR | Same reviewer in the CLI and in VS Code-family and JetBrains IDEs; plugs into Claude Code, Codex, Cursor and Gemini | Copilot in the IDE | Claude Code local review | Codex CLI and IDE (not compared) |
| Pricing model | Free and OSS plans, then Essentials, Team, Advanced and Enterprise with per-developer rate limits | AI credits per review, with an estimated cost range for each effort level | Team and Enterprise plans | Not compared |

So CodeRabbit is the only one of these that:

1. **Runs real linters and SAST in the same review.** Many findings marked "Supported by static analysis" in `findings.md` show the commands it executed before commenting.
2. **Works on any major Git host.** The others are documented for GitHub only.
3. **Is fully configured as code in one validated YAML file,** including which tools run and which paths are ignored.
4. **Checks its own findings after a fix.** It reruns the check on the new commit, replies whether the change addresses the finding, and resolves the thread.
5. **Labels each finding with severity, category and effort, and an AI-agent prompt.** That makes `findings.md` possible without hand triage.

### Where the alternatives are stronger

- **Noise.** Codex reports only P0 and P1. Claude Code Review drops candidate findings that fail its verification step. CodeRabbit posted 145 Minor or Trivial findings here, 73% of the total. It also labels some findings "Unverified finding" (5 here).
- **Prompt-injection resistance.** Claude Code Review reads `REVIEW.md` from the base branch only, so a PR cannot rewrite its own review rules.
- **Native GitHub approvals.** Copilot's approvals can count toward branch protection.
- **Reliability seen here.** CodeRabbit reported "Review failed" on personal-ai PRs #12 to #18, and posted only a summary on several docs-only PRs. On GitLab it skips merge requests opened by the account it posts as. It also stops incremental reviews after several pushes, so compare "Last reviewed commit" with HEAD before merging.

When another reviewer is evaluated, add a column to the table with a link to its documentation, and add one row only if it has a capability none of the existing columns cover.

Sources: [CodeRabbit docs index](https://docs.coderabbit.ai/llms.txt), [tools](https://docs.coderabbit.ai/tools/list.md), [knowledge base](https://docs.coderabbit.ai/integrations/knowledge-base.md), [pre-merge checks](https://docs.coderabbit.ai/pr-reviews/pre-merge-checks.md), [plans](https://docs.coderabbit.ai/management/plans.md); [GitHub Copilot code review](https://docs.github.com/en/copilot/concepts/agents/code-review); [Claude Code Review](https://code.claude.com/docs/en/code-review), [Claude Code GitHub Actions](https://code.claude.com/docs/en/github-actions); [Codex code review](https://developers.openai.com/codex/integrations/github).
