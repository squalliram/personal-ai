import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("coderabbit_findings", ROOT / "scripts/coderabbit-findings.py")
findings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(findings)

ICON = '<picture><img src="https://example.test/major-16.svg" alt=""></picture>'
INLINE = f"""<!-- coderabbit-ai-generated:v1 -->

**{ICON} Major** · **Stability & Availability** · **Quick win**

<details><summary>{ICON} Supported by static analysis</summary></details>

**Keep failed test runs out of the deployment queue.**

If a deployment is running, a later failed run replaces the pending one.
"""
REVIEW = f"""**Actionable comments posted: 0**

<details>
<summary>{ICON} Nitpick comments: 1</summary><blockquote>
<details>
<summary>tests/test_energy_python.py: 1</summary><blockquote>

`128-142`: **{ICON} Trivial** · **Functional Correctness** · **Quick win**

{ICON} **Unverified finding**

**Add a discovery-mode test for `start-energy.sh`.**

Run the real launcher.

`150`: **{ICON} Minor** · **Functional Correctness** · **Quick win**

**Check the second launcher.**

Based on learnings, it matters.
</blockquote></details>
</blockquote></details>
<details><summary>{ICON} Review info</summary></details>
"""


class CodeRabbitFindingsTests(unittest.TestCase):
    def test_parses_an_inline_finding(self):
        self.assertEqual(
            findings.parse(INLINE),
            {
                "severity": "Major",
                "category": "Stability & Availability",
                "effort": "Quick win",
                "title": "Keep failed test runs out of the deployment queue",
                "flags": ["static analysis"],
            },
        )

    def test_a_reply_is_not_a_finding(self):
        self.assertIsNone(findings.parse("@devin-ai-integration[bot] Confirmed. This addresses the finding."))

    def test_parses_findings_folded_into_a_review_body(self):
        nit, second = findings.nested(REVIEW)
        self.assertEqual((second["line"], second["flags"]), ("150", ["learnings"]))
        self.assertEqual(
            (nit["kind"], nit["path"], nit["line"], nit["severity"], nit["title"], nit["flags"]),
            (
                "Nitpick comments",
                "tests/test_energy_python.py",
                "128-142",
                "Trivial",
                "Add a discovery-mode test for `start-energy.sh`",
                ["unverified"],
            ),
        )

    def test_only_coderabbit_resolving_its_thread_counts_as_confirmed(self):
        def thread(resolved, outdated, resolver=None):
            return {"isResolved": resolved, "isOutdated": outdated, "resolvedBy": resolver and {"login": resolver}}

        def with_comments(*nodes):
            return {**thread(True, False, "coderabbitai[bot]"), "comments": {"nodes": list(nodes)}}

        finding = {"author": {"login": "coderabbitai"}, "body": "finding"}
        addressed = {"author": {"login": "coderabbitai"}, "body": "finding\n\n✅ Addressed in commit a837eeb"}
        self.assertEqual(findings.outcome(with_comments(addressed)), "fixed (confirmed by CodeRabbit)")
        self.assertEqual(findings.outcome(thread(True, False, "squalliram")), "resolved")
        for reply in (
            "You're right. I'm withdrawing my request.",
            "One case remains: a withdrawal would count as a fix.",
            "This no longer addresses the finding.",
        ):
            with self.subTest(reply=reply):
                outcome = findings.outcome(with_comments(finding, {"author": {"login": "coderabbitai"}, "body": reply}))
                self.assertEqual(outcome, "resolved by CodeRabbit")
        self.assertEqual(findings.outcome(thread(True, False, "coderabbitai[bot]")), "resolved by CodeRabbit")
        ghost = with_comments(addressed, {"author": None, "body": "✅ Addressed in commit 1234567"})
        self.assertEqual(findings.outcome(ghost), "fixed (confirmed by CodeRabbit)")
        self.assertEqual(findings.outcome(thread(True, True)), "resolved")
        self.assertEqual(findings.outcome(thread(False, True)), "outdated")
        self.assertEqual(findings.outcome(thread(False, False)), "open")
        self.assertEqual(findings.outcome(None), "open")

    def test_renders_a_table_row_per_finding(self):
        base = {
            "repo": "squalliram/jarvis",
            "number": 1,
            "pr_title": "A | B",
            "pr_url": "https://github.com/squalliram/jarvis/pull/1",
            "pr_state": "merged",
        }
        row = {
            **base,
            **findings.parse(INLINE),
            "kind": "inline",
            "path": "a.py",
            "line": "3",
            "url": base["pr_url"] + "#r1",
            "date": "2026-11-01T00:00:00Z",
            "outcome": "resolved",
        }
        prs = [{**base, "author": "x", "findings": 1, "reviewed": True, "note": None}]
        text = findings.render(prs, [row], datetime(2026, 11, 17, 11, 0, tzinfo=timezone.utc))
        self.assertIn("2026-11-17 11:00 (Europe/London)", text)
        self.assertIn("### [#1](https://github.com/squalliram/jarvis/pull/1) A \\| B (merged, 1 finding)", text)
        self.assertIn(
            "| Major | Stability & Availability | Quick win | [Keep failed test runs out of the deployment queue (static analysis)]",
            text,
        )
        other = [{**prs[0], "repo": "someone/else"}]
        text = findings.render(
            other, [{**row, "repo": "someone/else"}], datetime(2026, 11, 17, tzinfo=timezone.utc), ("someone/else",)
        )
        self.assertIn("## someone/else", text)
        self.assertIn("[Keep failed test runs out of the deployment queue (static analysis)]", text)

    def test_explains_a_pr_without_a_review(self):
        self.assertEqual(findings.review_note(["Review failed: the head commit changed"], False), "review failed")
        self.assertEqual(findings.review_note(["## Review skipped"], False), "review skipped")
        self.assertIsNone(findings.review_note(["Review skipped"], True))


if __name__ == "__main__":
    unittest.main()
