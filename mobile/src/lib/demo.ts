import type { Inbox, Overview } from "./api";

export const demoOverview: Overview = {
  generated_at: "2026-11-18T19:10:00+00:00",
  attention: [
    { source: "instagram", severity: "action", fact: "One sample Instagram draft is on hold for review.", href: "#instagram" },
    { source: "energy", severity: "warning", fact: "The sample gas meter readings are a day behind.", href: "#energy" },
  ],
};

export const demoInbox: Inbox = {
  state: "available", page: 1, pages: 1,
  entries: [
    { id: 3, created_at: "2026-11-18T18:45:00+00:00", source: "energy", outcome: "ok", title: "Sample energy sync completed", link: null },
    { id: 2, created_at: "2026-11-18T17:20:00+00:00", source: "instagram", outcome: "ok", title: "Sample draft is ready for review", link: null },
    { id: 1, created_at: "2026-11-18T09:00:00+00:00", source: "briefing", outcome: "ok", title: "Sample daily briefing is ready", link: null },
  ],
};

export const demoQuestions = [
  {
    question: "What needs my attention today?",
    answer: "In this demo, the Instagram draft needs review and the gas meter readings are a day behind. No changes were made.",
  },
  {
    question: "What’s next on Instagram?",
    answer: "The sample queue has two pending drafts. One needs review before the next scheduled slot; nothing was published.",
  },
  {
    question: "Did the energy sync run?",
    answer: "Yes, the sample sync completed. The gas meter readings are still a day behind, so the sync and the reading date tell you different things.",
  },
];
