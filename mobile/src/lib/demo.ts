import type { Inbox, Overview } from "./api";

export const demoOverview: Overview = {
  generated_at: "2026-11-18T19:10:00+00:00",
  status: { state: "degraded", automations_monitored: 4, automations_healthy: 3 },
  attention: [
    { source: "instagram", severity: "action", fact: "One sample Instagram draft is on hold for review." },
    { source: "energy", severity: "warning", fact: "The sample gas meter readings are a day behind." },
  ],
  instagram: {
    status: "operational", summary: "Sample weekly content queue.",
    pending_drafts: 2, next_slot: "2026-11-19T18:00:00+00:00",
  },
  energy: { state: "available", last_successful_sync_at: "2026-11-18T18:45:00+00:00" },
  briefing: { latest_report_date: "2026-11-18" },
};

export const demoInbox: Inbox = {
  state: "available", page: 1, pages: 1,
  entries: [
    { id: 3, created_at: "2026-11-18T18:45:00+00:00", source: "energy", outcome: "ok", title: "Sample energy sync completed", link: null },
    { id: 2, created_at: "2026-11-18T17:20:00+00:00", source: "instagram", outcome: "ok", title: "Sample draft is ready for review", link: null },
    { id: 1, created_at: "2026-11-18T09:00:00+00:00", source: "briefing", outcome: "ok", title: "Sample daily briefing is ready", link: null },
  ],
};
