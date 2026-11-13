"use strict";
(() => {
  const SECTIONS = [
    ["overview", "Overview"], ["my-day", "My Day"], ["tasks", "Tasks"], ["calendar", "Calendar"],
    ["energy", "Energy"], ["career", "Career"], ["finance", "Finance"], ["family", "Family"],
    ["health", "Health"], ["agents", "AI Agents"], ["instagram", "Instagram"], ["automations", "Automations"],
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
        body: opts.body === undefined ? undefined : JSON.stringify(opts.body), signal: opts.signal,
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
    assistantGeneration += 1;
    asking = false;
    closeAssistant(false);
    $("assistant-toggle").hidden = true;
    $("assistant-log").replaceChildren();
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
    setupAssistant();
  }

  const VAULT_LIMIT_MS = 1500;
  const VAULT_SEALED_MS = 1100;
  const VAULT_RELEASE_MS = 500;
  const VAULT_GRANTED_MS = 650;
  const VAULT = {
    unlock: { shell: "vault-arrive", caption: "Access granted" },
    lock: { shell: "vault-depart", caption: "Logging out of the command centre" },
  };
  const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let vaultRun = 0;
  let cancelVault = () => {};

  // Unlock resolves immediately. Lock holds the sealed dial until `ready` settles, then fades out
  // and resolves with `ready`'s value so the caller can reveal the login screen underneath.
  function playVault(mode, ready = Promise.resolve()) {
    const overlay = $("vault");
    const shell = $("app-view");
    if (!overlay || reducedMotion()) return ready;
    const { shell: shellClass, caption } = VAULT[mode];
    cancelVault();
    const run = ++vaultRun;
    let timer = 0;
    const reset = () => {
      overlay.classList.remove("run", "unlock", "lock", "release");
      shell.classList.remove("vault-arrive", "vault-depart");
    };
    const finish = () => {
      clearTimeout(timer);
      overlay.removeEventListener("animationend", onEnd);
      reset();
      overlay.hidden = true;
    };
    const onEnd = (event) => { if (event.animationName === "vault-out") finish(); };
    cancelVault = () => { clearTimeout(timer); overlay.removeEventListener("animationend", onEnd); };
    $("vault-caption").textContent = caption;
    overlay.hidden = false;
    reset();
    void overlay.offsetWidth;
    overlay.addEventListener("animationend", onEnd);
    overlay.classList.add("run", mode);
    shell.classList.add(shellClass);
    if (mode === "unlock") {
      timer = setTimeout(finish, VAULT_LIMIT_MS);
      return ready;
    }
    const sealed = new Promise((resolve) => setTimeout(resolve, VAULT_SEALED_MS));
    return Promise.all([ready.catch(() => null), sealed]).then(() => {
      if (run !== vaultRun) return ready;
      overlay.classList.add("release");
      timer = setTimeout(finish, VAULT_RELEASE_MS);
      return ready;
    });
  }

  const FEMALE_VOICE = /female|woman|samantha|victoria|karen|moira|tessa|serena|fiona|kate|zira|hazel|susan|libby|sonia|aria|jenny|natasha|catherine/i;
  function pickVoice() {
    const voices = window.speechSynthesis.getVoices().filter((voice) => /^en/i.test(voice.lang));
    return voices.find((voice) => FEMALE_VOICE.test(voice.name) && /^en-GB/i.test(voice.lang))
      || voices.find((voice) => FEMALE_VOICE.test(voice.name)) || voices[0] || null;
  }
  function speak(text, delay = 0) {
    if (!("speechSynthesis" in window) || typeof SpeechSynthesisUtterance === "undefined") return;
    setTimeout(() => {
      const utterance = new SpeechSynthesisUtterance(text);
      const voice = pickVoice();
      if (voice) { utterance.voice = voice; utterance.lang = voice.lang; }
      utterance.rate = 0.92;
      utterance.pitch = 0.75;
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(utterance);
    }, delay);
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
    await pendingLogout;
    const result = await api("api/v1/session", { method: "POST", body: { password, remember: $("remember").checked } });
    $("password").value = "";
    if (result.ok) {
      playVault("unlock");
      speak(VAULT.unlock.caption, reducedMotion() ? 0 : VAULT_GRANTED_MS);
      showApp(result.data);
    }
    else $("login-status").textContent = LOGIN_ERRORS[result.data && result.data.error] || "Sign-in failed.";
  }

  const LOGOUT_DEADLINE_MS = 10000;
  let pendingLogout = Promise.resolve();
  async function logout() {
    const button = $("logout");
    if (button.disabled) return;
    button.disabled = true;
    speak(VAULT.lock.caption);
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), LOGOUT_DEADLINE_MS);
    const revoke = api("api/v1/session", { method: "DELETE", signal: controller.signal })
      .finally(() => clearTimeout(deadline));
    let settled;
    pendingLogout = new Promise((resolve) => { settled = resolve; });
    try {
      const result = await playVault("lock", revoke);
      if (result.ok || result.status === 401) showLogin("You have signed out.");
      else $("view-status").textContent = "Sign-out did not complete. Try again.";
    } finally {
      button.disabled = false;
      settled();
    }
  }

  // Ask Jarvis ------------------------------------------------------------------------------
  const QuestionRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const ASSISTANT_ERRORS = {
    assistant_not_configured: "Ask Jarvis needs an OpenAI key on the server.",
    assistant_unavailable: "Jarvis could not reach OpenAI. Try again in a moment.",
    rate_limited: "That's the hourly question limit. Try again later.",
    network_error: "The server could not be reached.",
  };
  let questionListener = null;
  let asking = false;
  let assistantGeneration = 0;

  async function setupAssistant() {
    const result = await api("api/v1/assistant");
    const available = Boolean(result.ok && result.data.available && session);
    $("assistant-toggle").hidden = !available;
    $("assistant-mic").hidden = !QuestionRecognition;
  }

  function orb(state, message) {
    $("assistant-orb").className = `orb ${state}`;
    if (message !== undefined) $("assistant-state").textContent = message;
  }

  function openAssistant() {
    $("assistant").hidden = false;
    $("assistant-toggle").setAttribute("aria-expanded", "true");
    $("assistant-input").focus();
  }

  function closeAssistant(returnFocus = true) {
    assistantGeneration += 1;
    if (asking) orb("idle", "Question cancelled. Ask again when you're ready.");
    asking = false;
    stopQuestionListening();
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
    $("assistant").hidden = true;
    $("assistant-toggle").setAttribute("aria-expanded", "false");
    if (returnFocus) $("assistant-toggle").focus();
  }

  function addTurn(kind, text) {
    const item = el("li", { class: `turn-${kind}` },
      el("span", { class: "from" }, kind === "you" ? "You" : "Jarvis"), text);
    $("assistant-log").append(item);
    item.scrollIntoView({ block: "nearest" });
  }

  async function askJarvis(question) {
    question = question.trim();
    if (!question || asking) return;
    stopQuestionListening();
    asking = true;
    const generation = assistantGeneration;
    $("assistant-input").value = "";
    addTurn("you", question);
    orb("thinking", "Searching your Jarvis data…");
    const result = await api("api/v1/assistant", { method: "POST", body: { question } });
    // Signed out (or timed out) while waiting: never show or speak a private answer.
    if (generation !== assistantGeneration || !session) return;
    asking = false;
    if (result.status === 401) { showLogin("Your session has ended. Sign in again."); return; }
    if (!result.ok) {
      const message = ASSISTANT_ERRORS[result.data && result.data.error] || "Jarvis could not answer that.";
      addTurn("error", message);
      orb("error", message);
      return;
    }
    addTurn("jarvis", result.data.answer);
    orb("speaking", "Answering…");
    speak(result.data.answer);
    setTimeout(() => { if (!asking && !questionListener) orb("idle", "Ask another question, or close."); },
      Math.min(20000, 1500 + result.data.answer.length * 55));
  }

  function stopQuestionListening() {
    if (questionListener) {
      const active = questionListener;
      questionListener = null;
      active.abort();
    }
    $("assistant-mic").setAttribute("aria-pressed", "false");
    $("assistant-mic").classList.remove("listening");
  }

  function listenForQuestion() {
    if (questionListener) { stopQuestionListening(); orb("idle", "Stopped listening."); return; }
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
    const listener = new QuestionRecognition();
    listener.lang = "en-GB";
    listener.interimResults = true;
    let finalText = "";
    listener.onresult = (event) => {
      if (questionListener !== listener) return;
      const text = Array.from(event.results).map((r) => r[0].transcript).join(" ");
      $("assistant-input").value = text;
      if (event.results[event.results.length - 1].isFinal) finalText = text;
    };
    listener.onerror = (event) => {
      if (questionListener !== listener) return;
      stopQuestionListening();
      orb("error", ["not-allowed", "service-not-allowed"].includes(event.error)
        ? "Microphone access is blocked for this site." : "Jarvis could not hear you. Try again or type.");
    };
    listener.onend = () => {
      if (questionListener !== listener) return;
      stopQuestionListening();
      if (finalText.trim()) askJarvis(finalText);
      else orb("idle", "Jarvis did not catch that. Try again or type.");
    };
    questionListener = listener;
    $("assistant-mic").setAttribute("aria-pressed", "true");
    $("assistant-mic").classList.add("listening");
    orb("listening", "Listening…");
    try { listener.start(); } catch {
      stopQuestionListening();
      orb("error", "Voice input is unavailable in this browser. Type your question.");
    }
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
        session.remembered ? ["Remembered device", "Yes, until the session expires or you sign out"]
          : ["Idle timeout", `${Math.round(session.idle_timeout_seconds / 60)} minutes`]]),
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

  const DRAFT_STATUS = {
    pending: "Awaiting review", approved: "Approved", rejected: "Rejected", published: "Published", failed: "Failed",
  };

  async function decide(draft, decision, buttons) {
    for (const button of buttons) button.disabled = true;
    const result = await api("api/v1/instagram/decision", { method: "POST", body: { draft_id: draft.draft_id, decision } });
    if (result.status === 401) { showLogin("Your session has ended. Sign in again."); return; }
    if (!result.ok) {
      $("view-status").textContent = `Could not save the decision (${(result.data && result.data.error) || result.status}).`;
      for (const button of buttons) button.disabled = false;
      return;
    }
    render();
  }

  const slideUrl = (draft, n) => `api/v1/instagram/slide?draft=${encodeURIComponent(draft.draft_id)}&n=${n}`;

  function postKit(draft) {
    const copy = el("button", { type: "button", class: "primary" }, "Copy caption + hashtags");
    copy.addEventListener("click", async () => {
      const text = draft.hashtags.length ? `${draft.caption}\n\n${draft.hashtags.join(" ")}` : draft.caption;
      try {
        await navigator.clipboard.writeText(text);
        $("view-status").textContent = "Caption and hashtags copied.";
      } catch {
        $("view-status").textContent = "Couldn't copy automatically; select the caption above and copy it.";
      }
    });
    const downloads = draft.slides.map((slide, i) =>
      el("a", { href: slideUrl(draft, i + 1), download: `instafoodielove-${draft.created_on}-${i + 1}.${slide.format}` }, `Slide ${i + 1}`));
    return el("div", { class: "ig-actions" }, copy, el("span", { class: "ig-downloads" }, "Download: ", downloads));
  }

  function draftCard(draft, canDecide) {
    const slides = el("div", { class: "ig-slides" }, draft.slides.map((slide, i) => el("figure", {},
      el("img", {
        src: slideUrl(draft, i + 1),
        alt: slide.text, width: "200", height: "250", loading: "lazy",
      }),
      el("figcaption", {}, `${i + 1}. ${slide.text}`))));
    const children = [
      slides,
      el("p", { class: "ig-caption" }, draft.caption),
      el("p", { class: "muted" }, draft.hashtags.length ? `${draft.hashtags.length} hashtags: ${draft.hashtags.join(" ")}` : "No hashtags"),
      keyValues([
        ["Status", DRAFT_STATUS[draft.status] || draft.status],
        ["Category", draft.category],
        ["Review score (suggestion)", draft.review_score === null ? "Not scored" : `${draft.review_score}/100`],
        ["Reviewer notes", draft.review_notes || "None"],
        ["Suggested publish time", when(draft.scheduled_for)],
      ]),
    ];
    if (draft.status === "pending" && canDecide) {
      const approve = el("button", { type: "button", class: "primary" }, "Approve");
      const reject = el("button", { type: "button" }, "Reject");
      approve.addEventListener("click", () => decide(draft, "approve", [approve, reject]));
      reject.addEventListener("click", () => decide(draft, "reject", [approve, reject]));
      children.push(el("div", { class: "ig-actions" }, approve, reject));
    } else if (draft.status === "pending") {
      children.push(muted("Approving or rejecting needs RECOMMEND permission."));
    } else if (draft.status === "approved") {
      children.push(muted("Approved and saved as an example. Post it yourself: download the slides in order and copy the caption."));
      children.push(postKit(draft));
    }
    return card(`${draft.created_on} · ${draft.topic}`, ...children);
  }

  async function instagram(view) {
    const data = await load("api/v1/instagram/drafts");
    if (!data) return;
    const canDecide = Boolean(session) && ["recommend", "execute"].includes(session.permission);
    const last = data.runs[0];
    view.append(card("Daily drafts",
      muted("Three carousel drafts for @instafoodielove are ready by 12:00 London time. Nothing is posted to Instagram from here: approve the ones you like, then post them yourself."),
      last ? keyValues([["Last run", when(last.started_at)], ["Result", last.outcome], ["Drafts made", last.drafts],
        ...(last.error ? [["Error", last.error]] : [])])
        : muted(data.state === "not_configured" ? "Not connected: there is no Instagram database on this server yet." : "No daily run recorded yet.")));
    for (const draft of data.drafts) view.append(draftCard(draft, canDecide));
  }


  const VIEWS = { overview, energy, agents: agentsView, integrations, automations: automationsView, settings, instagram };

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
    $("assistant-toggle").addEventListener("click", () => ($("assistant").hidden ? openAssistant() : closeAssistant()));
    $("assistant-close").addEventListener("click", () => closeAssistant());
    $("assistant-mic").addEventListener("click", listenForQuestion);
    $("assistant-form").addEventListener("submit", (event) => { event.preventDefault(); askJarvis($("assistant-input").value); });
    $("assistant").addEventListener("keydown", (event) => { if (event.key === "Escape") closeAssistant(); });
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
