"use strict";
(() => {
  const SECTIONS = [
    ["overview", "Overview"], ["my-day", "My Day"], ["tasks", "Tasks"], ["calendar", "Calendar"],
    ["energy", "Energy"], ["career", "Career"], ["finance", "Finance"], ["family", "Family"],
    ["health", "Health"], ["agents", "AI Agents"], ["automations", "Automations"],
    ["integrations", "Integrations"], ["settings", "Settings"],
  ];
  const STATUS_LABELS = {
    operational: "Operational", running: "Running", degraded: "Degraded", stale: "Stale", failed: "Failed",
    refresh_required: "Refresh required", not_configured: "Not configured", not_implemented: "Not implemented",
    unverified: "Unverified", available: "Available", planned: "Planned", partial: "Partial",
  };
  const BASIS_LABELS = { measured: "Measured", estimated: "Estimated", inferred: "Inferred", source_record: "Source record" };
  let csrf = null;
  let session = null;
  const $ = (id) => document.getElementById(id);

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (key === "class") node.className = value;
      else node.setAttribute(key, value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  const londonTime = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/London", dateStyle: "medium", timeStyle: "short" });
  function when(value) {
    if (!value) return "Unavailable";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : londonTime.format(date);
  }
  const money = (v) => (typeof v === "number" ? `£${v.toFixed(2)}` : "Unavailable");
  const kwh = (v) => (typeof v === "number" ? `${v.toFixed(2)} kWh` : "Unavailable");
  const status = (s) => el("span", { class: `status status-${s}` }, STATUS_LABELS[s] || s || "Unknown");
  const basis = (b) => el("span", { class: `basis basis-${b}` }, BASIS_LABELS[b] || b);
  const card = (title, ...children) => el("section", { class: "card" }, el("h2", {}, title), ...children);
  const muted = (text) => el("p", { class: "muted" }, text);

  function table(columns, rows, empty) {
    if (!rows || rows.length === 0) return muted(empty || "No records.");
    return el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, columns.map(([label]) => el("th", { scope: "col" }, label)))),
      el("tbody", {}, rows.map((row) => el("tr", {}, columns.map(([, render]) => el("td", {}, render(row))))))));
  }

  function keyValues(entries) {
    return el("dl", { class: "kv" }, entries.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v ?? "Unavailable"))));
  }

  async function api(path, options) {
    const opts = options || {};
    const headers = { Accept: "application/json" };
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    if (opts.method && opts.method !== "GET" && csrf) headers["X-CSRF-Token"] = csrf;
    let response;
    try {
      response = await fetch(path, {
        method: opts.method || "GET", headers, credentials: "same-origin", cache: "no-store",
        body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      });
    } catch {
      return { ok: false, status: 0, data: { error: "network_error" } };
    }
    let data = null;
    try { data = await response.json(); } catch { data = null; }
    return { ok: response.ok, status: response.status, data };
  }

  function showLogin(message) {
    session = null; csrf = null;
    $("app-view").hidden = true;
    $("login-view").hidden = false;
    $("login-status").textContent = message || "";
    $("password").value = "";
    $("password").focus();
  }

  function showApp(info) {
    session = info; csrf = info.csrf_token;
    $("login-view").hidden = true;
    $("app-view").hidden = false;
    $("permission-badge").textContent = `Permission: ${info.permission.toUpperCase()}`;
    render();
  }

  const BOOT_SWEEP_LIMIT_MS = 1500;
  function bootSweep() {
    const overlay = $("boot-sweep");
    if (!overlay || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let timer = 0;
    const finish = () => {
      clearTimeout(timer);
      overlay.removeEventListener("animationend", onEnd);
      overlay.classList.remove("run");
      overlay.hidden = true;
    };
    const onEnd = (event) => { if (event.animationName === "boot-reveal") finish(); };
    overlay.hidden = false;
    overlay.classList.remove("run");
    void overlay.offsetWidth;
    overlay.addEventListener("animationend", onEnd);
    overlay.classList.add("run");
    timer = setTimeout(finish, BOOT_SWEEP_LIMIT_MS);
  }

  const LOGIN_ERRORS = {
    invalid_credentials: "That passphrase was not accepted.",
    rate_limited: "Too many attempts. Wait 15 minutes and try again.",
    authentication_not_configured: "Sign-in is not configured on the server.",
    network_error: "The server could not be reached.",
  };

  async function login(event) {
    event.preventDefault();
    const password = $("password").value;
    if (!password) { $("login-status").textContent = "Enter your passphrase."; return; }
    $("login-status").textContent = "Signing in…";
    const result = await api("api/v1/session", { method: "POST", body: { password } });
    $("password").value = "";
    if (result.ok) { bootSweep(); showApp(result.data); }
    else $("login-status").textContent = LOGIN_ERRORS[result.data && result.data.error] || "Sign-in failed.";
  }

  async function logout() {
    await api("api/v1/session", { method: "DELETE" });
    showLogin("You have signed out.");
  }

  async function load(path) {
    const result = await api(path);
    if (result.status === 401) { showLogin("Your session has ended. Sign in again."); return null; }
    if (!result.ok) {
      $("view-status").textContent = `Could not load data (${(result.data && result.data.error) || result.status}).`;
      return null;
    }
    return result.data;
  }

  // Views -----------------------------------------------------------------------------------

  function widget(title, subtitle, options, ...children) {
    const classes = ["widget", options.span ? `span-${options.span}` : "", options.className || ""].filter(Boolean).join(" ");
    return el("section", { class: classes, "aria-label": title },
      el("header", { class: "widget-head" }, el("h2", {}, title), subtitle ? el("p", { class: "muted" }, subtitle) : null),
      ...children);
  }

  function greeting(now) {
    const hour = Number(new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/London", hour: "numeric", hour12: false }).format(now));
    return hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  }

  function monthGrid(isoDate) {
    const [year, month, day] = isoDate.split("-").map(Number);
    const first = new Date(Date.UTC(year, month - 1, 1));
    const days = new Date(Date.UTC(year, month, 0)).getUTCDate();
    const offset = (first.getUTCDay() + 6) % 7;
    const name = new Intl.DateTimeFormat("en-GB", { month: "long", timeZone: "UTC" }).format(first);
    const cells = [];
    for (let i = 0; i < offset; i += 1) cells.push(el("span", { class: "day blank", "aria-hidden": "true" }));
    for (let d = 1; d <= days; d += 1) {
      cells.push(el("span", d === day ? { class: "day today", "aria-current": "date" } : { class: "day" }, String(d)));
    }
    return el("div", { class: "month" },
      el("div", { class: "month-head" }, el("strong", {}, name), el("span", { class: "muted" }, String(year))),
      el("div", { class: "month-grid", role: "group", "aria-label": `${name} ${year}` },
        ["M", "T", "W", "T", "F", "S", "S"].map((d) => el("span", { class: "dow", "aria-hidden": "true" }, d)), cells));
  }

  function energyBars(rows) {
    const byDate = new Map();
    for (const row of rows) {
      if (row.fuel !== "electricity" || typeof row.consumption_kwh !== "number") continue;
      byDate.set(row.date, row.consumption_kwh);
    }
    const points = [...byDate.entries()].sort().slice(-7);
    if (points.length === 0) return muted("No measured electricity days available.");
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 280 110");
    svg.setAttribute("class", "bars");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", points.map(([d, v]) => `${d}: ${v.toFixed(1)} kWh`).join("; "));
    const max = Math.max(...points.map(([, v]) => v)) || 1;
    const width = 280 / points.length;
    points.forEach(([date, value], i) => {
      const height = Math.max(2, (value / max) * 80);
      const rect = document.createElementNS(ns, "rect");
      rect.setAttribute("x", String(i * width + 6));
      rect.setAttribute("y", String(88 - height));
      rect.setAttribute("width", String(width - 12));
      rect.setAttribute("height", String(height));
      rect.setAttribute("rx", "4");
      const tip = document.createElementNS(ns, "title");
      tip.textContent = `${date}: ${value.toFixed(2)} kWh (measured)`;
      rect.append(tip);
      const label = document.createElementNS(ns, "text");
      label.setAttribute("x", String(i * width + width / 2));
      label.setAttribute("y", "104");
      label.setAttribute("text-anchor", "middle");
      label.textContent = date.slice(8);
      svg.append(rect, label);
    });
    return svg;
  }

  const HEALTH = {
    nominal: { label: "Nominal", detail: "No source health check needs attention." },
    degraded: { label: "Degraded", detail: "One or more sources or automations need attention." },
    fault: { label: "Fault", detail: "One or more sources or automations have failed." },
  };
  const DEGRADED_STATUSES = ["degraded", "stale", "refresh_required"];

  function aggregateHealth(data) {
    const severities = new Set(data.attention.map((a) => a.severity));
    const counts = data.integration_counts || {};
    if (severities.has("critical") || counts.failed) return "fault";
    if (severities.has("warning") || DEGRADED_STATUSES.some((s) => counts[s])) return "degraded";
    return "nominal";
  }

  function svg(tag, attrs) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    return node;
  }

  function reactor(data) {
    const state = aggregateHealth(data);
    const counts = data.integration_counts || {};
    const tracked = Object.entries(counts).filter(([s]) => s !== "not_implemented").reduce((sum, [, n]) => sum + n, 0);
    const operational = counts.operational || 0;
    const ring = svg("svg", { viewBox: "0 0 120 120", "aria-hidden": "true", focusable: "false" });
    ring.append(
      svg("circle", { class: "r-outer", cx: 60, cy: 60, r: 56 }),
      svg("circle", { class: "r-ticks", cx: 60, cy: 60, r: 50 }),
      svg("circle", { class: "r-dash", cx: 60, cy: 60, r: 42 }),
      svg("circle", { class: "r-inner", cx: 60, cy: 60, r: 30 }),
      svg("circle", { class: "r-core", cx: 60, cy: 60, r: 18 }),
      svg("circle", { class: "r-heart", cx: 60, cy: 60, r: 8 }));
    const ratio = tracked ? `${operational} of ${tracked} tracked sources operational` : "No tracked sources yet";
    let bar = null;
    if (tracked) {
      bar = el("div", { class: "hud-bar", "aria-hidden": "true" });
      bar.style.setProperty("--v", String(operational / tracked));
    }
    return widget("System status", "Aggregate source health", { className: `reactor-widget health-${state}` },
      el("div", { class: "reactor", role: "img", "aria-label": `System status: ${HEALTH[state].label}. ${ratio}.` },
        ring, el("p", { class: "reactor-state", "aria-hidden": "true" }, HEALTH[state].label), bar),
      el("p", { class: "reactor-detail" }, `${HEALTH[state].detail} ${ratio}.`));
  }

  async function overview(view) {
    const data = await load("api/v1/overview");
    if (!data) return;
    const now = new Date(data.generated_at);
    const longDate = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/London", weekday: "long", day: "numeric", month: "long" }).format(now);
    $("view-title").textContent = greeting(now);
    $("view-status").textContent = `${longDate} · Europe/London`;
    const energyState = data.energy.state === "available" ? "operational" : data.energy.state;
    const latestRun = data.briefing.latest_run;
    view.append(el("div", { class: "widgets" },
      reactor(data),
      widget("Needs attention", "Facts from source health checks", { span: 2 },
        data.attention.length === 0 ? muted("Nothing needs attention.") :
          el("ul", { class: "alerts" }, data.attention.slice(0, 6).map((a) => el("li", { class: `alert alert-${a.severity}` },
            el("strong", {}, a.severity.toUpperCase()), " ", a.fact))),
        data.attention.length > 6 ? el("a", { href: "#integrations" }, `View all ${data.attention.length}`) : null),
      widget("Monthly grid", "This month", {}, monthGrid(data.date),
        muted("No calendar source is connected yet, so no events are marked.")),
      widget("Top 3 today", "AI-suggested priorities", {},
        el("ol", { class: "slots" }, [1, 2, 3].map((n) => el("li", {}, el("span", { class: "slot-n" }, String(n)), el("span", { class: "muted" }, "Not available")))),
        muted("Arrives in Phase 2 from the Notion task snapshot. Suggestions will be labelled and linked to evidence.")),
      widget("Energy, last 7 days", "Electricity", { span: 2 },
        el("p", {}, status(energyState), " ", basis("measured"), " ", basis("estimated")),
        energyBars(data.energy.recent_days),
        muted(data.energy.actual_bills.reason),
        el("a", { href: "#energy" }, "Open energy")),
      widget("Daily briefing", "Latest run", {},
        latestRun ? keyValues([["Date", latestRun.report_date], ["Events", String(latestRun.calendar_events)],
          ["Tasks", String(latestRun.tasks_processed)], ["Connector errors", String(latestRun.error_count)]]) :
          muted("No briefing run recorded.")),
      widget("Automations", "Execution evidence", {},
        el("ul", { class: "plain" }, data.automations.map((j) => el("li", {}, status(j.status), ` ${j.name}`))),
        el("a", { href: "#automations" }, "View automations")),
      widget("Integrations", "Source health", {},
        el("ul", { class: "plain" }, Object.entries(data.integration_counts).map(([s, n]) => el("li", {}, status(s), ` ${n}`))),
        el("a", { href: "#integrations" }, "View integrations")),
      widget("Not connected yet", "Missing data sources", { span: 2 },
        el("ul", { class: "compact" }, data.missing_integrations.map((i) => el("li", {}, el("strong", {}, i.name), ` · ${i.summary}`))),
        el("p", { class: "muted" }, data.not_yet_available.map((i) => `${i.item}: Phase ${i.phase}`).join(" · "))),
    ));
  }
  async function energy(view) {
    const data = await load("api/v1/energy");
    if (!data) return;
    const h = data.health;
    view.append(
      card("Source status", el("p", {}, status(h.status), " ", h.summary),
        keyValues([["Last successful sync", when(data.last_successful_sync_at)],
          ["Latest sync result", h.evidence.latest_sync_status], ["Latest daily data", h.evidence.latest_daily_date]])),
      card("How to read these numbers",
        el("ul", {}, el("li", {}, basis("measured"), " Consumption comes from smart-meter readings."),
          el("li", {}, basis("estimated"), " Costs apply stored tariff rates. They are not bills."),
          el("li", {}, basis("inferred"), " Anomalies use a statistical rule (mean + 2 standard deviations)."),
          el("li", {}, "Actual bills: ", data.actual_bills.reason)),
        muted(data.notice)),
      card("Available-period totals", table([["Fuel", (r) => r.fuel], ["Usage", (r) => kwh(r.consumption_kwh)],
        ["Estimated cost", (r) => money(r.total_cost_gbp)]], data.totals, "No totals available.")),
      card("Daily, last 14 days", table([["Date", (r) => r.date], ["Fuel", (r) => r.fuel],
        ["Usage", (r) => kwh(r.consumption_kwh)], ["Estimated cost", (r) => money(r.total_cost_gbp)]],
      data.daily.slice().reverse(), "No daily data available.")),
      card("Monthly estimates (not bills)", table([["Month", (r) => r.month], ["Fuel", (r) => r.fuel],
        ["Usage", (r) => kwh(r.consumption_kwh)], ["Estimated cost", (r) => money(r.total_cost_gbp)]],
      data.monthly, "No monthly data available.")),
      card("Stored tariffs", table([["Fuel", (r) => r.fuel], ["Tariff code", (r) => r.tariff_code || "Unavailable"],
        ["Valid from", (r) => when(r.valid_from)]], data.tariffs, "No tariff records.")),
      card("Unusual days", el("p", {}, basis("inferred")), table([["Date", (r) => r.date], ["Fuel", (r) => r.fuel],
        ["Usage", (r) => kwh(r.consumption_kwh)], ["Threshold", (r) => kwh(r.threshold_kwh)]],
      data.anomalies, "No unusual days detected.")),
    );
  }

  async function integrations(view) {
    const data = await load("api/v1/integrations");
    if (!data) return;
    view.append(card("Sources",
      muted("A source is Operational only when its own execution evidence confirms it."),
      table([["Source", (r) => r.name], ["Status", (r) => status(r.status)], ["Kind", (r) => r.kind.replace(/_/g, " ")],
        ["Evidence", (r) => r.summary]], data.integrations)));
  }

  async function automationsView(view) {
    const data = await load("api/v1/automations");
    if (!data) return;
    for (const job of data.automations) {
      const rows = [["Purpose", job.purpose], ["Trigger", job.trigger], ["Next execution", job.next_execution_note]];
      if (job.installed_by) rows.push(["Installed by", job.installed_by]);
      if (job.local_cron) rows.push(["Cron on this host", job.local_cron.marked_entries === null ?
        `Not verified (${job.local_cron.state})` : `${job.local_cron.marked_entries} marked entr${job.local_cron.marked_entries === 1 ? "y" : "ies"}`]);
      if (job.stop_control) rows.push(["Stop control", `${job.stop_control.script}: ${job.stop_control.note}`]);
      rows.push(["Controls", "Enable, disable and run now arrive in Phase 5."]);
      view.append(card(job.name, el("p", {}, status(job.status), " ", job.summary), keyValues(rows),
        el("h3", {}, "Execution history"),
        table([["Started", (r) => when(r.started_at)], ["Result", (r) => r.status || (r.error_count ? "errors" : "completed")],
          ["Detail", (r) => r.error_category || (r.error_count !== undefined ? `${r.error_count} connector error(s)` : "")]],
        job.execution_history, "No execution history recorded.")));
    }
  }

  async function settings(view) {
    const data = await load("api/v1/audit");
    if (!data || !session) return;
    view.append(
      card("Session", keyValues([["Permission", session.permission.toUpperCase()], ["Session expires", when(session.expires_at)],
        ["Idle timeout", `${Math.round(session.idle_timeout_seconds / 60)} minutes`]]),
      el("ul", {}, el("li", {}, "READ: retrieve and analyse data."),
        el("li", {}, "RECOMMEND: suggest changes and prepare drafts."),
        el("li", {}, "EXECUTE: perform explicitly approved operations. No execute operations exist yet."))),
      card("Audit log", muted("Operational metadata only. Content and secrets are never recorded."),
        table([["When", (r) => when(r.at)], ["Actor", (r) => r.actor], ["Action", (r) => r.action], ["Outcome", (r) => r.outcome]],
          data.entries, "No audit entries.")));
  }

  async function placeholder(view, id) {
    const data = await load("api/v1/modules");
    if (!data) return;
    const module = data.modules.find((m) => m.id === id);
    if (!module) return;
    view.append(card("Status", el("p", {}, status(module.state), ` Planned for Phase ${module.phase}.`), el("p", {}, module.summary),
      table([["Source", (r) => r.id.replace(/_/g, " ")], ["Status", (r) => status(r.status)]], module.sources, "No data sources.")));
  }

  async function agentsView(view) {
    await placeholder(view, "agents");
    const data = await load("api/v1/agents");
    if (!data) return;
    for (const agent of data.agents) {
      view.append(card(agent.name, el("p", {}, status(agent.status), " ", agent.summary),
        keyValues([["Runs in", agent.runs_in], ["Definition", agent.definition], ["Guardrail", agent.guardrail]]),
        agent.commands.length ? [el("h3", {}, "Commands"),
          table([["Command", (r) => el("code", {}, r.command)], ["What it does", (r) => r.purpose]], agent.commands)] : null));
    }
  }

  const VIEWS = { overview, energy, agents: agentsView, integrations, automations: automationsView, settings };

  let renderCount = 0;
  async function render() {
    if (!session) return;
    const id = (location.hash || "#overview").slice(1);
    const section = SECTIONS.find(([key]) => key === id) || SECTIONS[0];
    for (const link of document.querySelectorAll("#nav-list a")) {
      if (link.dataset.section === section[0]) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    $("view-title").textContent = section[1];
    $("view-status").textContent = "Loading…";
    const current = ++renderCount;
    $("view").replaceChildren();
    const view = document.createDocumentFragment();
    await (VIEWS[section[0]] ? VIEWS[section[0]](view) : placeholder(view, section[0]));
    if (current !== renderCount) return;
    $("view").replaceChildren(view);
    if ($("view-status").textContent === "Loading…") $("view-status").textContent = "";
    if (section[0] !== "overview" && $("view-title").textContent !== section[1]) $("view-title").textContent = section[1];
    $("nav").classList.remove("open");
    $("menu-toggle").setAttribute("aria-expanded", "false");
  }

  async function boot() {
    $("nav-list").append(...SECTIONS.map(([id, label]) =>
      el("li", {}, el("a", { href: `#${id}`, "data-section": id }, label))));
    $("login-form").addEventListener("submit", login);
    $("logout").addEventListener("click", logout);
    $("menu-toggle").addEventListener("click", () => {
      const open = $("nav").classList.toggle("open");
      $("menu-toggle").setAttribute("aria-expanded", String(open));
    });
    window.addEventListener("hashchange", render);
    const result = await api("api/v1/session");
    if (result.ok) showApp(result.data);
    else showLogin(result.status === 0 ? LOGIN_ERRORS.network_error : "");
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
