import assert from "node:assert/strict";
import test from "node:test";
import { attentionDestination, priorityItems } from "../src/lib/attention.ts";

const item = (severity: string, href?: string) => ({ source: severity, severity, fact: severity, href });

test("prioritizes action items and issues without dashboard-only facts", () => {
  const warning = item("warning"), action = item("action"), critical = item("critical");
  assert.deepEqual(priorityItems([critical, item("info"), warning, action]), [action, critical, warning]);
  assert.deepEqual(priorityItems([]), []);
});

test("only links to the corresponding trusted Command Centre page", () => {
  assert.deepEqual(attentionDestination(item("action", "#instagram")), {
    label: "Review drafts on website", url: "https://iramkhan.com/command-centre/#instagram",
  });
  for (const href of [undefined, "https://example.com", "//example.com", "#instagram?next=other", "#settings"]) {
    assert.equal(attentionDestination(item("action", href)), undefined);
  }
});
